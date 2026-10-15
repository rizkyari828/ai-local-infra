# Master B Acceptance V0.1

## 1. Verdict, date, reviewed commit

**Verdict: ACCEPTED_WITH_NOTES** (unchanged — neither review found a HIGH finding)
**Date: 2026-10-15**
**Reviewed commit: `965fad53839cf0da8c42839ffd00838d2de2f4d5`** (`feat/master-b-durable-execution-v0.1`
branch tip, reviewed **at that commit, not at `main`**).

> **Two independent reviews agreed.** This path was written twice by two independent
> MASTER B acceptance runs (commits `8e0f5cf`, 234 lines, and `a5c96bb`, 155 lines).
> Both reached **ACCEPTED_WITH_NOTES** and **neither found a HIGH finding**. This file
> is the consolidated record: it keeps the strongest evidence of both, states where
> they differ, and records the collision rather than erasing it. History was **not**
> rewritten — both commits remain. The reconciliation note is section 9.

Legend for numbers: **[MQ]** = measured in this consolidation milestone;
**CARRIED_OVER (run-8e0f5cf / run-a5c96bb)** = measured by that review run, not re-run here.

---

## 2. Reviewed commits and geometry

| Ref | Value |
|---|---|
| Repository | `~/Developer/Back-End/ai-local-infra` |
| Reviewed branch | `feat/master-b-durable-execution-v0.1` (published, **NOT** merged by either review or by this record) |
| Reviewed commit | `965fad53839cf0da8c42839ffd00838d2de2f4d5` |
| Master B base | `7b6b73b` (= `feat/shared-knowledge-contract-v0.1` tip — frozen-contract ancestor) |
| `main` tip at review time | `652b5ff` |
| `origin/main` before consolidation | `652b5ff` |
| Local `main` at consolidation start | `a5c96bb` (carries `8e0f5cf` + `a5c96bb`, both local-only until this milestone) |
| Interpreter used this milestone | Python 3.13.15 (`~/.local/bin/python3.13`) **[MQ]**; run-a5c96bb also measured Python 3.9.6 |

### 2.1 Master B's own commits (verified, not assumed)

`git log --oneline 7b6b73b..965fad5` returns exactly three commits; per-file
`git diff --numstat 7b6b73b..965fad5` **[MQ]**:

| Commit | Subject | Own contribution (added/deleted) |
|---|---|---|
| `b8c0a93` | feat: add durable execution coordination and conservative reuse | `scripts/common/active_work.py` new +451/0; `execution.py` +206/−41; `orchestration_store.py` +33/−7; `orchestrate_cli.py` +4/−1 |
| `9ca7bed` | test: cover master b races mutation recovery and interruption | `tests/test_master_b.py` new +540/0; touch-ups in **6** other test modules +14/−5 (`test_incremental_reuse`, `test_knowledge_contract`, `test_operational_store`, `test_stabilization_fix_pass2/3/4`) |
| `965fad5` | docs: describe master b durability and deferred engine boundaries | `docs/MASTER_B.md` +120/0; `docs/ARCHITECTURE.md` +4/0 |

Own-scope diff total: **13 files, +1372/−54** **[MQ]**.

*Reconciliation correction:* run-8e0f5cf described `9ca7bed` as "6-line touch-ups in 5
other test modules". The measured value is **6 other test modules, +14/−5**; the
full-branch file count (13) only reconciles with 4 + 7 + 2 files. The evidence supports
this record's number.

### 2.2 Geometry evidence (re-measured this milestone)

```text
$ git rev-list --left-right --count main...feat/master-b-durable-execution-v0.1
15	0

$ git merge-base --is-ancestor feat/master-b-durable-execution-v0.1 main ; echo $?
0

$ git merge-base --is-ancestor main feat/master-b-durable-execution-v0.1 ; echo $?
1
```

- main-only = 15, branch-only = 0. Master B is a **strict ancestor** of `main`: it
  contains no commit absent from `main`, so a fast-forward would be trivially possible
  and carries no conflict risk. That merge is a **separate human decision** and is not
  made or implied here.
- **Diff direction is understood.** `git diff main..feat/master-b-durable-execution-v0.1`
  lists `main`'s later commits in reverse; it is **not** Master B's work. Master B's work
  is the 13-file, +1372/−54 diff from `7b6b73b` in §2.1.

---

## 3. Scope

### 3.1 Under review

| Path | Role |
|---|---|
| `scripts/common/active_work.py` @ `965fad5` | primary subject — identity, claim/acquire, coordination, launch, adoption, recovery |
| `tests/test_master_b.py` @ `965fad5` | 48 tests, primary evidence |
| `docs/MASTER_B.md` @ `965fad5` | the claims document |

### 3.2 Read for context and ordering only — NOT accepted or rejected here

`scripts/common/execution.py`, `scripts/common/result_cache.py`,
`scripts/common/lifecycle.py`, `scripts/common/orchestration_store.py`, and
`scripts/common/knowledge_contract.py` (frozen).

### 3.3 Scope drift — stated loudly

**`main` has advanced past the reviewed commit — including on the reviewed subject
itself.** `git diff --numstat 965fad5..main` **[MQ]**:

