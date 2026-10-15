# Master D Acceptance V0.1

**Verdict: ACCEPTED_WITH_NOTES**
**Date: 2026-10-15**
**Review type:** independent, read-only (Phase B of MASTER K). This document does
not accept, merge, or promote Master D.

## Reviewed commits and worktree

| Ref | Value |
|---|---|
| Reviewed worktree | `~/.codex/worktrees/shared-knowledge-runtime-v0-1/ai-local-infra` |
| Branch | `feat/shared-knowledge-runtime-v0.1` (published, NOT merged) |
| Phase A base | `8088275998da5b3147a652d52af64b4efd4c7611` |
| Phase B commit | `bb9e57803b2eb8915cdcda8122146b223f7a1717` |
| Docs commit | `c156eff7016389d795b73b1e762a274e052b7817` (HEAD) |
| Interpreter used | Python 3.13.15 (`/Users/tovantest/.local/bin/python3.13`) |

## B1 — Scope (`git diff --name-status 8088275..HEAD`)

Exactly:

| Status | Path |
|---|---|
| M | `scripts/common/execution.py` |
| M | `scripts/common/result_cache.py` |
| A | `tests/test_knowledge_runtime_execution.py` |
| M | `docs/SHARED_KNOWLEDGE_RUNTIME.md` |

`scripts/common/knowledge_contract.py` and `scripts/common/active_work.py` **do not
appear** anywhere in the diff (verified by grep of `--name-only`). Scope matches
exactly.

## B2 — Hardening L1 (persisted evidence)

`scripts/common/result_cache.py`, `bounded_result`, lines **237–241**:

```python
evidence = result.get("knowledge_evidence")
if evidence is not None and not (isinstance(evidence, dict)
                                 and isinstance(evidence.get("bundle_fingerprint"), str)):
    # Present but malformed persisted evidence must never be reused and must not
    # crash the projection: refuse the bounded result so callers fail closed.
    return None
```

- Present-but-malformed `knowledge_evidence` (non-dict, or dict with a non-string
  `bundle_fingerprint`) → **clean `None`** (no raise). CONFIRMED.
- An **absent** key (`None`) is not malformed and leaves the "no evidence" path
  intact (`evidence['bundle_fingerprint']` emitted only when `evidence` is truthy,
  lines 248 and 266). CONFIRMED.
- Covering test: `tests/test_knowledge_runtime_execution.py`
  `HardeningTests.test_L1_malformed_persisted_knowledge_evidence_refuses` (line 317).

## B3 — Hardening L2 (dangling dependency)

`scripts/common/execution.py`, `_check_dependency_knowledge`, lines **825–829**
(`DEPENDENCY_KNOWLEDGE_UNKNOWN` raised at line **828**):

```python
dependency = by_id.get(task)
if dependency is None:
    # A dependency id absent from the plan cannot be validated: fail closed.
    raise active_work.WorkError('DEPENDENCY_KNOWLEDGE_UNKNOWN')
pending.extend(dependency['depends_on'])
```

A dependency id absent from the plan fails closed with `active_work.WorkError`, not
a `KeyError`. CONFIRMED. Covering test:
`HardeningTests.test_L2_dangling_dependency_id_refuses_not_crashes` (line 334).

## B4 — Hardening L3 (capability bound semantics)

`scripts/common/execution.py`, `_input_budget`, lines **847–857**. Decision,
documented inline (lines 847–850):

> Native adapter bounds only; no invented conversion from tokens to characters.
> A declared bound must be a positive integer: an absent key means "no declared
> bound", while 0, a negative, or a non-int is an invalid declaration (never an
> implicit "unlimited") and fails closed.

Implementation: `bound = capabilities.get(key); if bound is None: continue`
(line 854), then `if type(bound) is not int or bound <= 0 or size > bound: raise`
(line 856). CONFIRMED, and **documented in code** (not UNDOCUMENTED_DECISION).
Covering test: `HardeningTests.test_L3_zero_and_non_int_capability_bounds_refuse`
(line 341) — asserts 0 and a non-int (string) bound both refuse, and a positive
bound larger than the prompt does not.

## B5 — Behavioural claims (code + tests)

