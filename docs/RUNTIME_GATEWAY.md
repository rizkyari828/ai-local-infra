# Runtime gateway (OpenCode -> Local AI Infra -> DeepSeek)

A transparent, OpenAI-compatible pass-through endpoint. OpenCode keeps talking
to its own DeepSeek credential, but its requests now flow through Local AI Infra
so every completed chat completion emits a normalized inference span.

```
OpenCode ──► oc-gateway (127.0.0.1:8788) ──► https://api.deepseek.com
     Authorization forwarded verbatim            usage/cache/cost parsed
```

This is **pass-through + telemetry only**. There is no routing, model selection,
fallback, retry change, or cost-aware policy. The `import-opencode-stats`
importer is unchanged and remains an independent aggregate cross-check.

## Run it

```bash
./scripts/oc-gateway                 # foreground, telemetry on
./scripts/oc-gateway --debug         # log request lines only (never headers/bodies)
./scripts/oc-gateway --check         # print effective config and exit
./scripts/oc-gateway --no-telemetry  # forward only
./scripts/oc-gateway --upstream-base-url http://127.0.0.1:9999
```

It is one stdlib HTTP server in `scripts/common/gateway.py`; it reuses
`providers.normalize_openai_usage`, the runtime span builder
(`runtime.build_inference_span`), and `pricing.estimate_usage_cost`. It is not a
separate service, database, container, or daemon framework.

Endpoint: `POST /v1/chat/completions` (and the `/chat/completions` alias).
`GET /v1/models` is proxied without inference telemetry. Any other path is 404.

Upstream base URL is configuration-driven: `--upstream-base-url`, or the
`LAI_GATEWAY_UPSTREAM_BASE_URL` environment variable, defaulting to
`https://api.deepseek.com`. Every request path is appended verbatim, so
`/v1/chat/completions` reaches `https://api.deepseek.com/v1/chat/completions`.

## Auth

OpenCode sends its existing DeepSeek `Authorization` header; the gateway
forwards it to the upstream. The gateway never needs, stores, or returns the
credential.

- Never logged (debug logs only the request line and status).
- Never persisted, never in telemetry, never echoed in errors.
- `Accept-Encoding` is forced to `identity` so relayed bodies (and SSE usage
  chunks) are parseable without decompression.
- No repository file or committed config contains a credential.

## Telemetry

One `inference` span per completed chat completion, written to
`telemetry/spans/runs.jsonl` (gitignored) in the existing span schema, so
`cache-metrics` and `trace-report` see it without new models:

- provider, model, role
- input / cached input / cache-miss / output / reasoning / total tokens
- cache hit ratio, latency, success/failure
- provider-reported cost is not invented; locally estimated cost comes from
  `config/pricing.yaml` when configured
- `prompt_prefix_hash` only (never the prompt)
- trace/run ids; an incoming UUID `x-request-id` (or session id) is reused as
  `trace_id` when present

Streaming: chunks are relayed line-by-line as they arrive, never buffered. Only
the final SSE `usage` object is parsed. On client disconnect the span is still
written with whatever usage was observed. Telemetry failure never breaks a
response: the writer catches validation/IO errors and the response is unaffected.

## Failure behavior

Upstream status and body are preserved; errors are never collapsed into a 500.

| Upstream | Gateway status | Span `error_category` / `error_code` |
| --- | --- | --- |
| 400 | 400 | `validation_failure` / `bad_request` |
| 401/403 | 401/403 | `configuration_failure` / `auth_error` |
| 404 | 404 | `configuration_failure` / `model_missing` |
| 429 | 429 | `provider_failure` / `rate_limited` |
| 5xx | 5xx | `provider_failure` / `http_5xx` |
| unreachable | 502 | `provider_failure` / `connection_error` |
| timeout | 504 | `provider_timeout` / `timeout` |

## OpenCode configuration

The global config `~/.config/opencode/opencode.json` overrides only the DeepSeek
endpoint; the credential, catalog, model IDs, and `high`/`low`/`default`
variants are untouched (OpenCode merges provider `settings` over the catalog).

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "providers": {
    "deepseek": {
      "settings": { "baseURL": "http://127.0.0.1:8788/v1" }
    }
  }
}
```

Back up the previous config before editing it (in this workspace there was no
pre-existing `opencode.json`).

## Rollback

Stop the gateway and remove the `providers.deepseek.settings.baseURL` override
(or set it back to `https://api.deepseek.com/v1`). OpenCode then talks to
DeepSeek directly again; no other change is required. The gateway has no state
to migrate.

```bash
# restore direct OpenCode -> DeepSeek
$EDITOR ~/.config/opencode/opencode.json   # delete the providers.deepseek.settings.baseURL override
kill "$(cat /tmp/opencode/lai-gateway.pid)"
```

## Verification (2026-09-18, OpenCode v2.0.8, DeepSeek live)

- Streaming request through the gateway: 200, response delivered, spans written.
- Tool-use request: the shell tool ran and returned output.
- Variants `#high` and `#low` both worked; `deepseek/deepseek-flash` default too.
- Ponytail still active through the gateway (first ladder rung quoted,
  `PONYTAIL=yes LEVEL=full`).
- Cache/usage telemetry captured, e.g. `cached=153,088 miss=254
  cache_hit_ratio=0.9983 latency=8913ms`.
- `grep -iE 'sk-|authorization|bearer'` over the gateway log: 0 matches; the
  span JSONL contains no prompt or auth text.

## Cross-check vs `opencode stats --json`

`import-opencode-stats` remains the independent aggregate check. One controlled
request:

- OpenCode delta: uncached input 8,078, cache read 162,560, output 193,
  reasoning 517, cost 0.00212538.
- Gateway spans for the same window: cache miss 7,709, cached 163,712,
  output 604.

Uncached/cache-read match within ~3% (strong directional agreement). Semantic
differences to remember:

- OpenCode `tokens.input` is **uncached** input and maps to span
  `cache_miss_tokens`; gateway/span `input_tokens` is the **provider total**
  prompt (cached + miss). Do not compare `tokens.input` to `input_tokens`.
- OpenCode's visible `tokens.output` and `tokens.reasoning` are counted
  separately; DeepSeek's `completion_tokens` is the combined total the gateway
  records as `output_tokens`, with
  `completion_tokens_details.reasoning_tokens` recorded separately.
- OpenCode token totals can be tokenizer-derived and therefore differ slightly
  from provider-reported totals. Exact lifetime equality is not expected.

## Deferred intentionally

Dynamic routing, model selection, QwenCloud routing, Codex escalation through
the gateway, fallback, retries beyond existing behavior, load balancing, and
cost-aware routing.

## Tests

`tests/test_gateway.py` covers pass-through, streaming pass-through, usage and
cache normalization, auth forwarding without logging, upstream 4xx/429/5xx
preservation, unreachable upstream, malformed upstream bodies, telemetry-failure
isolation, and no prompt/auth persistence.
