# Master B Acceptance V0.1

**Verdict: `ACCEPTED_WITH_NOTES`**
**Date: 2026-10-15**
**Reviewed at commit `965fad53839cf0da8c42839ffd00838d2de2f4d5` (branch tip), not at `main`.**

---

## 1. Reviewed commits and geometry

| Ref | Value |
| --- | --- |
| Repository | `~/Developer/Back-End/ai-local-infra` |
| Reviewed branch | `feat/master-b-durable-execution-v0.1` (published, NOT merged by this review) |
| Reviewed commit | `965fad53839cf0da8c42839ffd00838d2de2f4d5` |
| Master B base | `7b6b73b` (`feat/shared-knowledge-contract-v0.1` tip — the frozen-contract ancestor) |
| `main` tip at review | `652b5ff` |
| Interpreter | Python 3.13.15 (`python3.13`) |

### 1.1 Master B's own commits (verified, not assumed)

The three commits around the branch tip are correct, and the grouping in the task
statement is confirmed by `git show --stat`:

| Commit | Subject | Own contribution |
| --- | --- | --- |
| `b8c0a93` | feat: add durable execution coordination and conservative reuse | `active_work.py` (new, +451), `execution.py` +206/−41, `orchestration_store.py` +33/−7, `orchestrate_cli.py` +4/−1 |
| `9ca7bed` | test: cover master b races mutation recovery and interruption | `tests/test_master_b.py` (new, +540) + 6-line touch-ups in 5 other test modules |
| `965fad5` | docs: describe master b durability and deferred engine boundaries | `docs/MASTER_B.md` (+120), `docs/ARCHITECTURE.md` (+4) |

Full own-scope diff (`git diff --numstat 7b6b73b..965fad5`) — 13 files,
+1372/−54. Everything before `b8c0a93` in this branch is Master A / Shared
Knowledge Contract work and is out of scope here.

### 1.2 Geometry evidence

```
$ git rev-list --left-right --count main...feat/master-b-durable-execution-v0.1
15	0

$ git merge-base --is-ancestor feat/master-b-durable-execution-v0.1 main ; echo $?
0

$ git merge-base --is-ancestor main feat/master-b-durable-execution-v0.1 ; echo $?
1
```

Master B is a **strict ancestor of `main`**: branch-only count is `0`, so it
contains no commit absent from `main`, and a fast-forward into `main` is trivially
possible and carries no conflict risk.

**Diff direction is understood.** `git diff main..feat/master-b-durable-execution-v0.1`
lists **15 commits** — those are the commits that landed on `main` *after* Master B
was cut, shown in reverse. They are **not** Master B's work. The work reviewed here
is the 3 commits in §1.1.

Fast-forwarding Master B into `main` is a **separate human decision**; it is not
made or implied by this verdict.

---

## 2. Scope

### 2.1 Under review

| Path | Role |
| --- | --- |
| `scripts/common/active_work.py` | primary subject — identity, claim/acquire, coordination, launch, adoption, recovery |
| `tests/test_master_b.py` | 48 tests, primary evidence |
| `docs/MASTER_B.md` | the claims document |

### 2.2 Read for context only — not accepted or rejected here

`scripts/common/execution.py`, `scripts/common/result_cache.py`,
`scripts/common/lifecycle.py`, `scripts/common/orchestration_store.py`,
`scripts/common/knowledge_contract.py` (frozen).

### 2.3 Scope drift — important, and state it loudly

`main` has advanced **the reviewed subject itself**, and the adjacent files, since
`965fad5`. Measured (`git diff --numstat 965fad5..main`):

| Path | added | deleted | commits on `main` after Master B |
| --- | --- | --- | --- |
| `scripts/common/active_work.py` | 60 | 10 | `912060f`, `990f037`, `a8ff68a` |
| `scripts/common/execution.py` | 532 | 59 | `912060f`, `0244b6c`, `990f037`, `a8ff68a` |
| `scripts/common/orchestration_store.py` | 10 | 2 | same range |
| `scripts/common/result_cache.py` | 20 | 3 | same range |
| `tests/test_master_b.py` | — | — | **identical** at `965fad5` and `main` |
| `docs/MASTER_B.md` | — | — | **identical** at `965fad5` and `main` |

The `main`-side additions to `active_work.py` are cross-provider/verified-result
hardening (`execution_requirement` + `allowed_bindings`, `result_contract` /
`verified_result` proof checks, and an `acceptance_check` parameter threaded through
`accepted()` / `acquire()` / `coordinate()`), landed by commits that are **not**
ancestors of the Master D review commit `c156eff`.