| Claim | Status | Evidence |
|---|---|---|
| Prelaunch re-validation raises `KNOWLEDGE_CHANGED_BEFORE_LAUNCH` and returns BLOCKED without consuming an attempt/budget | CONFIRMED | `execution.py:1224` (raise) + `execution.py:1916` (`prelaunch_check(request)`; on `WorkError` → `_finalize_node(...BLOCKED...)` before `attempt += 1`/`budget["used"] += 1`); test `test_K19_change_after_context_render_is_refused_at_launch_barrier` |
| Fail-closed, zero attempts, for missing required / revoked / superseded / malformed / redaction-insufficient / over-budget knowledge | CONFIRMED | `execution.py:777-838`; tests `test_K3` (missing), `test_K4` (revoked), `test_K5` (superseded), `test_K11_K12` (redaction), `test_K13` (over-budget); the shared `assert_blocked` asserts `attempts==[]` and `calls==0` |
| No fabricated success on reviewer FAIL / secondary-acceptance FALSE / provider unavailable | CONFIRMED | tests `test_reviewer_fail_is_never_workflow_success`, `test_provider_unavailable_has_no_fabricated_attempt`, `test_K25_false_secondary_implementer_acceptance_blocks_review` |
| Direct Mode (`provider=None`) issues zero model calls | CONFIRMED | `execution.py:768-774` (`_current_knowledge` returns plan metadata only when `provider is None`); `_run_tool_node` uses only the tool executor; test `test_K30_K31_K32_real_direct_mode_remains_tool_only` |
| Execution identity binds requirements identity, delivered identity, and manifest item fingerprints | CONFIRMED | `execution.py` builds/passes the hydrated document + context into `active_work.identity` (`active_work.py:59-121`, **unchanged**); `_attach_knowledge` (`execution.py:801-814`) writes the manifest item fingerprint and evidence; tests `test_K14_K16_K18...`, `test_K24...` |
| Registry authority cannot be replaced by a legacy `knowledge_provider` document | CONFIRMED | `execution.py:784` raises `KNOWLEDGE_AUTHORITY_CONFLICT`; test `test_registry_authority_cannot_be_replaced_by_legacy_injection` |
| Cache adoption and resume re-validate persisted knowledge evidence | CONFIRMED | `_check_knowledge_evidence` `execution.py:796-798`; resume `:988,:1083`, adoption `:1251`; tests `test_corrupt_cached_knowledge_evidence_refuses_no_replacement`, `test_corrupt_resume_evidence_refuses_no_repair_no_launch`, `test_K17_K27...` |
| Master B ordering: `active_work.py` unchanged by this diff | CONFIRMED | B1 scope; `active_work.py` absent from `8088275..HEAD` |

## B6 — Documentation correction

`docs/SHARED_KNOWLEDGE_RUNTIME.md` no longer claims a separate Phase B gate. The
replacement sentence, verbatim:

> Phase B integrates this runtime into the execution controller and the
> Master B durable lifecycle; the Phase B tests are offline-only and exercise no
> live provider or model.

## B7 — Focused tests (read-only, worktree, Python 3.13.15)

| Module | Result |
|---|---|
| test_knowledge_runtime | Ran 25 — OK |
| test_knowledge_runtime_execution | Ran 33 — OK |
| test_knowledge_contract | Ran 61 — OK |
| test_master_b | Ran 48 — OK |
| test_verified_handoff | Ran 28 — OK |
| test_execution_contract | Ran 35 — OK |

**230 passed / 0 failed.** Interpreter: **Python 3.13.15**. The sandbox
state-directory trap (`sqlite3.OperationalError: unable to open database file`)
was not encountered; no `LAI_STATE_DIR` override was required.

## B8 — Known historical failures

| Module | Worktree `c156eff` | Baseline `a8ff68a` |
|---|---|---|
| test_opencode_import | Ran 13 — FAILED (1 failure) | Ran 13 — FAILED (1 failure) |
| test_opencode_outcome | Ran 14 — FAILED (1 error) | Ran 14 — FAILED (1 error) |

Identical on both → HISTORICAL, not Master D regressions.

## Notes and findings

| ID | Severity | Finding | Reachability |
|---|---|---|---|
| N1 | LOW | `_input_budget` decides that `0`/negative/non-int capability bounds are invalid and fails closed. This is a design choice (no in-repo producer defines these keys), not a defect. | Only from an adapter that declares a `max_context_chars`/`max_context_bytes` of 0, a negative, or a non-int; not from any current adapter. |
| N2 | LOW | `bounded_result` refuses present-but-malformed persisted `knowledge_evidence` by returning `None`. | Only from crafted or corrupted durable state; normal runs always write well-formed evidence. |
| N3 | LOW | `_check_dependency_knowledge` fails closed on a dependency id absent from the plan. | Only from a malformed plan marked `valid: true` with a dangling `depends_on`; the controller blocks such a node earlier in normal runs. |

No HIGH or MEDIUM findings. No behavioural regression was observed.

## Not proven in this review

- **Live provider/model behaviour.** No provider or model call was made; all
  evidence is offline (fake adapters).
- **Full-suite status on this branch** was not re-run here (only the focused
  modules listed in B7). The prior full-suite run is not part of this verdict.
- **Home WSL portability** and **Master B independent acceptance** remain open
  gates and are not addressed by this document.

## Next decision required

Accept `feat/shared-knowledge-runtime-v0.1` (`c156eff`) as reviewed and proceed to
complete the remaining gates before any merge decision — or hold? (yes = accept the
verdict and continue to the Home WSL gate / no = hold)