| Path | added | deleted | `main`-only commits after Master B |
|---|---|---|---|
| `scripts/common/active_work.py` | 60 | 10 | `912060f`, `990f037`, `a8ff68a` |
| `scripts/common/execution.py` | 532 | 59 | `912060f`, `0244b6c`, `990f037`, `a8ff68a` |
| `scripts/common/result_cache.py` | 20 | 3 | same range |
| `scripts/common/orchestration_store.py` | 10 | 2 | same range |
| `tests/test_master_b.py` | — | — | **byte-identical** between `965fad5` and `main` (diff exit 0) **[MQ]** |
| `docs/MASTER_B.md` | — | — | **byte-identical** between `965fad5` and `main` (diff exit 0) **[MQ]** |

The `main`-side additions are cross-provider / verified-result hardening
(`execution_requirement` + `allowed_bindings`, `result_contract` / `verified_result`
proof checks, and an `acceptance_check` parameter threaded through `accepted()`).

**Therefore: this acceptance covers Master B at `965fad5` only.** It does **not** cover
`main`'s current `active_work.py` / `execution.py` / `result_cache.py` /
`orchestration_store.py`, which are `Master D` and later work. Citing this verdict as
"Master B plus the current controller is accepted" is an overread. Both reviews stated
this limitation; it is the single most important caveat in this record.

---

## 4. Independent verification — V1..V10

Line numbers are against the **reviewed commit `965fad5`**. Evidence was read from the
branch, not from `main`.

