# Model routing

Logical roles for the teacher/student loop live in [`../models/roles.yaml`](../models/roles.yaml):
`student` (local Qwen), `teacher-cheap` (hosted DeepSeek), and `teacher-premium`
(hosted Codex). They describe intent and escalation order only. No routing, proxy,
automatic fallback, or secrets are implemented. See
[Teacher/student learning loop](TEACHER_STUDENT_LOOP.md).

| Workload | Default/candidate | Context | Notes |
|---|---|---:|---|
| Routine bounded coding (student) | `qwen3.6:27b-coding` | 32768 | Default developer profile. |
| Larger bounded coding | `qwen3.6:27b-coding` | 49152 | Use only when the task genuinely needs it. |
| Content Studio structured runtime | `qwen3.8:27b-q4_K_M` | 16384 candidate | Conservative existing provider value; representative benchmarking remains required. |
| General/secondary | `qwen3.6:27b-q4_K_M` | workload-specific | Strict GenerateIdea JSON was unreliable in observed runs. |

Raw inference and agent behavior are different measurements. The recorded direct coding benchmark does not prove Qwen Code tool orchestration quality. Likewise, Content Studio runtime validation does not make `qwen3.8` the developer-agent default.

Model lifecycle is `experimental`, `candidate`, `validated`, `default`, or `retired`. A role-specific validation does not imply universal validation. Stable profiles change through reviewed versioned edits, never automatic promotion from one favorable run.