**Therefore: this acceptance covers Master B at `965fad5` only.** It does **not**
cover `main`'s current versions of `active_work.py`, `execution.py`,
`orchestration_store.py` or `result_cache.py`. Because the test module is byte-identical
at both revisions, the suite was run **twice** — once against the tip (extracted with
`git archive`, no repo mutation) and once against the `main` working tree — see §4.

---

## 3. Independent verification — V1..V10

Line numbers are against the **reviewed commit `965fad5`** (read from a
`git archive` extraction of that commit), not against `main`.

| # | Item | Status | Evidence | Test |
| --- | --- | --- | --- | --- |
| V1 | Identity binding — requirements identity, delivered validated knowledge identity, and context manifest item fingerprints all participate; content/authority/applicability changes change the key | **CONFIRMED** | `active_work.py:37` `identity()`; `:73-76` `kc.task_identity(objective_identity=result_cache.task_semantic_fingerprint(node), acceptance_identity=digest(...), permissions_identity=digest(...), requirements_identity=digest(requirements))`; `:101-102` `kc.active_work_equivalence_input(task=task, knowledge_identity=selected['delivered_identity'], …)`; `:89-90` `'context': result_cache.context_identity(manifest)` + `'dependencies': [d['result_fingerprint'] …]`; `:85-86` repo id + `workspace_instance` + `workspace_scope`; `:67-68` refuses `DEPENDENCY_IDENTITY_UNAVAILABLE` when any dependency lacks `result_fingerprint`; `:58,65-66` `kc.select()` must be `READY` before identity is built | `test_task_ids_and_titles_are_not_work_identity` (:356), `test_dependency_binding_swap_changes_work_identity` (:387), `test_L2_L3_L4_content_scope_and_unselected_changes` (:333), `test_policy_and_capability_not_equivalent` (:228), `test_validated_knowledge_snapshot_matches_delivered_worker_content` (:415), `test_same_basename_repo_and_linked_workspace_do_not_join` (:363) |
| V2 | Claim/acquire semantics — exclusive per identity, no duplicate launch by a second owner, timeout returns WAIT_TIMEOUT rather than launching, ordering claim → launch → adopt | **CONFIRMED** | `active_work.py:164` `acquire()` returns exactly one of `'RESULT'` (:175) / `'BUSY'` (:182) / `'WAIT'` (:184) / `'OWNER'` (:193); a busy row for the same key returns `WAIT`, never a launch; `:188` `INSERT OR REPLACE INTO active_work … 'CLAIMED'`; `:121,126` `launch()` sets `state='RUNNING'` under `_owned()`; ordering is `acquire` → `execute()` → `launch()` → `persist_result` → `complete`: `execution.py:1397` `durable_store.launch(claim)  # durable intent before any possible external call`, invoked from `execute_owned` (`execution.py:768`) which `coordinate` calls at `execution.py:793`; `active_work.py:365-378` deadline → `WAIT_TIMEOUT` event then `{'status':'BLOCKED','failure_category':'WAIT_TIMEOUT'}` | `test_C1_two_requests_one_launch` (:117), `test_C2_C3_ten_requests_nine_waiters_one_launch` (:120), `test_timeout_never_launches_duplicate` (:172), `test_completion_between_lookup_and_acquire_does_not_launch_again` (:197) |
| V3 | Mutation exclusion — MUTATING excludes concurrent equivalent work; READ_ONLY may join; the permitted/refused matrix | **CONFIRMED** | `active_work.py:176-180` conflicts query `"SELECT * FROM active_work WHERE workspace=? AND state IN ('CLAIMED','RUNNING','OUTCOME_UNCERTAIN') AND (mode='MUTATING' OR ?='MUTATING')"` against `identity['workspace']` (= `workspace_instance`): a MUTATING claim conflicts with **every** busy row in the workspace instance, and a READ_ONLY claim conflicts with any busy MUTATING row (reader/writer exclusion both directions). READ_ONLY vs READ_ONLY is the only pair that is allowed to coexist; `:43-50` refuses `MUTATION_SCOPE_UNPROVEN` for a real (non-`SIMULATED`) mutation without `mutation_scope:'workspace'`, and `UNSCOPED_EXTERNAL_MUTATION` for a `://` target or a path outside the owned root | `test_M1_M2_workspace_exclusion_for_equivalent_and_different_mutations` (:188), `test_reader_writer_exclusion` (:223), `test_real_mutation_requires_explicit_supported_resource_scope` (:215), `test_external_mutation_is_refused_without_resource_scope` (:513), `test_mutation_reuse_does_not_bypass_other_workspace_writer` (:481) |
| V4 | In-flight deduplication / single-flight | **CONFIRMED** | `active_work.py:118-126` `atomic()` is `BEGIN IMMEDIATE` … `commit()`/`rollback()`; `acquire()` performs `self._recover(now)`, the claim read, the conflict query and the `INSERT` inside that single write transaction (`:169-193`), so two concurrent callers serialise and the loser observes `WAIT`. No execution runs inside a transaction — `execute()` is called at `coordinate` `:429-430`, outside `atomic()` | `test_C1_two_requests_one_launch` (:117), `test_C2_C3_ten_requests_nine_waiters_one_launch` (:120) — asserts `len(worker.calls) == 1` and 9 waiters across 10 threads |
| V5 | Lease and fencing — a stale worker cannot publish over a newer claim; behaviour when the lease holder dies | **CONFIRMED** | `active_work.py:133-137` `_owned()` requires the live row's `generation`, `token` and `state in ('CLAIMED','RUNNING')`, else `STALE_GENERATION`; enforced at every publication point: `launch()` (:196), `heartbeat()` (:202), `persist_result()` (:215), `complete()` (:230), `fail()` (:242); `complete()` additionally requires the `accepted_work` row to carry the same `work_key`+`generation`+`token` (`:231-235`); `reconcile()` re-checks generation/token and raises `STALE_GENERATION` (:307); `Lease` (:329-351) heartbeats every 5 s on its **own** connection, daemon thread, never shared; dead-holder path is `_recover` `:278` `if row['lease_until'] >= now or alive(row['owner_pid']): continue` — a live but non-heartbeating owner is *not* reclaimed (bounded wait → `WAIT_TIMEOUT`), a dead+expired owner moves to `OUTCOME_UNCERTAIN` (launched mutation) or `FAILED_SAFE_TO_RETRY`, bumps `generation` (:282), and marks its `LAUNCHED` attempts `UNKNOWN_OUTCOME`/`OWNER_LOST` (:285-287) | `test_C7_C8_stale_readonly_reclaimed_late_owner_fenced` (:248), `test_live_owner_cannot_be_reclaimed_only_on_lease_expiry` (:284), `test_M7_M8_reconciliation_fences_old_mutation_without_replay` (:289) |
| V6 | Conservative reuse — a durable outcome is reused only while the current input identity still matches the persisted one; refused when delivered knowledge evidence differs | **CONFIRMED** | `active_work.py:140` `accepted()`; `:144` `if not row or json.loads(row['identity']) != identity: return None` — the persisted identity is compared **whole**, and it embeds `delivered_identity`, so a different delivered knowledge evidence cannot match; `:146` optional generation pin; `:149` requires `status == 'SUCCESS'` and `acceptance == 'PASSED'`; `:152` READ_ONLY requires a reusable freshness policy; `:155-157` `actual_*` must equal `resolved_*` (no inferred model/effort); `:159` MUTATING requires `postcondition_verified` **and** a current `postcondition(result) is True`; `identity()` `:64` refuses `KNOWLEDGE_DELIVERY_UNSAFE` before any reuse | `test_L1_legacy_result_cannot_match_current_version` (:327), `test_L2_L3_L4_content_scope_and_unselected_changes` (:333), `test_L5_required_revoked_refuses_before_reuse` (:341), `test_L6_policy_change_blocks_reuse` (:346), `test_resume_revalidates_current_knowledge` (:351), `test_volatile_observation_never_reuses_completed_result` (:395), `test_reported_effort_mismatch_is_not_reused` (:504), `test_force_refresh_waiter_adopts_new_generation_not_old_cached_result` (:489) |
| V7 | Recovery and interruption — recoverable state; a resumed run must not re-launch succeeded work nor adopt a stale result | **CONFIRMED** | `active_work.py:253-288` `_recover()`, invoked at the head of every `acquire()` inside the same write transaction (`:169`) and via `recover()` (:290); interrupted `WAIT_STARTED` events with a dead pid are closed (`runs`→`CANCELLED`/`WAITER_LOST`, `tasks`→`BLOCKED`, `WAIT_TIMEOUT` event) without touching the owner or inventing an attempt (:257-266); a claim whose generation+token already has an `accepted_work` row is promoted to `COMPLETED` instead of re-launched (:272-275); `fail()` returns early when a persisted result exists (`:245-247` "recovery must finish publication, never discard accepted evidence"); adoption always re-runs `refresh()` and refuses on change (`coordinate` :376-378, :415-417); `execution.py:688-691` refuses resume with `REFUSE_RESUME_UNCERTAIN_MUTATION` and `REFUSE_RESUME_CURRENT_IDENTITY_CHANGED` | `test_P1_result_persisted_completion_crash_reuses_without_worker` (:308), `test_P2_completion_requires_durable_result` (:303), `test_C6_M3_prelaunch_failure_allows_new_owner` (:243), `test_M4_M5_launched_mutation_expiry_never_replays` (:260), `test_M6_quota_failure_is_uncertain` (:266), `test_R7_disappeared_waiter_does_not_own_attempt_or_change_owner` (:274), `test_claimed_unlaunched_owner_vanished` (:256), `test_unverified_mutation_is_not_adopted_on_resume` (:519) |
| V8 | Frozen contract untouched — `scripts/common/knowledge_contract.py` not modified by Master B | **CONFIRMED** (structural, no test) | `git log --oneline 7b6b73b..965fad5 -- scripts/common/knowledge_contract.py` → **empty**; the same query for `authority.py` and `deterministic.py` is also empty; the file is absent from `git diff --name-only 7b6b73b..965fad5` | none — see T3 gap G1 |
| V9 | No fabricated success — terminal success cannot be recorded for reviewer FAIL, secondary-acceptance FALSE, provider unavailable, or publication crash | **CONFIRMED** | `active_work.py:209-213` `persist_result()` raises `RESULT_IDENTITY_MISMATCH` / `RESULT_NOT_ACCEPTED` (`status != 'SUCCESS'` or `acceptance != 'PASSED'`) / `POSTCONDITION_UNVERIFIED`; `:227-235` `complete()` raises `DURABLE_RESULT_REQUIRED` unless a matching durable row exists; `coordinate` :434-441 only persists when `verified` is true, otherwise `store.fail(claim)` and `coordination_outcome = 'OUTCOME_UNCERTAIN'`; `reconcile()` :294-301 raises `RECONCILIATION_UNPROVEN` / `RESULT_NOT_ACCEPTED`; unknown `actual_*` are never inferred (V6 `:155-157`) | `test_dogfood_quota_interruption_does_not_fabricate_completion` (:526), `test_P2_completion_requires_durable_result` (:303), `test_mutation_result_requires_current_postcondition` (:318), `test_actual_model_effort_are_not_inferred` (:374), `test_verified_mutation_reuse_rechecks_current_postcondition` (:461), `test_known_mutation_never_replays_without_current_verifier` (:473) |
| V10 | Zero provider calls in tests | **CONFIRMED** | `tests/test_master_b.py:19` imports `Stub` from `test_runtime_authority_identity`; `:23` `class Worker(Stub)`; every case drives `execution.execute_plan(..., backend=worker)` (`:57`); the only other backend is `execution.FakeBackend()` (`:181`, patch of `orchestrate_cli._local_health`). `grep -n 'requests\|urllib\|http\|socket\|subprocess\|urlopen' tests/test_master_b.py` → **no matches** | all 48 (the suite completes in ~27 s with no network) |