| # | Item | Verdict | Evidence (file:line @ 965fad5) | Test |
|---|---|---|---|---|
| V1 | Identity binding — requirements identity, delivered validated knowledge identity, and context manifest item fingerprints all participate; a change in content, authority or applicability changes the key | CONFIRMED | `active_work.py:37` `identity()`; `:70` `requirements = kc.agent_semantic_inputs(document)['requirements']`; `:71-72` `permissions`; `:73-79` `kc.task_identity(objective_identity=result_cache.task_semantic_fingerprint(node), acceptance_identity=digest(...), permissions_identity=digest(...), requirements_identity=digest(requirements))`; `:89` `'context': result_cache.context_identity(manifest)`; `:90` `'dependencies': [d['result_fingerprint'] …]`; `:83-85` repo id + `workspace_instance` + `workspace_scope`; `:101-102` `kc.active_work_equivalence_input(task=task, knowledge_identity=selected['delivered_identity'], …)`; `:68` refuses `DEPENDENCY_IDENTITY_UNAVAILABLE` when any dependency lacks a `result_fingerprint`; `:55-66` `kc.select()` must be `READY` and each delivered entry is secret-checked (`KNOWLEDGE_DELIVERY_UNSAFE`) before the key is built | `test_L2_L3_L4_content_scope_and_unselected_changes` (:333), `test_L5_required_revoked_refuses_before_reuse` (:341), `test_C5_dependency_result_identity_changes_key` (:369), `test_dependency_binding_swap_changes_work_identity` (:387), `test_policy_and_capability_not_equivalent` (:228), `test_task_ids_and_titles_are_not_work_identity` (:356), `test_validated_knowledge_snapshot_matches_delivered_worker_content` (:415) |
| V2 | Claim / acquire semantics — claim exclusive per identity, second owner cannot launch a duplicate, timeout returns `WAIT_TIMEOUT` rather than launching; ordering claim -> launch -> adopt | CONFIRMED | `active_work.py:119-126` `atomic()` `BEGIN IMMEDIATE` … `commit()`/`rollback()`; `acquire()` returns `'RESULT'` (:172-176), `'BUSY'` (:181-183), `'WAIT'` (:184-185), else inserts a new generation `'CLAIMED'` (:188-192, 13-column `INSERT OR REPLACE`); `:195-199` `launch()` sets `RUNNING` + `launch_intent=1`; ownership ordering: `coordinate` -> `execution.py:1397` `durable_store.launch(claim)` -> `active_work.py:436-437` `persist_result`/`complete`; timeout: `coordinate:397-399` emits `WAIT_TIMEOUT` and returns `{'status':'BLOCKED','failure_category':'WAIT_TIMEOUT'}` without calling the worker | `test_C1_two_requests_one_launch` (:117), `test_C2_C3_ten_requests_nine_waiters_one_launch` (:120), `test_timeout_never_launches_duplicate` (:172) — asserts `worker.calls == []` **and** `WAIT_TIMEOUT`, `test_completion_between_lookup_and_acquire_does_not_launch_again` (:197) |
| V3 | Mutation exclusion — MUTATING cannot run with concurrent equivalent work; READ_ONLY may join rather than block | CONFIRMED | `active_work.py:176-183` conflict scan: `… WHERE workspace=? AND state IN ('CLAIMED','RUNNING','OUTCOME_UNCERTAIN') AND (mode='MUTATING' OR ?='MUTATING')` — a MUTATING requester conflicts with any busy work; a READ_ONLY requester conflicts with busy MUTATING only; same-identity READ_ONLY joins as a waiter (`:184-185`, `:366-399`); `:43-50` refuses `MUTATION_SCOPE_UNPROVEN` for a real (non-`SIMULATED`) mutation without `mutation_scope:'workspace'`, and `UNSCOPED_EXTERNAL_MUTATION` for a `://` target or a path outside the owned root | `test_M1_M2_workspace_exclusion_for_equivalent_and_different_mutations` (:188), `test_reader_writer_exclusion` (:223), `test_real_mutation_requires_explicit_supported_resource_scope` (:215), `test_external_mutation_is_refused_without_resource_scope` (:513), `test_mutation_reuse_does_not_bypass_other_workspace_writer` (:481) |
| V4 | In-flight deduplication / single-flight | CONFIRMED | `active_work.py:118-126` `BEGIN IMMEDIATE`; `acquire()` runs `_recover`, claim read, conflict query and the insert inside one short write transaction (`:164-193`), so two concurrent callers serialise and the loser observes `WAIT`/`BUSY`; no worker executes inside a transaction — `execute()` runs in `coordinate:427-429` via `Lease`, outside `atomic()` | `test_C1…` (:117), `test_C2_C3…` (:120) — asserts exactly `len(worker.calls) == 1` with 9 waiters across 10 threads |
| V5 | Lease and fencing — a stale worker cannot publish over a newer claim; behaviour when the lease holder dies | CONFIRMED | `active_work.py:133-138` `_owned()` requires the live row's `generation`, `token`, and `state in ('CLAIMED','RUNNING')`, else `STALE_GENERATION`; enforced at launch `:195-199`, heartbeat `:201-205`, publish `:215`, completion `:230`, fail `:242`; `complete()` re-checks generation/token and refuses a missing result `:223-236`; `Lease` heartbeats every 5 s on its own connection `:329-353`; `_recover` skips a live lease-holder `:278` and on death bumps generation (`:280-283`), moving a launched mutation to `OUTCOME_UNCERTAIN` and a pre-launch claim to `FAILED_SAFE_TO_RETRY`; a killed owner's `LAUNCHED` attempt becomes `UNKNOWN_OUTCOME`/`OWNER_LOST` (`:283-288`) | `test_C7_C8_stale_readonly_reclaimed_late_owner_fenced` (:248, raises `STALE_GENERATION`), `test_claimed_unlaunched_owner_vanished` (:256), `test_M4_M5_launched_mutation_expiry_never_replays` (:260), `test_live_owner_cannot_be_reclaimed_only_on_lease_expiry` (:284), `test_M7_M8_reconciliation_fences_old_mutation_without_replay` (:289) |
| V6 | Conservative reuse — a durable outcome is reused only when the current input identity still matches the persisted one; reuse refused when delivered knowledge evidence differs | CONFIRMED | `active_work.py:140-148` `accepted()` requires a persisted row whose `version` matches **and** `json.loads(row['identity']) == identity` (whole-identity equality) and `status=='SUCCESS'`/`acceptance=='PASSED'`; `:149-151` READ_ONLY requires a reusable freshness policy; `:152-155` adapter-reported `actual_model`/`actual_effort` must equal the resolved binding (never inferred); `:156-159` MUTATING additionally requires `postcondition_verified` **and** a currently-true caller postcondition; delivered-knowledge change changes the key (V1), so reuse is refused | `test_L1_legacy_result_cannot_match_current_version` (:327), `test_L2_L3_L4…` (:333), `test_L5…` (:341), `test_L6_policy_change_blocks_reuse` (:346), `test_resume_revalidates_current_knowledge` (:351), `test_volatile_observation_never_reuses_completed_result` (:395), `test_reported_effort_mismatch_is_not_reused` (:504), `test_verified_mutation_reuse_rechecks_current_postcondition` (:461), `test_force_refresh_waiter_adopts_new_generation_not_old_cached_result` (:489) |
| V7 | Recovery and interruption — crash/interruption leaves recoverable state; a resumed run does not re-launch succeeded work nor adopt a stale result | CONFIRMED | `active_work.py:253-288` `_recover()`, invoked at the head of every `acquire()` inside the same write transaction (`:169`), also callable via `recover()` `:290`; interrupted `WAIT_STARTED` events with a dead pid are closed (`runs`->`CANCELLED`/`WAITER_LOST`, `tasks`->`BLOCKED`, a `WAIT_TIMEOUT` event) without touching the owner or inventing an attempt (`:257-266`); a claim whose generation+token already has an `accepted_work` row is promoted to `COMPLETED` instead of re-launched (`:272-275`); `fail()` returns early when a persisted result exists (`:245-247`, "recovery must finish publication, never discard accepted evidence"); adoption always re-runs `refresh()` and refuses on change (`coordinate:376-381`, `:411-417`); `execution.py:691` refuses `REFUSE_RESUME_CURRENT_IDENTITY_CHANGED` and `:674-676` `REFUSE_RESUME_UNCERTAIN_MUTATION` | `test_P1_result_persisted_completion_crash_reuses_without_worker` (:308), `test_P2_completion_requires_durable_result` (:303), `test_C6_M3_prelaunch_failure_allows_new_owner` (:243), `test_M4_M5…` (:260), `test_M6_quota_failure_is_uncertain` (:266), `test_R7_disappeared_waiter_does_not_own_attempt_or_change_owner` (:274), `test_unverified_mutation_is_not_adopted_on_resume` (:519) |
| V8 | Frozen contract untouched by Master B | CONFIRMED | `git diff --quiet 7b6b73b 965fad5 -- scripts/common/knowledge_contract.py` -> exit **0** (byte-identical) **[MQ]**; `git log --oneline 7b6b73b..965fad5 -- scripts/common/knowledge_contract.py` -> **empty** **[MQ]**. **Newer-unique finding retained:** Master B *did* modify `tests/test_knowledge_contract.py` — it renamed `test_no_production_module_consumes_the_contract_yet` to `test_no_shared_knowledge_runtime_rollout` and removed `execution.py` from the prohibited-consumers list (`git diff 7b6b73b..965fad5 -- tests/test_knowledge_contract.py` **[MQ]**). That is legitimate: Master B consumes caller-supplied contract inputs while retrieval/skill rollout stays deferred. The contract module itself is unmodified | git evidence, not a test (see T3/O-G1) |
| V9 | No fabricated success — the code cannot record terminal success for reviewer FAIL, secondary-acceptance FALSE, provider unavailable, or a publication crash | CONFIRMED | Acceptance is independent of exit status `execution.py:114-157`; `accepted = process_ok and acceptance_state == ACCEPTANCE_PASSED` `execution.py:1475`; FAILED/UNVERIFIED finalize to FAILURE/UNVERIFIED `execution.py:1484-1509`; `acceptance` is `PASSED` only for status SUCCESS `execution.py:1706-1710`; `persist_result` rejects anything not `SUCCESS`+`PASSED` (`RESULT_NOT_ACCEPTED`) `active_work.py:209-213`, plus `RESULT_IDENTITY_MISMATCH` and `POSTCONDITION_UNVERIFIED`; `accepted()` applies the same gate `:144-148`; `complete` requires a durable result `:227-235`; a publication crash cannot fabricate success (`coordinate:434-441`). Provider-unavailable/rate-limit -> FAILURE, never persisted. The reviewer-FAIL / secondary-FALSE classifier is covered by `tests/test_execution_contract.py:183-186` (Ran 35 OK, CARRIED_OVER run-a5c96bb), not directly in `test_master_b.py` (T3/N-a) | `test_M6_quota_failure_is_uncertain` (:266), `test_dogfood_quota_interruption_does_not_fabricate_completion` (:526), `test_P1…` (:308), `test_P2…` (:303), `test_mutation_result_requires_current_postcondition` (:318) |
| V10 | Zero provider calls in tests | CONFIRMED | All tests use in-process stubs: `Worker(Stub)` `tests/test_master_b.py:19-39` extending `execution.Backend`, whose `execute()` only records and returns a dict; `run_work` passes `backend=worker` (`:57`); the only other backend used is `execution.FakeBackend()` (`:181`, patched through `orchestrate_cli._local_health`). `grep -n 'requests\|urllib\|http\|socket\|subprocess\|urlopen' tests/test_master_b.py` -> no matches **[MQ]**; the module completes in ~25 s with no network. No live call was made by either review or by this milestone | whole module, 48/48 offline |

