# Qwen Code local coding workflow

Evidence-backed tuning for running Qwen Code against local Ollama. This document
records measurements, the verified settings surface, the diagnosed bottlenecks,
and the recommended configuration. It is the local-ai-infra reference for **how**
the developer coding runtime performs; product scope stays in the owning repo.

No prompts, source, or proprietary text are stored here — only timings, token
counts, tool names, and settings.

## 1. Installed Qwen Code

- `qwen --version` reports `0.24.0`; the installed npm package
  `@qwen-code/qwen-code` `package.json` reports `0.23.4` at
  `/home/tama/.nvm/versions/node/v24.21.0/lib/node_modules/@qwen-code/qwen-code`.
- Treat the running binary as the source of truth and check the bundled offline
  docs before configuring: `bundled/qc-helper/docs/configuration/settings.md`
  plus `configuration/qwen-ignore.md`, `features/sub-agents.md`,
  `features/auto-mode.md`, and `features/memory.md`.

Verified settings/defaults (from the bundled docs of the installed version):

| Setting | Default | Purpose |
| --- | --- | --- |
| `model.reasoningEffort` | unset | `low/medium/high/xhigh/max`; mapped/clamped per provider via `/effort`. |
| `context.autoCompactThreshold` | internal `0.85` | Ceiling trigger for auto-compaction; replaced the removed `chatCompression.contextPercentageThreshold`. |
| `model.chatCompression.maxRecentFilesToRetain` | `5` | Recent files restored after compaction. |
| `model.chatCompression.maxRecentImagesToRetain` | `3` | Recent images restored after compaction. |
| `compactionModel` | `""` (main model) | Side model for compression; `/model --compaction`. |
| `context.fileFiltering.respectGitIgnore` | `true` | Search respects `.gitignore`. |
| `context.fileFiltering.respectQwenIgnore` | `true` | Search respects `.qwenignore` + custom ignore files. |
| `context.fileFiltering.customIgnoreFiles` | `[".agentignore", ".aiignore"]` | `.qwenignore` is always included. |
| `context.fileFiltering.enableFuzzySearch` | `true` | Fuzzy file matching. |
| `context.fileFiltering.enableRecursiveFileSearch` | `true` | `@`-prefix recursive crawl. |
| `tools.useRipgrep` | `true` | Ripgrep-backed content search. |
| `model.maxSubagentDepth` | `5` | Subagent nesting. |
| `agents.builtin.exploreModel` | `inherit` | Model used by the built-in Explore subagent. |
| `tools.approvalMode` | `auto` | `plan/default/auto-edit/auto/yolo`. |
| `model.maxToolCallsPerTurn` | `100` (adaptive) | Per-turn tool cap. |

Context files: Qwen loads `QWEN.md` hierarchically from the working directory and
its ancestors up to the project root. When a session starts at the repository
root, `ai-studio/QWEN.md` is **not** loaded. A root `QWEN.md` now covers this.

## 2. Direct Ollama benchmark

Model `qwen3.6:27b-coding`, Ollama `0.34.1`, RTX 4060 Ti 16 GB, warm model
(reloaded once), `temperature=0`, `num_predict` bounded.

| Run | Prompt tokens | Prompt tok/s | Output tokens | Generation tok/s | GPU util avg/max | VRAM peak |
| --- | --- | --- | --- | --- | --- | --- |
| small prompt | 40 | ~31 (cold) | 96 | 14.7 | 28% / 80% | 14.9 GB |
| ~2.6K-token prompt | 2599 | 284 | 16 | 14.1 | 78% / 100% | 14.9 GB |

- Cold model load: **26.8 s** (`load_duration`). An idle unload therefore adds a
  ~27 s penalty to the next request.
- VRAM peak ~14.9 GB of 16.4 GB while the model is ~18 GB, so it is partially
  CPU-offloaded (`ollama ps` earlier reported ~37% CPU / 63% GPU).
- Short-context generation ≈ 14 tok/s; prompt eval ≈ 284 tok/s warm.

Conclusion: raw inference works and uses the GPU, but the model does not fit in
VRAM. This is the single largest physical limit.

## 3. Session evidence

Two real attempts (Qwen Code session telemetry under `~/.qwen/projects/...`):

**Attempt #1 — `coding-routine` (context 32768).** 11 requests, input 218,650,
output 2,563 tokens; one Explore subagent (52.6 s) and one `grep_search` that took
52.7 s; compaction at 28,532 → 25,851 tokens; a later request reached 30,760 input
tokens; the final request returned 0 output tokens; the task ended with
`COMPRESSION_FAILED_EMPTY_SUMMARY` before any edit.

**Attempt #2 — `coding-large` (context 49152).** 8 requests, input 171,664,
output 6,804 tokens, 73,299 cached; tools totalled under ~0.5 s
(`grep_search` ×2, `read_file` ×6); two compactions (25,090 → 23,460 and
23,696 → 17,081); total model latency 27.7 min. Per-request durations 76–458 s
for 115–1,943 output tokens. Effective generation during these calls is only
≈5 tok/s (duration minus time-to-first-token over output tokens), versus 14 tok/s
at short context. `thoughts_token_count` was 0 for every request.

## 4. Diagnosis by layer

- **A. Raw model / hardware — dominant.** 18 GB model on 16 GB VRAM → partial CPU
  offload; ~5 tok/s at 20–27K context. Low GPU utilization during long turns is
  consistent with CPU-offloaded layers bottlenecking the pipeline, not idle time.