---

## 4. Test quality — T1..T5

**MODULE_RESULT:** `Ran 48 tests in 27.023s — OK` at the branch tip (`git archive 965fad5`), and
`Ran 48 tests in 27.129s — OK` against the `main` working tree.
**COUNT:** 48 (`grep -c '    def test' tests/test_master_b.py` = 48) — matches the stated count exactly.
**INTERPRETER:** Python 3.13.15. **STATE_DIR_TRAP_ENCOUNTERED:** no — every run used an isolated
`LAI_STATE_DIR` plus `LAI_MACHINE=work-mac` (`tests/test_master_b.py:49`), and the real state
directory was never written to.

| # | Question | Assessment |
| --- | --- | --- |
| T1 | Real controller, or helper-only? | **Real controller.** `Fixture.run_work` (`:57`) calls `execution.execute_plan(...)` with `confirm=True`, a real `state_dir`, and a `Worker` backend; assertions inspect the real durable store (`:114` `self.store().run_attempts(r['run_id'])`) and the real request payloads captured by the fake worker (`:108` `len(worker.calls)`). Two tests use `unittest.mock.patch` to inject a crash at a real boundary (`:310` `patch.object(aw.WorkStore,'complete',side_effect=OSError('publication crash'))`, `:407`/`:422` `patch.object(aw,'identity',side_effect=…)` for mid-flight change), which is the correct way to reach an otherwise unreachable window. Not helper-only. |
| T2 | Non-vacuous assertions? | **Yes.** `:108` `self.assertEqual(1, len(worker.calls))` and `:109` `self.assertEqual(count - 1, waiters)` fail immediately if single-flight is removed (10 threads would call the worker 10 times). `:113` `self.assertEqual(1 if i == 0 else 0, len(r['attempts']))` fails if a waiter fabricates an attempt. `:112` asserts a real node status of `SUCCESS` for all 10 participants. These are behavioural, not structural. |
| T3 | Coverage of V1..V9, and the gaps | Dedicated tests: V1 ✅ V2 ✅ V3 ✅ V4 ✅ V5 ✅ V6 ✅ V7 ✅ V9 ✅. **Gaps:** **G1 — V8 has no test and cannot have one**: the frozen-contract guarantee is a git-history property (`7b6b73b..965fad5` empty for that path), verified by command, not by test. A future regression would have to be caught by a scope check, not this module. **G2 — no test asserts the `WORK_MODE_INVALID` branch** (`active_work.py:41-42`), i.e. a node whose `intent` is neither `READ_ONLY` nor `MUTATING`. **G3 — no test asserts `DEPENDENCY_IDENTITY_UNAVAILABLE`** (`:68`) directly, though `test_dependency_binding_swap_changes_work_identity` exercises dependency-fingerprint binding. |
| T4 | Race coverage — two concurrent owners, and content vs authority change | **Two concurrent owners: covered** (`test_C1`, `test_C2_C3`, and both `ConcurrencyTests` mid-flight change tests `test_C4_knowledge_changes_while_waiting` (:123) and `test_C5_dependency_changes_while_waiting` (:145), which mutate the input while a second owner waits). **Which change is distinguished:** *source-content* change is covered (`test_C4`, `test_L2_L3_L4`); the *content-vs-authority* distinction is covered indirectly by `test_L5_required_revoked_refuses_before_reuse` (:341) and `test_policy_and_capability_not_equivalent` (:228). **Missing:** a race where the **registry authority** (not the source content) changes while a second owner waits — that path lives in the Master D / knowledge-runtime module (`tests/test_knowledge_runtime_execution.py`), not here. Stated as a gap, not a defect. |
| T5 | Weakened or skipped assertions? | **None.** `grep -n 'skip\|SkipTest\|expectedFailure\|TODO\|FIXME' tests/test_master_b.py` → **no matches**. 113 `assert*` statements across 48 tests; no `xfail`, no conditional pass, no `assertTrue(True)`. |

