# Provider usage and cache telemetry

Observability only. This captures real token usage, provider cache effectiveness,
latency, and estimated cost so a **future** router can make evidence-based
provider decisions. It adds no router, gateway, proxy, cache, database, or
dashboard, and it never changes provider selection or escalation behavior.

## Integration point

Every provider call already flows through one path:

```text
ModelProvider.execute() -> ProviderResponse -> AgentRuntime._record_span() -> inference span JSONL
```

Extending the existing span schema is the single integration point, so hosted and
local providers are covered without duplicating a telemetry model.

## Normalized usage fields

The inference span adds these optional, null-capable columns (all absent values
stay `null`; nothing is fabricated):

| Field | Meaning |
|---|---|
| `cache_miss_tokens` | normal (uncached) input tokens |
| `total_tokens` | provider-reported or `input + output` |
| `cache_hit_ratio` | `cached / (cached + miss)`, `null` for a zero-token request |
| `estimated_input_cost` | miss tokens at the normal input rate |
| `estimated_cached_input_cost` | cached tokens at the cached rate |
| `estimated_output_cost` | output tokens at the output rate |
| `estimated_total_cost` | sum of the three above, only when all three are known |
| `pricing_profile` | price entry that produced the estimate |
| `pricing_currency` | configured currency label |
| `prompt_prefix_hash` | `sha256:` digest of the first 2048 prompt characters |

Existing fields already cover `provider`, `model`, `timestamp_utc`, `run_id`,
`span_id`, `input_tokens`, `cached_input_tokens`, `reasoning_tokens`, and
`duration_ms`. `span_id` is the request id and `trace_id`/`run_id` is the session;
no duplicate identifier columns are added.

## DeepSeek

`providers.normalize_openai_usage()` reads the actual response `usage`:

- input: `prompt_tokens`; output: `completion_tokens`
- cached: `prompt_cache_hit_tokens`, else `prompt_tokens_details.cached_tokens`
- miss: `prompt_cache_miss_tokens`, else derived from `input - cached` only when
  both are known and the subtraction is valid
- reasoning: `completion_tokens_details.reasoning_tokens`

Cache hits are always taken from what the API reported; they are never inferred
from prompt content.

## Qwen / OpenAI-compatible

The same normalizer reads `usage.prompt_tokens_details.cached_tokens`, so a future
OpenAI-compatible Qwen endpoint normalizes with no new code. The configured Qwen
provider is Ollama's native `/api/generate`, which reports only
`prompt_eval_count`/`eval_count` and **no** cache field; local Ollama usage
therefore reports `cached_input_tokens = null` and is never assigned a fake cache
cost.

## Pricing and cost

Pricing is data in [`../config/pricing.yaml`](../config/pricing.yaml), not code:

```json
{
  "schema_version": 1, "pricing_version": "0.1.0", "currency": "USD",
  "providers": {"deepseek": {"models": {"deepseek-flash": {
    "normal_input_price_per_million": 0.28,
    "cached_input_price_per_million": 0.028,
    "output_price_per_million": 0.42
  }}}}
}
```

```text
estimated_input_cost        = cache_miss_tokens      / 1_000_000 * normal_input_price
estimated_cached_input_cost = cached_input_tokens    / 1_000_000 * cached_input_price
estimated_output_cost       = output_tokens          / 1_000_000 * output_price
estimated_total_cost        = input + cached + output
```

The shipped file is **empty by default**, so estimated cost stays `null` until a
human configures prices; the config is never silently assumed. All three prices
are required per model entry. Each span keeps the `pricing_profile` that produced
its estimate, so changing prices never rewrites history.

## Report

```bash
./scripts/cache-metrics
./scripts/cache-metrics --json
./scripts/cache-metrics --spans path/to/spans.jsonl
```

```text
Provider Cache Metrics
----------------------

deepseek / deepseek-flash
  requests          87 (successful: 87)
  input tokens      7,326,753
  cached tokens     6,384,187
  cache miss        942,566
  cache hit ratio   87.14%
  output tokens     76,605
  avg latency       2200ms
  estimated cost    0.474850 USD
```

It reports totals, overall/by-provider/by-model cache hit ratio, averages per
request, and estimated cost. `trace-report` also shows per-trace cache-miss and
total tokens.

## OpenCode stats import

`./scripts/import-opencode-stats` maps `opencode stats --json` into the same
usage vocabulary. It runs the command (or reads `--input`/`--stdin`), validates
the normalized rows, and appends them to
`telemetry/opencode/stats.jsonl`. It does not create a plugin, does not read
OpenCode's database, does not need `DEEPSEEK_API_KEY`, and does not touch OpenCode
auth.

Mapping (OpenCode -> normalized column):

| OpenCode | Normalized |
|---|---|
| `model.providerID` | `provider` |
| `model.id` | `model` |
| `model.variant` | `variant` (`high`/`low`/`default`) |
| `tokens.input` | `cache_miss_tokens` (OpenCode `input` is the **uncached** portion) |
| `tokens.cache.read` | `cached_input_tokens` |
| `tokens.cache.write` | `cache_write_tokens` |
| `tokens.output` | `output_tokens` |
| `tokens.reasoning` | `reasoning_tokens` |
| `cost` | `provider_reported_cost` |
| `sessions`/`subagents`/`prompts`/`steps` | same names |

`input_tokens` is derived as `tokens.input + tokens.cache.read` (the provider
total) and `cache_hit_ratio = cache.read / (cache.read + tokens.input)`, matching
OpenCode's own ~97.8% example. `cache.read` is **never** reinterpreted as a miss.
`provider_reported_cost` stays separate from `estimated_total_cost`; the importer
never overwrites one with the other.

Each import is one snapshot: the mapped rows are hashed into a content-derived
`snapshot_id` shared by every row. Re-importing identical stats is a no-op
(`duplicate: true`, nothing appended), so repeated runs cannot duplicate an
aggregate. `cache-metrics` reads the file automatically when present (or via
`--opencode`), reporting the **latest** snapshot per provider/model/variant
rather than summing cumulative aggregates.

```bash
./scripts/import-opencode-stats                 # runs `opencode stats --json`
./scripts/import-opencode-stats --input stats.json
some-command | ./scripts/import-opencode-stats --stdin
./scripts/cache-metrics                          # includes OpenCode rows when imported
```

## Stable-prefix observability

`telemetry.hash_prompt_prefix()` stores only a domain-separated SHA-256 digest of
the first 2048 prompt characters. Comparing digests across requests with the same
logical agent context detects a prefix that keeps changing (which defeats provider
prefix caching) without ever storing prompt content. It is a hint for future
optimization, not an input to routing.

## Privacy

Spans carry metadata and token counts only. Prompts, source, credentials, auth
headers, and raw paths are never stored; the existing secret guard still scans
every span field, and the prefix is stored only as a digest.

## Codex and OpenCode (deferred)

- **Codex**: not touched. Its ChatGPT auth (`~/.codex/auth.json`) is untouched and
  no fake cache metric is produced. The schema keeps cache/cost fields optional so
  Codex usage events can later map into the same columns via OpenTelemetry.
- **OpenCode gateway**: this repository exposes no OpenAI-compatible endpoint, and
  none is added now. The `import-opencode-stats` importer reads OpenCode's own
  aggregate stats; a forwarding endpoint remains a separate decision.
- **Peak/off-peak pricing**: deferred until a real provider schedule justifies it.
- **Dynamic routing on cache**: deferred; collect data first.