- **B. Qwen Code orchestration — minor.** Tool execution is sub-second. Wall time
  is model calls, not tool plumbing.
- **C. Context / compaction — significant.** 32K is too small once system prompt,
  tool schemas, agent docs, and a PRD slice are loaded; compaction ran repeatedly,
  and at least once failed with an empty summary.
- **D. Repo navigation / search — occasional.** Most `grep_search`/`glob` calls are
  tens of milliseconds, but a broad pattern took 52.7 s once (attempt #1).
- **E. Reasoning / tool behavior — significant and controllable only upstream.**
  Ollama's `/v1/messages` returns a `thinking` block by default even when no
  `thinking` parameter is sent, and ignores `budget_tokens` (verified: budget 64,
  16000, and unset produced the same thinking volume; a tiny `max_tokens` can be
  consumed entirely by thinking with no text output). Qwen Code's Anthropic adapter
  only sends `thinking` when `model.reasoningEffort` is set, and has no path that
  sends `{type:"disabled"}`. So reasoning is on by default and `reasoningEffort`
  is **not** a usable lever through this integration.

## 5. Root causes

- **Attempt #1:** context loss. 32K filled by broad navigation (agent docs + PRD +
  subagent), repeated compaction, a request at 30,760 tokens, then an empty
  compaction summary. Contributing factor: default-on thinking consumes output
  budget that compaction needs.
- **Attempt #2:** strongest hypothesis is slow generation amplified by large
  context. 49K prevented overflow but each turn re-sent ~20–27K tokens at ~5 tok/s
  with default-on thinking; 8 turns ⇒ ~28 min. Not a tool loop, not excessive
  tool time, not raw CPU idle.

## 6. Implemented changes

| Change | Hypothesis |
| --- | --- |
| Root `QWEN.md` (auto-loaded operating rules) | Failed sessions ran from the repo root, so `ai-studio/QWEN.md` was never loaded and the model explored broadly. Concise rules push exact paths, bounded reads, no full PRD, no subagents for bounded work, and edit-after-minimum-evidence. |
| Root `.qwenignore` | Explicitly excludes build output and local-ai-infra runtime/generated output from file search, glob, and `@`-completion, so large trees are not crawled. |

Both are reversible documentation/config changes; neither touches product source
or model/profile semantics.

## 7. Recommended configuration

- **Routine mechanical coding:** `coding-routine` (32768) only for single-file or
  few-file edits where the task and paths are known. Keep the live context small
  (roughly < 20K) to stay near 14 tok/s; expect no subagents. If the model needs a
  broad scan, it is the wrong profile.
- **Bounded feature coding:** `coding-large` (49152), but split into vertical
  slices, specify exact files, give one bounded doc section, and avoid subagents.
  Validate per slice.

Do not raise context beyond 49152 on this hardware — it worsens VRAM pressure and
generation speed without addressing the physical limit.

## 8. Retest procedure (bounded, later milestone)

Replay the same bounded Storyboard-contract task and compare to attempt #2:

1. `./scripts/ai-profile status` and `./scripts/ai-profile verify` — confirm the
   active profile and healthy Ollama.
2. Start a fresh Qwen session from the repository root in Ask Permission mode.
3. Prompt with exact target files and a bounded PRD section; request no subagents.
4. Record per session: duration, request count, per-request duration/TTFT/output
   tokens, compaction events, tool durations, and time to first edit.
5. Compare against attempt #2 baseline (8 requests, ~27.7 min latency, first edit
   beyond 21 min, 2 compactions).

Success criteria: a meaningful edit within a few minutes, no context overflow, no
compaction failure, and materially fewer model requests.

## 9. Deferred experiments (do not implement in this milestone)

- **Compaction tuning.** Hypothesis: a lower `context.autoCompactThreshold`
  (e.g. 0.70–0.75) leaves headroom for the summary call and avoids empty-summary
  failures at 32K. Requires deciding whether profiles should manage it; not applied.
- **Reasoning off.** Highest-value latency lever: Ollama defaults thinking on and
  ignores `budget_tokens`. Disabling it needs a supported path (Ollama model
  `think=false`, an Anthropic `{type:"disabled"}` from Qwen Code, or a request
  rewrite). Do not add a request proxy or a new model now.
- **Compaction model.** `compactionModel` could offload summarization, but that
  needs another model installed; deferred.
- **Smaller coding model.** A model that fits fully in 16 GB VRAM (smaller quant or
  a ~7–14B coder) should be benchmarked next; it would likely beat the current
  partial-offload throughput. Not installed here.

## 10. Hardware / WSL

- VRAM (16 GB) is the binding constraint: the 18 GB model spills to host RAM.
- Host RAM 32 GB, WSL previously ~15–16 GB. The CPU-resident model slice plus
  runtime fits, so WSL RAM is not the primary cause. Raising WSL to 24 GB may
  reduce paging but will not fit the model in VRAM and should not be expected to
  deliver a large speedup.
- Do not edit `.wslconfig` automatically.

## 11. Learning observations

Both are recorded as `observation` learning candidates (never promoted). See
`datasets/candidates/` output from `scripts/record-learning-candidate`:

- Attempt #1 — category `context_loss`, contributing observation: empty compaction
  summary (`COMPRESSION_FAILED_EMPTY_SUMMARY`).
- Attempt #2 — category `unknown` (evidence points to model/hardware-bound
  generation latency, which the taxonomy has no dedicated category for). A lesson
  becomes `validated` only after the bounded replay above shows measurable
  improvement.