### 4.1 Settled attributions — NOT reclassified

`test_opencode_import` and `test_opencode_outcome` remain **HISTORICAL** per
`docs/MASTER_D_ACCEPTANCE_V0_1.md:130-133` with its recorded Home WSL caveat;
`test_stabilization_fix_pass6` / `pass7` remain an **invocation artifact**; and
`test_runtime_authority.WorkspaceTests.test_infrastructure_repo_invocation_is_recognized`
remains **pre-existing / environment-specific** (the Home WSL checkout is named
`local-ai-infra`). None of these modules was run as evidence in this review, and none is
reclassified here.

---

## 5. Claims versus code

Every claim in `docs/MASTER_B.md` was checked against the code at `965fad5`.

| # | Claim (MASTER_B.md) | Verdict | Evidence |
| --- | --- | --- | --- |
| B1 | Equivalence key is produced by `knowledge_contract.active_work_equivalence_input` and includes repo/workspace-instance identity, relative workspace scope, exact task/objective/acceptance/permission identity, validated delivered knowledge identity, current retrieved context, dependency result fingerprints, requested capability, resolved execution binding, provenance, protocol versions; run IDs, task IDs, timestamps, PIDs and titles do not define equivalence | **CONFIRMED** | `:101-102`, `:73-76`, `:85-102`; `test_task_ids_and_titles_are_not_work_identity` (:356) |
| B2 | Required knowledge failures refuse before reuse; an omitted knowledge document means explicit empty selection, never an inferred project skill | **CONFIRMED** | `:56-58` builds the empty document explicitly when `knowledge is None`; `:65-66` requires `kc.select()` outcome `READY`; `test_L5_required_revoked_refuses_before_reuse` (:341), `test_knowledge_provider_unavailable_cannot_silently_drop_requirements` (:430) |
| B3 | Every reuse lookup and waiter adoption recomputes current policy and knowledge context; deterministic dependency evidence is refreshed before adoption; legacy cache entries lacking current identity are ineligible | **CONFIRMED** | `coordinate` re-runs `refresh()` before each decision and before adoption (`:376-378`, `:415-417`); `accepted()` requires whole-identity equality (:144); `test_L1_legacy_result_cannot_match_current_version` (:327), `test_resume_revalidates_current_knowledge` (:351) |
| B4 | Completed-result reuse honors `cache`/`force_refresh`; a requester that already observed active equivalent work may adopt the owner's accepted result even when completed-caching is disabled; capability/binding equality is conservative with no fuzzy model matching | **CONFIRMED** | `coordinate` `:372-373` `can_reuse = (reuse and not force_refresh) or waited or joined_generation is not None`; `:373-375` / `:410-412` generation pinning under `force_refresh`; `test_force_refresh_waiter_adopts_new_generation_not_old_cached_result` (:489) |
| B5 | READ_ONLY work may join; different-repository mutations conflict across the entire workspace instance; active readers exclude writers; linked worktrees have different workspace instances; V0.1 is same-machine / same-operational-store only | **CONFIRMED** | conflicts query `:176-180` scoped to `identity['workspace']` = `workspace_instance`; `test_M1_M2` (:188), `test_reader_writer_exclusion` (:223), `test_same_basename_repo_and_linked_workspace_do_not_join` (:363) |
| B6 | `BEGIN IMMEDIATE` transaction; generation plus random token fence launch, heartbeat and terminal publication | **CONFIRMED** | `:118-126` (transaction), `:188` / `_owned` `:133-137` (generation + `uuid4().hex` token) |
| B7 | States are `CLAIMED`, `RUNNING`, `COMPLETED`, `FAILED_SAFE_TO_RETRY`, `OUTCOME_UNCERTAIN`; claims contain owner run/task/PID, mode, workspace, generation, token, lease and durable launch intent; worker execution never holds a database transaction | **CONFIRMED** | `:27`, `:188-190` (13-column `INSERT`), `:126` (`launch_intent=1`); `execute()` is invoked outside `atomic()` (`coordinate` `:429-430`) |
| B8 | An owner heartbeats with a connection-local thread, not a daemon service; waiters poll for at most `wait_seconds` and create coordination events, not provider attempts; timeout never starts duplicate work automatically | **CONFIRMED** | `Lease` `:329-351` (heartbeat thread, own connection); `coordinate` `:359-360` bound validation → `WAIT_BOUND_INVALID`, `:365-378` deadline → `WAIT_TIMEOUT` event and `BLOCKED`; `test_wait_must_remain_bounded` (:456), `test_timeout_never_launches_duplicate` (:172) |
| B9 | Recovery closes a vanished waiter's run/task state without touching the owner or inventing an Attempt | **CONFIRMED** | `_recover` `:257-266` — writes `runs`, `tasks` and a `WAIT_TIMEOUT` event only; the `active_work` owner row and `attempts` are untouched; `test_R7_disappeared_waiter_does_not_own_attempt_or_change_owner` (:274) |
| B10 | Publication order is: persist launch-intent + attempt ownership before the external call → execute → persist accepted result with current identity/generation → mark claim `COMPLETED` with the durable result reference | **CONFIRMED** | `execution.py:1397` (`launch` before the call) and the attempt row is created at the launch boundary; `active_work.py:207-235`; `test_P1_result_persisted_completion_crash_reuses_without_worker` (:308) |
| B11 | Completion cannot reference a missing result; recovery can close the window between result persistence and claim completion without another worker call; completion is idempotent for the same generation/token/result; obsolete generations cannot publish | **CONFIRMED** | `complete()` `:223-235` (`DURABLE_RESULT_REQUIRED`, early return when already `COMPLETED` with the same `result_id`); `_recover` `:272-275` promotes to `COMPLETED`; `_owned` generation+token fencing `:133-137`; `test_P2_completion_requires_durable_result` (:303), `test_P1…` (:308) |
| B12 | A vanished READ_ONLY owner can be recovered only after **both** lease expiry and evidence the local process is no longer alive; live-but-non-heartbeating processes are not automatically replaced and a bounded wait returns timeout | **CONFIRMED** | `_recover` `:278` `if row['lease_until'] >= now or alive(row['owner_pid']): continue` (both conditions required to proceed); `test_live_owner_cannot_be_reclaimed_only_on_lease_expiry` (:284), `test_C7_C8_stale_readonly_reclaimed_late_owner_fenced` (:248) |
| B13 | A launched mutation that loses its owner remains `OUTCOME_UNCERTAIN` and continues excluding other mutations; neither expiry, quota failure nor cancellation authorizes replay; a pre-launch failure releases the claim safely | **CONFIRMED** | `_recover` `:280-283`; exclusion of `OUTCOME_UNCERTAIN` in the conflicts query `:177`; `fail()` `:241-243` (`uncertain or (mode == 'MUTATING' and launch_intent)`); `test_M4_M5_launched_mutation_expiry_never_replays` (:260), `test_M6_quota_failure_is_uncertain` (:266), `test_C6_M3_prelaunch_failure_allows_new_owner` (:243) |
| B14 | A real mutation is durably caller-owned; explicit external mutation targets without a proven resource scope are refused; a known completed real mutation cannot be replayed merely because caching is disabled or refresh requested; failed current verification blocks reconciliation; `WorkStore.reconcile` requires proof the old owner stopped **and** proof of the current postcondition; pure `SIMULATED` adapters have no external mutation to authorize and their results cannot satisfy real execution | **CONFIRMED** | `:43-50` scope refusal; `coordinate` `:404-408` `known_mutation` detection and forced re-check, `:420-423` `MUTATION_RECONCILIATION_REQUIRED`; `reconcile()` `:294-301`; `:93` `adapter_mode` participates in the identity, so a `SIMULATED` result cannot match a real run; `test_external_mutation_is_refused_without_resource_scope` (:513), `test_known_mutation_never_replays_without_current_verifier` (:473), `test_reconciliation_requires_both_proofs` (:297), `test_mutation_result_requires_current_postcondition` (:318) |
| B15 | Postcondition verifiers must return boolean `True`; truthy strings/numbers are not proof; current mutation postconditions are checked under workspace exclusion before completed-result adoption | **CONFIRMED** | `:159` `postcondition(result) is not True`; `:300` `postcondition(result) is not True`; `coordinate` calls `store.fail(claim)` in the same branch that adopts (`:418-421`), i.e. under the workspace-exclusion transaction |
| B16 | Run/Task records exist before execution; real worker launches create durable Attempts; a killed owner may leave an ambiguous `LAUNCHED` attempt which recovery marks `UNKNOWN_OUTCOME`; waiters/adopters own no fake provider Attempt or usage; tool-only substep state survives interruption; safe READ_ONLY resume preserves the prior call counter and revalidates completed evidence | **CONFIRMED** | `_recover` `:285-287`; `test_R7_disappeared_waiter_does_not_own_attempt_or_change_owner` (:274); `:113` asserts waiter attempt count is 0 |
| B17 | Requested model/effort, resolved model/effort and adapter-reported actual model/effort are separate persistent fields; unknown actual values stay null; reported actual values that contradict the resolved binding cannot satisfy reuse | **CONFIRMED** | `:155-157`; `settle_attempt()` `:317-325` writes requested/resolved/actual separately; `test_actual_model_effort_are_not_inferred` (:374), `test_reported_effort_mismatch_is_not_reused` (:504) |
| B18 | Named cross-provider routing, reviewer effort fidelity and deferred engine issues are deferred; Master B neither fixes them nor treats a requested value as proof of actual execution fidelity; compatibility remains provider-agnostic | **CONFIRMED (stays deferred)** | `identity()` `:72-73` excludes `prefer_provider`/`prefer_model` from the permission identity but carries them as `requested_*`; no engine-fidelity logic exists in the reviewed scope; consistent with the deferral statement |
| B19 | 48 tests; Git/SQLite; the Home WSL portability verification remains `DEFERRED_ENVIRONMENT_UNAVAILABLE`; Master B is feature-branch independent review, and no merge, Shared Knowledge Runtime rollout or Master C qualification is part of the phase | **CONFIRMED** | 48 tests counted and run (§4); no merge performed by this review; Home WSL portability is not claimed |