---

## 5. Test-quality assessment — T1..T5

**MODULE_RESULT (re-measured this milestone, Python 3.13.15)** **[MQ]:**

- at branch tip `965fad5`: `Ran 48 tests in 25.026s — OK`
- against the `main` working tree (`tests/test_master_b.py` is byte-identical, but the
  controller under it is `main`'s): `Ran 48 tests in 26.491s — OK`

**COUNT:** 48 (`grep -c '    def test' tests/test_master_b.py` = 48) **[MQ]**.
**INTERPRETER:** Python 3.13.15 this milestone; run-a5c96bb additionally measured the
module OK on Python 3.9.6 (CARRIED_OVER).
**STATE_DIR_TRAP_ENCOUNTERED:** no — every run used an isolated `LAI_STATE_DIR` +
`LAI_MACHINE=work-mac` (`tests/test_master_b.py:49`); the real state directory was never
written to.

Supporting modules touched by Master B (`test_incremental_reuse`,
`test_knowledge_contract`, `test_operational_store`, `test_stabilization_fix_pass2/3/4`):
CARRIED_OVER (run-a5c96bb) — 177 tests; one failure under Python 3.9.6
(`test_stabilization_fix_pass2.HumanAcceptanceTests.test_cli_records_acceptance_for_a_real_run`,
launcher requires Python >= 3.11), classified **ENVIRONMENT_INTERPRETER**, all 177 OK
under Python 3.13.15. Plus `tests.test_execution_contract` Ran 35 OK (CARRIED_OVER).

| # | Question | Assessment |
|---|---|---|
| T1 | Real controller, or helper-only? | **Real controller.** `Fixture.run_work` (`:57`) calls `execution.execute_plan(...)` with `confirm=True`, a real `state_dir` and a `Worker` backend; assertions inspect the real durable store (`self.store().run_attempts(r['run_id'])`, `accepted_work`) and the real request payloads captured by the fake worker (`len(worker.calls)`; `:401`, `:415`). Two tests inject a crash at a real boundary with `unittest.mock.patch` (`:310` `patch.object(aw.WorkStore,'complete',side_effect=OSError('publication crash'))`; `:407`/`:422` `patch.object(aw,'identity',side_effect=…)` for a mid-flight change) — the correct way to reach an otherwise unreachable window. `test_completion_between_lookup_and_acquire…` (`:197`) drives `active_work.coordinate` directly. Identity-component tests (`:356`, `:363`, `:369`, `:387`, `:228`) call `aw.identity` as units; not controller evidence on their own, but the same properties are exercised end-to-end by L2..L6 |
| T2 | Non-vacuous assertions? | **Yes.** `:108` asserts `len(worker.calls) == 1` and `:109` asserts `count - 1` waiters — both fail immediately if single-flight is removed (10 threads would call the worker 10 times). `:113` asserts `1 if i == 0 else 0` attempts per run — fails if a waiter fabricated an attempt. `test_timeout…:172` asserts `worker.calls == []` plus `WAIT_TIMEOUT`. `test_C7_C8…:248` asserts a `STALE_GENERATION` raise. `test_M4_M5…:260` asserts `OUTCOME_UNCERTAIN` with continued `BUSY` exclusion |
| T3 | Coverage of V1..V9, and the gaps | Dedicated tests for V1..V7, V9. **Gaps (union of both reviews):** (a) V9 reviewer-FAIL / secondary-FALSE path has no dedicated test inside `test_master_b.py` (classifier covered by `test_execution_contract.py:183-186`); (b) recovery marking a `LAUNCHED` attempt `UNKNOWN_OUTCOME` (`active_work.py:283-288`) is not directly exercised; (c) READ_ONLY_VIOLATION -> conservative `OUTCOME_UNCERTAIN` exclusion (`active_work.py:440-441`) not exercised; (d) linked-worktree `workspace_instance` non-join not exercised; (e) two distinct READ_ONLY identities running concurrently is permitted by code but not asserted; (f) reader-blocked-by-active-writer direction not asserted; **(O-G1)** V8 has no test and cannot have one — the frozen-contract guarantee is a git-history property, caught by scope checks, not this module; **(O-G2)** the `WORK_MODE_INVALID` branch (`active_work.py:41`) is untested; **(O-G3)** `DEPENDENCY_IDENTITY_UNAVAILABLE` (`:68`) is not asserted directly, though `test_dependency_binding_swap_changes_work_identity` exercises dependency-fingerprint binding |
| T4 | Race coverage — two concurrent owners, and content vs authority change | **Two concurrent owners: covered** (`test_C1` :117, `test_C2_C3` :120, and both mid-flight change tests `test_C4_knowledge_changes_while_waiting` (:123) and `test_C5_dependency_changes_while_waiting` (:145), which mutate the input while a second owner waits). **Source-content change: covered** (`test_C4`, `test_L2_L3_L4`). **Missing:** a race where the **registry authority** (not the source content) changes while a second owner waits — that path lives in the Master D / knowledge-runtime module (`tests/test_knowledge_runtime_execution.py`), not here. Both reviews marked this a gap, not a defect |
| T5 | Weakened or skipped assertions? | **None.** `grep -n 'skip\|SkipTest\|expectedFailure\|TODO\|FIXME' tests/test_master_b.py` -> no matches **[MQ]**; 113 `self.assert*` statements across 48 tests **[MQ]**. Test-side edits by Master B are scope-consistent, not weakening, and are named explicitly: (1) `test_knowledge_contract.py` rollout test renamed and `execution.py` removed from the prohibited-consumers list (Master B now consumes the frozen contract by design — the V8 finding); (2) `test_incremental_reuse.py:255` changed an attempt assertion from task-scoped to run-scoped ("waiter/reuse run owns no fake Attempts"); (3) pass2/3/4 fixtures declare `mutation_scope: workspace` for MUTATING plans (required by the new fail-closed identity rule) |

### 5.1 Settled attributions — NOT reclassified

`test_opencode_import` and `test_opencode_outcome` remain **HISTORICAL** per
`docs/MASTER_D_ACCEPTANCE_V0_1.md:130-133`, with the recorded Home WSL caveat (both
measure OK there); `test_stabilization_fix_pass6` / `pass7` remain an **invocation
artifact** (`tests.<module>` vs discover); and
`test_runtime_authority.WorkspaceTests.test_infrastructure_repo_invocation_is_recognized`
remains **pre-existing / environment-specific** (the Home WSL checkout is named
`local-ai-infra`). None of these modules was run as evidence here, and none is
reclassified.

---

## 6. Claims versus code (`docs/MASTER_B.md` at `965fad5`)

Every claim in the claims document was checked against the code at `965fad5`.

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| B1 | Equivalence key = repo/workspace-instance identity, relative scope, exact task/objective/acceptance/permission identity, validated delivered knowledge identity, current context, dependency fingerprints, requested capability, resolved binding, provenance, protocol versions; run IDs/task IDs/timestamps/PIDs/titles do not define equivalence | CONFIRMED | `active_work.py:73-102`; `test_task_ids_and_titles_are_not_work_identity` (:356) |
| B2 | Required knowledge failures refuse before reuse; an omitted document is an explicit empty selection, never an inferred skill | CONFIRMED | `:55-58` builds an explicit empty document when `knowledge is None`; `:65-66` requires `kc.select()` outcome `READY`; `test_L5…` (:341), `test_knowledge_provider_unavailable_cannot_silently_drop_requirements` (:430) |
| B3 | Every reuse lookup and waiter adoption recomputes current policy, knowledge and context; deterministic dependency evidence refreshed before adoption; legacy cache/resume results lacking current identity are ineligible | CONFIRMED | `coordinate` re-runs `refresh()` before decision and before adoption (`:372-381`, `:411-417`); `accepted()` requires whole-identity equality (`:144`); `test_L1…` (:327), `test_resume_revalidates_current_knowledge` (:351), `test_C4…` (:123), `test_C5…` (:145) |
| B4 | Completed-result reuse honours `cache`/`force_refresh`; a requester that already observed active equivalent work may adopt the owner's result even with completed caching disabled; capability/binding equality is conservative, no fuzzy model matching | CONFIRMED | `coordinate:371` `can_reuse = (reuse and not force_refresh) or waited or joined_generation is not None`; `execution.py:511` default `cache=False`; `test_C1…` (:117), `test_C2_C3…` (:120), `test_force_refresh_waiter…` (:489) |
| B5 | One primary key per equivalence identity; `BEGIN IMMEDIATE`; conflict scan and insertion in the same short transaction; same-machine / same-operational-store only | CONFIRMED | `active_work.py:118-126`, `:164-193` |
| B6 | Mutation conflicts across the workspace instance; readers exclude writers; linked worktrees have different workspace instances | CONFIRMED (code) | `:176-183`; `authority.py:380-420` (`workspace_instance = worktree:<git-dir digest>`); tests per V3. Linked-worktree distinctness is code+authority evidence, not a Master B test (O/d gap) |
| B7 | Generation + random token fence launch, heartbeat and terminal publication; states are `CLAIMED`, `RUNNING`, `COMPLETED`, `FAILED_SAFE_TO_RETRY`, `OUTCOME_UNCERTAIN`; claims carry owner run/task/PID, mode, workspace, generation, token, lease and durable launch intent | CONFIRMED | `:133-138`, `:188-193`, `:195-205`, `:207-236`; five states used throughout |
| B8 | An owner heartbeats via a connection-local thread, not a daemon service; waiters poll for at most `wait_seconds` and create coordination events, not provider attempts; a timeout never starts duplicate work | CONFIRMED | `Lease:329-353`; `coordinate:359-360` bound validation (`WAIT_BOUND_INVALID`), `:397-399` deadline (`WAIT_TIMEOUT` event + `BLOCKED`); `test_wait_must_remain_bounded` (:456), `test_timeout…` (:172) |
| B9 | Recovery closes a vanished waiter's run/task state without touching the owner or inventing an Attempt | CONFIRMED | `_recover:257-266` writes only `runs`, `tasks` and a `WAIT_TIMEOUT` event; `test_R7…` (:274) |
| B10 | Publication order: persist launch intent + attempt ownership before the external call -> execute -> persist accepted result with current identity/generation -> mark claim `COMPLETED` with that durable reference | CONFIRMED | `execution.py:1397` (`launch` before the call) + attempt row created at the launch boundary `:1400-1421`; `active_work.py:207-235`; `test_P1…` (:308) |
| B11 | Completion cannot reference a missing result; recovery closes the result-persistence -> completion window without another worker call; completion is idempotent for the same generation/token/result; obsolete generations cannot publish | CONFIRMED | `complete():223-236` (`DURABLE_RESULT_REQUIRED`, early return when already `COMPLETED` with the same `result_id`); `_recover:271-276` promotes to `COMPLETED`; `:133-138`; `test_P2…` (:303), `test_P1…` (:308) |
| B12 | A vanished READ_ONLY owner is recoverable only after lease expiry and proof its process is dead; live non-heartbeating processes are not automatically replaced; bounded wait returns a timeout | CONFIRMED | `:277-280` (`if row['lease_until'] >= now or alive(row['owner_pid']): continue`); `test_live_owner_cannot_be_reclaimed_only_on_lease_expiry` (:284), `test_claimed_unlaunched_owner_vanished` (:256), `test_timeout…` (:172) |
| B13 | A launched mutation that loses its owner stays `OUTCOME_UNCERTAIN` and keeps excluding; expiry, quota failure and cancellation never authorise replay; a pre-launch failure releases the claim for safe contention | CONFIRMED | `:238-251`, `:280-283`; `test_M4_M5…` (:260), `test_M6…` (:266), `test_C6_M3…` (:243), `test_cancellation_stops_independent_later_nodes` (:445) |
| B14 | A real mutation is durably reusable only with a caller-owned postcondition verifier that verifies the current postcondition; requires explicit `mutation_scope: workspace`; unsupported/undeclared external scopes fail closed; the restriction is delivered to the worker; a known completed real mutation is not replayed merely because caching is off or refresh is requested; failed current verification blocks for reconciliation; without a verifier the success is retained but coordination stays uncertain and resume refuses adoption | CONFIRMED | `:43-50`, `coordinate:404-408` (`known_mutation`), `:420-423` (`MUTATION_RECONCILIATION_REQUIRED`); `test_external_mutation_is_refused_without_resource_scope` (:513), `test_known_mutation_never_replays_without_current_verifier` (:473), `test_unverified_mutation_is_not_adopted_on_resume` (:519), `test_real_mutation_requires_explicit_supported_resource_scope` (:215) |
| B15 | Stop and postcondition verifiers must return the boolean `True`; truthy strings/numbers are not proof; current mutation postconditions are checked under workspace exclusion before completed-result adoption | CONFIRMED | `:159` `postcondition(result) is not True`; `reconcile():300`; `coordinate:418-421` calls `store.fail(claim)`/adopts under workspace exclusion; `test_reconciliation_requires_both_proofs` (:297) |
| B16 | `WorkStore.reconcile` requires proof the old owner stopped and proof of the current postcondition; replacement generation, accepted result and completion are committed atomically, without replay | CONFIRMED | `reconcile():294-315`; `test_M7_M8…` (:289), `test_reconciliation_requires_both_proofs` (:297) |
| B17 | Pure `SIMULATED` adapters need no external-scope authorisation; their results cannot satisfy real execution | CONFIRMED (code) | `:44`; `adapter_mode`/`backend` participate in the identity `:91-96` (exact-match reuse). No dedicated Master B test (O-e gap) |
| B18 | A detected READ_ONLY filesystem violation retains conservative exclusion | CONFIRMED (code) | `coordinate:440-441` + `fail()` `:246-250` (an uncertain claim becomes a `MUTATING`-mode workspace blocker). No dedicated Master B test (O-c gap) |
| B19 | Accounting: Run/Task records exist before execution; real launches create durable Attempts; a killed owner's ambiguous `LAUNCHED` attempt is marked `UNKNOWN_OUTCOME`; waiters/adopters own no fake Attempt or provider usage; tool-only state and the safe READ_ONLY resume call counter survive interruption | CONFIRMED (code) | `execution.py:634-660`, `:1397-1421`; `active_work.py:283-288`; `orchestration_store.py:424-426`; `test_R7…` (:274), `test_M6…` (:266), `test_dogfood_quota_interruption…` (:526), `test_actual_model_effort_are_not_inferred` (:374) |
| B20 | Coordination events expose found work, waiting, timeout, revalidation failure, adoption, claim recovery and uncertain outcomes | CONFIRMED | `orchestration_store.py:66-76`; used in `active_work.py:186,260,266,278,380,388-397,417,440` |
| B21 | Requested/resolved/actual model-effort are separate persistent fields; unknown actuals stay null; contradicting actuals cannot satisfy reuse | CONFIRMED | `orchestration_store.py:207-212` (migration 4); `active_work.py:152-155`; `test_actual_model_effort_are_not_inferred` (:374), `test_reported_effort_mismatch_is_not_reused` (:504) |
| B22 | Deferred items remain deferred (named cross-provider routing, reviewer effort fidelity; Home WSL; no merge / Shared Knowledge Runtime rollout / Master C) | CONFIRMED | `docs/MASTER_B.md:100-118`; no branch code claims them done. `main` later edits cross-provider routing in `912060f`, outside this acceptance |

**No OVERSTATED claim was found.** Two claims are deliberately narrow and stay narrow:
B22 (deferrals) and the no-merge/no-rollout statement. One documentation weakness is
recorded as finding F3 (telegraphic prose). One brief-versus-document mismatch is
recorded in §9.

---

## 7. Findings

No **HIGH** finding was reported by either review. F1 (MEDIUM) is a documentation/scope
risk, not a code defect; all other findings are LOW.

| ID | Severity | Finding | Source | Reachability |
|---|---|---|---|---|
| F1 | MEDIUM | **Scope drift, not a defect.** This acceptance covers `965fad5` only; `main` since added 60/10 lines to `active_work.py`, 532/59 to `execution.py`, 20/3 to `result_cache.py`, 10/2 to `orchestration_store.py` (`912060f`, `0244b6c`, `990f037`, `a8ff68a` — all `main`-only). Reusing this verdict as "Master B plus the current controller is accepted" would overread it | run-8e0f5cf (N1) + run-a5c96bb (scope §); retained at MEDIUM | Documentation/scope risk only; reachable by any future reader who cites `main`'s controller under this verdict. Mitigation: the loud §3.3 statement |
| F2 | LOW | `alive(pid)` (`active_work.py:107-115`) uses `os.kill(pid, 0)`; a recycled PID, or `PermissionError` (which returns `True`), can make a dead owner look alive, so recovery skips the row and a waiter's bounded wait returns `TIMEOUT` / an uncertain mutation stays excluded. Failure direction is conservative | run-8e0f5cf (N2); code re-read **[MQ]** | Only on PID reuse or a `PermissionError`; single-machine scope is already declared |
| F3 | LOW | `docs/MASTER_B.md` is written in heavily telegraphic prose (articles/connectives elided, sentences reduced to fragments). Every claim is recoverable and was verified, but the document is hard to cite and easy to misquote | run-8e0f5cf (N4) | Documentation quality only; reachable by any reader. Not a code finding |
| F4 | LOW | `_recover()` runs a full `run_events` scan (correlated `NOT EXISTS` subquery) plus a scan of all `CLAIMED`/`RUNNING` claims on every `acquire()` (`active_work.py:253-288`, inside the write transaction at `:169`). Cost grows with event history. **NOT MEASURED** | run-8e0f5cf (N5); code re-read **[MQ]** | Normal operation as the operational store accumulates history; flagged for measurement, not an asserted defect |
| F5 | LOW | Recovery writes failure categories outside `orchestration_store.FAILURE_TAXONOMY` (`OWNER_LOST`, `WAITER_LOST`; coordination categories `WAIT_TIMEOUT`, `MUTATION_RECONCILIATION_REQUIRED` also appear as report/attempt categories). The constant is currently descriptive only — nothing in the code validates against it | run-a5c96bb (F1) | Only observable by a consumer that adopts the taxonomy constant; normal runs are unaffected |
| F6 | LOW | Test-coverage gaps listed in T3/T4 (union of both reviews). A future regression in those paths would not be caught by this module | run-a5c96bb (F2) + run-8e0f5cf (G1..G3) | Gaps only; the code paths were read and are consistent with the documented behaviour |
| F7 | LOW | `docs/MASTER_B.md` says "Linked worktrees have different workspace instances"; the only workspace-identity test (`test_same_basename_repo_and_linked_workspace_do_not_join:363`) varies the repository path and asserts a different `id`, not a different `workspace_instance` for linked worktrees of one repository | run-a5c96bb (F3) | Documentation/test-naming mismatch only; `authority.py:394-415` implements what the doc says |

---

## 8. What was NOT proven

- **Live provider/model behaviour.** Neither review made a live provider or model call;
  everything is offline (fake/stub backends, real SQLite, real threads).
- **`main`'s current versions of `active_work.py` (+60/−10), `execution.py` (+532/−59),
  `result_cache.py` (+20/−3) and `orchestration_store.py` (+10/−2)** — explicitly outside
  this verdict. Running Master B's module against `main`'s controller passes, but that is
  evidence about the tests, not an acceptance of `main`'s controller code.
- **Home WSL runtime behaviour.** Not exercised; the recorded environment note stands and
  the deferral is unchanged.
- **Cross-machine coordination.** V0.1 is declared same-machine / same-operational-store;
  nothing here covers two machines sharing work.
- **Performance of the recovery scan (F4).** Unmeasured — no benchmark was run and no
  performance claim is made.
- **The `tests.<module>` vs discover invocation artifact** for `test_stabilization_fix_pass6/7`
  was not re-run or reclassified here.
- **V9's reviewer-FAIL / secondary-FALSE scenario** is proven from code
  (`execution.py:114-157`) and the pre-existing classifier test
  (`test_execution_contract.py:183-186`), not from a Master B module test.
- **Any merge decision.** Whether to fast-forward Master B into `main` is a separate
  human call and is not made here.

---

## 9. Peer review and reconciliation note

**The collision.** Two independent MASTER B acceptance runs wrote this same path:

| Commit | Document | Verdict | HIGH findings | Status |
|---|---|---|---|---|
| `8e0f5cf` | first review, 234 lines | ACCEPTED_WITH_NOTES | none | on `main`, content replaced by `a5c96bb` |
| `a5c96bb` | second review, 155 lines | ACCEPTED_WITH_NOTES | none | on `main`, was the file content before this milestone |
| this commit | consolidated record | ACCEPTED_WITH_NOTES | none | final |

Both reviews agreed, so this is **corroboration**, not a contradiction. This file is the
consolidated record so a reader sees one authoritative document; the two commits remain
in history (nothing was rewritten or deleted). The reasons for multiple commits are
exactly this: two parallel runs at one path.

**R1 — evidence unique to the first review (run-8e0f5cf), carried forward:** the
per-commit own-contribution numstat table (§2.1); the raw geometry commands with exit
codes (§2.2); the **loud scope-drift** section with the per-file/per-commit `main`-only
breakdown (§3.3); deeper inline V1/V2/V5/V7 reasoning and per-test assertion references
(§4); the T1 crash-injection detail (`patch.object` at `:310`, `:407`/`:422`); the extra
T3 gaps O-G1 (`V8` has no test), O-G2 (`WORK_MODE_INVALID`), O-G3
(`DEPENDENCY_IDENTITY_UNAVAILABLE`); the "no OVERSTATED claim" note and the two
deliberately-narrow claims; and findings F1, F2, F3, F4 (older N1, N2, N4, N5).

**R2 — finding unique to the second review (run-a5c96bb), carried forward:** the **V8
finding** — Master B modified `tests/test_knowledge_contract.py` (rollout assertion
renamed; `execution.py` removed from the prohibited-consumers list) and that is
legitimate, not a weakening (re-confirmed by diff); the six specific coverage gaps plus
the T4 authority-under-contention gap; the sharper T5 naming three concrete test-side
edits with reasons; the clarification that `test_timeout_never_launches_duplicate`
asserts `worker.calls == []`; the brief-versus-document mismatch (below); and findings
F5, F6, F7 (taxonomy, coverage, linked-worktree naming).

**Merged / corrected.** Nothing from either review was dropped. Two corrections were
required by re-measurement: (i) the first review's "5 other test modules" for `9ca7bed`
is **6** other modules (+14/−5) — §2.1; (ii) the first review's claim that the module run
against `main` proves nothing about acceptance is preserved as a *scope* statement, not
as acceptance of `main`. No claim was carried solely because one version asserted it;
every carried git-derivable number was re-measured this milestone and every carried code
assertion was re-read.

**Brief-versus-document mismatch (kept from run-a5c96bb).** The review brief attributed
the gate "Master B independent acceptance = DEFERRED" to `docs/MASTER_B.md:118`. At the
branch tip that line is the **Home WSL Master 0 `DEFERRED_ENVIRONMENT_UNAVAILABLE`**
deferral. The Master B acceptance gate is actually recorded as open in
`docs/MASTER_D_ACCEPTANCE_V0_1.md:151` and `docs/ORCHESTRATION_ROUTING_FIX.md:122`.

**This milestone changed nothing else.** One file (`docs/MASTER_B_ACCEPTANCE_V0_1.md`),
one commit, `docs/` only. No source, test, config, machine, model, template or other
document was modified. Nothing was merged or deleted; the two prior commits were not
rewritten; `feat/master-b-durable-execution-v0.1` remains `965fad5` and unmerged.

---

## Next decision required

**One yes/no:** record Master B at `965fad5` as independently accepted with notes
(this verdict) and treat the Master B acceptance gate as delivered — or hold?

The fast-forward of Master B into `main` is **not** decided here; it remains a separate
human call, and this document neither performs nor recommends it.
