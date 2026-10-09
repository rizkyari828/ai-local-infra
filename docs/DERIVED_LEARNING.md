# Derived learning

Derived learning reduces repeated reasoning and repeated investigation by turning
reviewed observations into small, portable, Git-shared knowledge that OpenCode and
every Codex surface can reuse — without sharing raw chats, sessions, credentials,
or machine runtime databases.

```text
OpenCode / Codex CLI / VS Code / Desktop
        |  record bounded outcome (local)
        v
local evidence  ($LAI_STATE_DIR/learning/evidence.db)   machine-local, never committed
        |  review / distill / promote (human, explicit)
        v
shared knowledge  (knowledge/* in Git)                  portable, tool-neutral
        +--> usable by OpenCode   +--> usable by Codex   +--> shared Mac <-> Home WSL via Git
```

## Local evidence vs shared knowledge

| | Local evidence | Shared knowledge |
|---|---|---|
| Where | `$LAI_STATE_DIR/learning/evidence.db` | `knowledge/` in the repository |
| Committed | never | yes |
| Synced between machines | never (SQLite is machine-local) | yes, through normal Git |
| Contains | bounded structured metadata | concise lessons, failures, seeds, defaults |
| Contains prompts/secrets | never | never |

`$LAI_STATE_DIR` follows the existing state convention (default
`~/.local/state/local-ai-infra`). The DB path is rejected if it resolves inside
the repository. Mac and Home WSL keep **independent** databases; they share only
`knowledge/*`.

## Privacy and storage boundary

The ledger stores only: observation id, timestamp, task family/class, surface,
provider/model/native variant, normalized effort, initial/final tier and effort,
outcome, escalated flag, failure category, source, confidence, and a few bounded
optionals (duration category, attempt count, lesson ids, routing decision id).
It never stores raw prompts, full responses, transcripts, terminal logs, tokens,
API keys, cookies, Codex/OpenCode session DBs, embeddings, or binary blobs. Text
fields are length-bounded and secret-scanned; unknown fields are rejected.

## Historical seed semantics

`knowledge/routing/historical-seed.yaml` holds **observed history**. A recorded
success does **not** prove a lower tier would have failed, and a single sample
never creates a permanent floor. Seed records capture used tier/effort, outcome,
escalation, failure category, and confidence. The seed imports idempotently into
the local ledger using stable observation ids (`./scripts/learn seed`; re-running
imports zero duplicates). Seed `notes` are documentation and are not stored.

## Failure categories

Operational evidence uses `REASONING_INSUFFICIENT`, `IMPLEMENTATION_ERROR`,
`INFRA_FAILURE`, `PROVIDER_503`, `RATE_LIMIT`, `TIMEOUT`, `AUTH_FAILURE`,
`ACCOUNT_MODEL_UNSUPPORTED`, `SAFETY_GUARD`, `STALE_SESSION`, `CONFIG_DRIFT`,
`UNKNOWN_FAILURE`. Only `REASONING_INSUFFICIENT` may influence intelligence
escalation: provider 503, rate limits, timeouts, auth failures, account/model
unsupported, safety guards, and stale sessions are **not** model weakness. See
`knowledge/failures/failures.yaml`.

## Promotion workflow

`./scripts/learn candidates` suggests only. `./scripts/learn promote ...` is
dry-run by default and mutates shared knowledge only with `--apply`. Promotion
requires a reviewer and explicit human acceptance, rejects secrets, raw/dump-like
text, and over-long text, and enforces the learned-default thresholds
(`min_samples`, `min_success_ratio`) so a single observation cannot become a
permanent rule. There is **no** automatic promotion and **no** automatic Git
mutation.

## Learned defaults and routing influence

`knowledge/routing/learned-defaults.yaml` starts empty. An approved default may
lower a task family's initial tier/effort, but never below the classification
**safety floor** (`MECHANICAL`/`NORMAL` → `FAST`, `CORRECTNESS_SENSITIVE` →
`STRONG`, `CRITICAL` → `MAX`) and never below an explicit user override. When a
default conflicts with the safety floor, the floor wins. Historical observations
alone are advisory and never change a floor.

`./scripts/route` prints `HISTORICAL EVIDENCE`, `RELEVANT LESSONS`, and
`LEARNED DEFAULT` (none/available/applied) alongside the recommendation.

## Commands

```bash
./scripts/learn status                 # DB path, schema, counts (no raw dump)
./scripts/learn seed                   # idempotent import of shared historical seed
./scripts/learn record --task-family F --class NORMAL --tier BALANCED --effort MEDIUM --outcome SUCCESS
./scripts/learn query --task-family configuration_resolution
./scripts/learn query --failure-category PROVIDER_503
./scripts/learn candidates
./scripts/learn promote --family F --tier FAST --effort low --samples 5 --success-ratio 0.9 \
                        --reviewer me --human-accepted            # dry-run; add --apply to write
./scripts/learn reset --confirm        # delete local DB only; shared knowledge untouched
```

All of these are deterministic and local: no LLM/provider call, no inference, no
downloads. Recording happens after meaningful tasks, not for routine reads.

## Cross-machine sync and reset/backup

Only `knowledge/*` travels through Git. To reset the local DB without losing
shared knowledge, `./scripts/learn reset --confirm` (or delete the file). To back
up local evidence, copy `$LAI_STATE_DIR/learning/evidence.db` while no command is
running. A corrupt/unreadable DB is reported clearly and never silently recreated.