**No OVERSTATED claim was found.** Two claims are deliberately narrow and stay narrow:
B18 (deferrals remain deferrals) and B19 (no merge / no rollout). One documentation
weakness is recorded as finding N4.

---

## 6. Findings

| ID | Severity | Finding | Reachability |
| --- | --- | --- | --- |
| N1 | **MEDIUM** | **Scope drift, not a defect:** this acceptance covers `965fad5` only. `main` has since added 60/10 lines to `active_work.py`, 532/59 to `execution.py`, 20/3 to `result_cache.py` and 10/2 to `orchestration_store.py` (`912060f`, `0244b6c`, `990f037`, `a8ff68a` — all `main`-only, none an ancestor of the Master D review commit `c156eff`). Reusing this verdict as "Master B plus the current controller is accepted" would overread it. | Documentation/scope risk only; it is reachable by any future reader who cites this file for `main`'s code. Mitigation is the scope statement in §2.3 and the next-decision item in §9. |
| N2 | LOW | `alive(pid)` (`:107-115`) uses `os.kill(pid, 0)`; a recycled PID (or `PermissionError`, which returns `True`) can make a dead owner look alive. Recovery then skips the row and the waiter's bounded wait returns `TIMEOUT`. `OUTCOME_UNCERTAIN` for mutations, so the failure direction is conservative. Note also that PIDs are meaningful only on the same machine, which the V0.1 scope already declares. | Only on PID reuse or a `PermissionError` for a vanished owner — rare; consequence is a blocked wait, never a duplicate launch or a stale adoption. |
| N3 | LOW | `coordinate` reads `SELECT COALESCE(MAX(rowid),0) FROM accepted_work` (`:364`) outside any transaction. A publish committing immediately after this read is not seen by `new_result`; the caller then waits instead of adopting. `accepted()` re-checks the full identity afterwards, so no incorrect reuse follows. | Only under a genuine concurrent publish between the marker read and `acquire()`; effect is an extra wait, not incorrect reuse. |
| N4 | LOW | `docs/MASTER_B.md` is written in heavily telegraphic prose (articles and connectives elided, several sentences reduced to fragments, e.g. the "RESULT_PUBLISHED" and "ACCOUNTING AND INTERRUPTION" sections). Claims are recoverable and were all verified, but the document is hard to cite and easy to misquote. | Documentation-quality only; reachable by any reader. Not a code finding. |
| N5 | LOW | `_recover()` runs a full scan of `run_events` with a correlated `NOT EXISTS` subquery, plus a scan of all `CLAIMED`/`RUNNING` claims, on **every** `acquire()` (`:253-288` inside the write transaction at `:169`). Cost grows with event history. | Normal runs as the operational store accumulates history; **NOT MEASURED** in this review — no benchmark was run and no performance claim is made. Flagged for measurement, not asserted as a defect. |

