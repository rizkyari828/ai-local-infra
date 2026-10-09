# Model routing

Logical roles for the teacher/student loop live in [`../models/roles.yaml`](../models/roles.yaml):
`student` (local Qwen), `teacher-cheap` (hosted DeepSeek), and `teacher-premium`
(hosted Codex). They describe intent and escalation order. A single bounded,
opt-in Qwen -> DeepSeek escalation is implemented (see
[Escalation](ESCALATION.md)); Codex, multi-hop routing, scoring, and secrets in
configuration remain unimplemented. No API keys are stored.

| Workload | Default/candidate | Context | Notes |
|---|---|---:|---|
| Routine bounded coding (student) | `qwen3.6:27b-coding` | 32768 | Default developer profile. |
| Larger bounded coding | `qwen3.6:27b-coding` | 49152 | Use only when the task genuinely needs it. |
| Content Studio structured runtime | `qwen3.8:27b-q4_K_M` | 16384 candidate | Conservative existing provider value; representative benchmarking remains required. |
| General/secondary | `qwen3.6:27b-q4_K_M` | workload-specific | Strict GenerateIdea JSON was unreliable in observed runs. |

Raw inference and agent behavior are different measurements. The recorded direct coding benchmark does not prove Qwen Code tool orchestration quality. Likewise, Content Studio runtime validation does not make `qwen3.8` the developer-agent default.

Model lifecycle is `experimental`, `candidate`, `validated`, `default`, or `retired`. A role-specific validation does not imply universal validation. Stable profiles change through reviewed versioned edits, never automatic promotion from one favorable run.

## Adaptive task-class routing (V0.1)

A deterministic, offline recommendation layer answers "what is the cheapest
sufficient model/reasoning tier for this task?" It never calls a model, never
changes a user-selected model, and never stores quotas or secrets. Policy lives
in [`../config/model-routing.yaml`](../config/model-routing.yaml); logic in
`scripts/common/routing.py`; CLI in `./scripts/route`.

Task classes (conservative; an uncertain task falls through to `NORMAL`, never
silently to the cheapest class):

| Class | Examples | Tier | Effort (role) |
|---|---|---|---|
| `MECHANICAL` | search, rename, format, config key, rerun tests, git | `FAST` | low (`student`) |
| `NORMAL` | bounded implementation, bugfix, straightforward refactor | `BALANCED` | medium (`student`) |
| `CORRECTNESS_SENSITIVE` | architecture, concurrency, security, persistence, cross-machine | `STRONG` | high (`teacher-cheap`) |
| `CRITICAL` | unresolved correctness-sensitive or security-sensitive design | `MAX` | max (`teacher-premium`) |

Tiers are symbolic and reference the existing roles in
[`../models/roles.yaml`](../models/roles.yaml), which map role -> provider/model.
Provider/model names are never hardcoded in routing, so mappings can differ per
machine/surface. Every surface (OpenCode, Codex CLI/VS Code/Desktop) is
`RECOMMEND_ONLY` in V0.1: the router recommends, the human or agent applies it.

```bash
./scripts/route --task "design a cross-machine config resolver"
./scripts/route --task "rename a config key" --json
./scripts/route --task "anything" --class critical         # explicit override wins
./scripts/route --task "rename" --effort high              # user floor, never downgraded
```

Escalation is bounded and evidence-based: a small number of same-tier retries
(`max_retries`), then a single step up the class chain
`MECHANICAL -> NORMAL -> CORRECTNESS_SENSITIVE -> CRITICAL`. `CRITICAL` stops and
reports rather than retrying. Success always stops. This layers on top of (and
does not duplicate) the failure-category escalation in
[`ESCALATION.md`](ESCALATION.md) and the retry ceilings in
[`../config/limits.yaml`](../config/limits.yaml).

## Quota-saving principles

- Use stronger models only when task complexity justifies them.
- Prefer fresh, bounded task context over carrying a huge session forever.
- Use targeted retrieval (and Graphify) before broad reads.
- Stop when acceptance criteria pass; do not perform unrelated cleanup.
- Escalate based on evidence (ambiguity, correctness risk, repeated failure),
  not instinct.
- Never reduce reasoning below a safe floor for security, concurrency,
  destructive operations, architecture contracts, or data integrity.
- Headroom context compression is independent of routing; never add the two
  savings together.

Expected quota impact is qualitative only (no invented percentages): **HIGH** for
mechanical work (avoids high reasoning and large context), **MEDIUM** for normal
bounded work (bounded escalation, early stop), **LOW** for complex work (stronger
models are the point; savings come only from smaller retrieval scope). Actual
token measurement belongs to later telemetry, not V0.1.