No HIGH finding was identified in the reviewed scope.

---

## 7. Not proven in this review

- **Live provider behaviour.** No live provider or model call was made. Everything
  here is offline: fake/stub backends, real SQLite, real threads.
- **`main`'s current versions** of `active_work.py` (+60/−10), `execution.py`
  (+532/−59), `result_cache.py` (+20/−3) and `orchestration_store.py` (+10/−2) —
  explicitly outside this verdict (§2.3). The suite passing against the `main` working
  tree is evidence about the tests, not an acceptance of that code.
- **Home WSL runtime behaviour.** Not exercised; the recorded environment note stands
  and remains `DEFERRED_ENVIRONMENT_UNAVAILABLE`.
- **Cross-machine coordination.** V0.1 is same-machine / same-operational-store by
  declaration; nothing in this review covers two machines sharing work.
- **Performance of the recovery scan (N5).** Unmeasured.
- **Any merge decision.** Whether to fast-forward Master B into `main` — which is
  trivially possible, since it is already an ancestor — is a separate human call and
  is not made here.

---

## 8. This review produced no other change

One new file (`docs/MASTER_B_ACCEPTANCE_V0_1.md`), one commit, `docs/` only.
No source file, test, descriptor, config or other document was modified. Nothing was
merged, pushed, rebased, amended or deleted. The Master B branch was not modified.

---

## 9. Next decision required

**One yes/no:** do you authorize a **separate** acceptance milestone for `main`'s
current `active_work.py` / `execution.py` / `result_cache.py` /
`orchestration_store.py` (the post-Master-B commits `912060f`, `0244b6c`, `990f037`,
`a8ff68a`), or should this verdict stand scoped to `965fad5` and the gate close as
delivered?
