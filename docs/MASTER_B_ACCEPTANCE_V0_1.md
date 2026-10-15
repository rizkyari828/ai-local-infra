# Master B Acceptance V0.1

**Verdict: ACCEPTED_WITH_NOTES**
**Date: 2026-10-15**
**Review type:** independent, read-only (Master B targeted acceptance). This document
merges nothing, changes nothing, and pushes nothing. It delivers the previously open
gate "Master B independent acceptance" recorded as open in
`docs/MASTER_D_ACCEPTANCE_V0_1.md:151` and `docs/ORCHESTRATION_ROUTING_FIX.md:122`.
Note on the review brief: the brief attributed that gate line to `docs/MASTER_B.md:118`;
at the branch tip that line is the Home WSL
`DEFERRED_ENVIRONMENT_UNAVAILABLE` deferral, which remains unchanged (see Gates).

## Reviewed branch and geometry

| Ref | Value |
|---|---|
| Repository | `~/Developer/Back-End/ai-local-infra` |
| main tip | `652b5ff778cf44ba74b79fea4f8e2c6e3604f2f8` |
| Master B tip | `965fad53839cf0da8c42839ffd00838d2de2f4d5` (`feat/master-b-durable-execution-v0.1`) |
| Master B base | `7b6b73badebeb1837e8d694932eed28b5e469250` (= frozen contract tip, `feat/shared-knowledge-contract-v0.1`) |
| `git rev-list --left-right --count main...feat/master-b-...` | `15  0` (main-only 15, branch-only 0) |
| Ancestor | YES — `git merge-base --is-ancestor feat/master-b-... main` exits 0; main has advanced past Master B |
| Master B own commits | `b8c0a93`, `9ca7bed`, `965fad5` (exactly 3; `git rev-list --count 7b6b73b..965fad5` = 3) |
| Master B own diff | `git diff 7b6b73b..965fad5` — 13 files, +1372/−54 |

Diff direction was understood and not misread: `git diff main..feat/master-b-...` lists
main's later commits in reverse (~26 files); it is **not** Master B work. Master B's own
work is the 13-file diff from its base `7b6b73b`.

## Scope

**Primary subject (this verdict covers):**

- `scripts/common/active_work.py` @ `965fad5` — identity, claim/acquire, coordination,
  launch, adoption, recovery.
- `tests/test_master_b.py` @ `965fad5` — 48 tests.
- `docs/MASTER_B.md` @ `965fad5` — claims document.

**Read for context and ordering only; NOT accepted or rejected here:**

- `scripts/common/execution.py` (Master B touched it; main changed it further)
- `scripts/common/result_cache.py` (same)
- `scripts/common/lifecycle.py`, `scripts/common/orchestration_store.py`
- `scripts/common/knowledge_contract.py` (FROZEN — verified untouched by Master B)

**Scope drift, stated so the acceptance is not overread:** main has advanced 15 commits
past Master B. Main's current `scripts/common/active_work.py` (70 lines changed),
`scripts/common/execution.py` (591 lines changed) and `scripts/common/result_cache.py`
(23 lines changed) differ from the reviewed revision (`git diff --stat 965fad5..main`).
Acceptance of `965fad5` does **not** cover main's current versions of those files.

**Excluded by instruction:** live provider behaviour (no live call was made); the Home
WSL runtime beyond the recorded environment note; any merge or fast-forward decision
(a separate human call); all gate statuses other than the Master B verdict.

## V1..V10 verification

| Item | Verdict | Evidence (file:line @ 965fad5) | Test that exercises it |
|---|---|---|---|
| V1 Identity binding | CONFIRMED | `active_work.py:37-104`: task identity (`:75-79` objective/acceptance/permissions/requirements), delivered knowledge identity (`:101-102` via `kc.active_work_equivalence_input` `knowledge_contract.py:721`), context manifest item fingerprints (`:89` via `result_cache.context_identity` `result_cache.py:174-195`), dependency fingerprints (`:90`), workspace instance/scope (`:83-85`), execution binding/provenance (`:91-98`) | `test_L2_L3_L4_content_scope_and_unselected_changes:333`, `test_L5_required_revoked_refuses_before_reuse:341`, `test_C5_dependency_result_identity_changes_key:369`, `test_dependency_binding_swap_changes_work_identity:387`, `test_policy_and_capability_not_equivalent:228`, `test_task_ids_and_titles_are_not_work_identity:356` |
| V2 Claim / acquire semantics | CONFIRMED | Exclusive insert under `BEGIN IMMEDIATE` `active_work.py:119-126,164-193`; second owner gets `WAIT`/`BUSY` (`:181-187`); ownership ordering claim -> launch -> adopt: `execution.py:793` (coordinate) -> `durable_store.launch(claim)` `execution.py:1397` -> `persist_result`/`complete` `active_work.py:436-437` | `test_C1_two_requests_one_launch:117`, `test_C2_C3_ten_requests_nine_waiters_one_launch:120`, `test_timeout_never_launches_duplicate:172` (asserts `worker.calls == []` and `WAIT_TIMEOUT`) |
| V3 Mutation exclusion | CONFIRMED | Conflict scan `active_work.py:176-183`: workspace-wide; requester MUTATING conflicts with all busy work, requester READ_ONLY conflicts with busy MUTATING only; same-identity READ_ONLY joins as waiter (`:184-185`, `:387-399`). Untested permitted/refused combinations named in T3 | `test_reader_writer_exclusion:223` (writer blocked by reader), `test_M1_M2_workspace_exclusion_for_equivalent_and_different_mutations:188`, `test_mutation_reuse_does_not_bypass_other_workspace_writer:481` |
| V4 In-flight dedup / single-flight | CONFIRMED | One `OWNER` insert per `work_key` inside the acquisition transaction `active_work.py:164-193`; later owners observe `WAIT` and adopt the owner's result `:366-381` | `test_C1...:117`, `test_C2_C3...:120`, `test_completion_between_lookup_and_acquire_does_not_launch_again:197` (two racing threads, exactly 1 execution) |
| V5 Lease and fencing | CONFIRMED | `_owned` fences generation+token+state `active_work.py:133-138`; fences launch `:195-199`, heartbeat `:201-205`, publish `:207-221`, completion `:223-236`; heartbeat thread `:329-353`; dead-owner recovery bumps generation `:280-283` | `test_C7_C8_stale_readonly_reclaimed_late_owner_fenced:248` (`STALE_GENERATION`), `test_M4_M5_launched_mutation_expiry_never_replays:260`, `test_claimed_unlaunched_owner_vanished:256`, `test_live_owner_cannot_be_reclaimed_only_on_lease_expiry:284` |
| V6 Conservative reuse | CONFIRMED | Exact persisted-vs-current identity equality `active_work.py:140-148`; READ_ONLY freshness gate `:149-151`; adapter-reported actuals vs resolved binding `:152-155`; mutation requires verified postcondition re-checked now `:156-158`; delivered-knowledge change changes the key (V1) so reuse is refused | `test_L1_legacy_result_cannot_match_current_version:327`, `test_L2_L3_L4...:333`, `test_L5...:341`, `test_L6_policy_change_blocks_reuse:346`, `test_volatile_observation_never_reuses_completed_result:395`, `test_reported_effort_mismatch_is_not_reused:504`, `test_verified_mutation_reuse_rechecks_current_postcondition:461` |
| V7 Recovery and interruption | CONFIRMED | `_recover` `active_work.py:253-288`: persisted-result window closed `:271-276`; dead owner after lease+pid check -> `FAILED_SAFE_TO_RETRY`/`OUTCOME_UNCERTAIN` `:277-286`; vanished waiter -> CANCELLED/BLOCKED `:254-268`; resume refuses changed current identity and uncertain mutations `execution.py:671-695` | `test_P1_result_persisted_completion_crash_reuses_without_worker:308`, `test_P2_completion_requires_durable_result:303`, `test_R7_disappeared_waiter...:274`, `test_M4_M5...:260`, `test_M6_quota_failure_is_uncertain:266`, `test_dogfood_quota_interruption_does_not_fabricate_completion:526`, `test_unverified_mutation_is_not_adopted_on_resume:519` |
| V8 Frozen contract untouched | CONFIRMED | `git log --oneline 7b6b73b..feat/master-b-durable-execution-v0.1 -- scripts/common/knowledge_contract.py` → empty; `git diff 7b6b73b..965fad5 -- scripts/common/knowledge_contract.py` → empty. (Master B did update `tests/test_knowledge_contract.py`'s rollout assertion to remove `execution.py` from the prohibited-consumers list — legitimate, since Master B consumes caller-supplied contract inputs; the contract module itself is byte-identical) | git evidence, not a test |
| V9 No fabricated success | CONFIRMED | Acceptance is independent of exit status `execution.py:114-157`; `accepted = process_ok and acceptance_state == ACCEPTANCE_PASSED` `execution.py:1475`; FAILED/UNVERIFIED finalize to FAILURE/UNVERIFIED `execution.py:1484-1509`; `acceptance` is `PASSED` only for status SUCCESS `execution.py:1706-1710`; `persist_result` rejects anything not `SUCCESS`+`PASSED` `active_work.py:207-211`; `accepted()` applies the same gate `:147-148`; `complete` requires a durable result `:228-235`; publication crash cannot fabricate (P1). Provider-unavailable/rate-limit -> FAILURE, never persisted. Reviewer-FAIL/secondary-FALSE classifier covered by `tests/test_execution_contract.py:183-186` (Ran 35 OK), not directly inside `test_master_b.py` (T3 gap) | `test_M6_quota_failure_is_uncertain:266`, `test_dogfood_quota_interruption_does_not_fabricate_completion:526`, `test_P1...:308` |
| V10 Zero provider calls in tests | CONFIRMED | All tests use in-process stubs: `Worker(Stub)` `test_master_b.py:23`, `Stub` "never touches a provider" `tests/test_runtime_authority_identity.py:71-84`; `run_work` always passes `backend=worker` `test_master_b.py:54-59`; no network imports in the module; the one CLI test patches `_local_health` to return `FakeBackend` `:177-181`. No live call was made by this review (0 provider calls) | whole module, 48/48 offline |

## Test-quality assessment (T1..T5)

**Module result:** `python3 -m unittest tests.test_master_b -v` → **Ran 48 tests, OK**
(48 "ok", 0 failures, 0 errors), interpreter **Python 3.9.6** (system `python3`). Re-run
on **Python 3.13.15** (`~/.local/bin/python3.13`): Ran 48, OK.

**State-dir trap:** NOT encountered. `tests.test_master_b` uses per-test temporary
state (`Fixture.setUp`, `test_master_b.py:42-52`) and required no `LAI_STATE_DIR`
override; nothing was created or modified under the real state directory.

**Supporting modules touched by Master B** (`test_incremental_reuse`,
`test_knowledge_contract`, `test_operational_store`,
`test_stabilization_fix_pass2/3/4`): 177 tests. Under Python 3.9.6 one failure:
`test_stabilization_fix_pass2.HumanAcceptanceTests.test_cli_records_acceptance_for_a_real_run`
— it launches `scripts/orchestrate` with `LAI_PYTHON=sys.executable` and the launcher
refuses Python < 3.11 (`launch: LAI_PYTHON=... is not a usable Python >= 3.11`). All
177 pass under Python 3.13.15. Classified **ENVIRONMENT_INTERPRETER**, not a Master B
defect. `tests.test_execution_contract` additionally: Ran 35, OK (3.9.6).

| Item | Assessment |
|---|---|
| T1 Real controller | Split, and sufficient. Core concurrency/recovery/reuse tests drive the real controller: `execution.execute_plan` via `Fixture.run_work` (`test_master_b.py:54`) against real `WorkStore`/SQLite with fake adapters, and they inspect real durable state (`store.run_attempts`, `accepted_work`) and real request payloads (`test_request_task_snapshot...:401`, `test_validated_knowledge_snapshot...:415`). `test_completion_between_lookup_and_acquire...:197` drives `active_work.coordinate` directly. Identity-component tests (`:356`, `:363`, `:369`, `:387`, `:228`) call `aw.identity` as units; not controller evidence on their own, but the same properties are exercised end-to-end by L2..L6 |
| T2 Non-vacuous assertions | Yes. `test_C1...:117` asserts exactly 1 worker launch from 2 concurrent runs — would fail with 2 if single-flight were removed. `test_timeout...:172` asserts zero calls plus `WAIT_TIMEOUT`. `test_C7_C8...:248` asserts a `STALE_GENERATION` raise on stale publication. `test_M4_M5...:260` asserts `OUTCOME_UNCERTAIN` and continued `BUSY` exclusion |
| T3 Dedicated coverage by item | V1..V7 covered by dedicated tests (see table). Gaps: (a) V9 reviewer-FAIL/secondary-FALSE path has no dedicated test inside `test_master_b.py` (classifier covered by `test_execution_contract.py:183-186`); (b) recovery marking a LAUNCHED Attempt `UNKNOWN_OUTCOME` (`active_work.py:283-288`) is not directly exercised; (c) READ_ONLY_VIOLATION -> conservative `OUTCOME_UNCERTAIN` exclusion (`active_work.py:440-441`) not exercised; (d) linked-worktree workspace-instance non-join not exercised (`test_same_basename...:363` varies the repository path, which changes `id`, not the worktree instance); (e) two distinct READ_ONLY identities running concurrently is permitted by code but not asserted; (f) reader-blocked-by-active-writer direction not asserted (only writer-blocked-by-reader). V8 is git evidence, not a test |
| T4 Race coverage | Yes for single-flight: 2 concurrent runs (`:117`) and 10 (`:120`), completion-between-lookup-and-acquire (`:197`), force-refresh waiter adopts the new generation (`:489`). Source-content change while waiting: covered (`test_C4...:123` changes delivered content under a blocked waiter; `test_C5...:145` changes a dependency revision). **Registry-authority change under contention is missing**: `test_L5...:341` proves authority refusal (REVOKED source) only sequentially, not while a second owner is blocked and revalidating |
| T5 Weakened assertions | None observed. No `skip`, `skipIf`, `skipUnless` or `expectedFailure` in `test_master_b.py`. Test-side edits by Master B are scope-consistent, not weakening: the knowledge-contract rollout test was renamed and `execution.py` removed from the prohibited-consumers list (Master B now consumes the frozen contract by design); `test_incremental_reuse.py:255` changed an attempt assertion from task-scoped to run-scoped ("waiter/reuse run owns no fake Attempts"); pass2/3/4 fixtures declare `mutation_scope: workspace` for MUTATING plans (required by the new fail-closed identity rule). No SKIPPED assertions were found |

## Claims versus code (docs/MASTER_B.md)

| Claim | Verdict | Evidence |
|---|---|---|
| Equivalence key includes repository/workspace-instance identity, relative scope, task/objective/acceptance/permission identity, delivered knowledge identity, context, dependency fingerprints, capability/binding, provenance and versions; run/task IDs, timestamps, PIDs, titles, branding do not define equivalence | CONFIRMED | `active_work.py:37-104`; `knowledge_contract.py:711-729`; `test_task_ids_and_titles_are_not_work_identity:356` |
| Required knowledge failures refuse before reuse; an omitted document is an explicit empty selection, never an inferred skill | CONFIRMED | `active_work.py:55-65`; `test_L5...:341`; `test_knowledge_provider_unavailable...:430` |
| Every reuse lookup and waiter adoption recomputes policy, knowledge and context; deterministic dependency evidence refreshed before adoption | CONFIRMED | `coordinate` refresh loop `active_work.py:361-363,372-381,427`; `execution.py:727-748`; `test_C4...:123`, `test_C5...:145` |
| Legacy cache/resume results lacking current identity are ineligible | CONFIRMED | `active_work.py:140-147` (version + identity equality); `execution.py:691`; `test_L1...:327`, `test_resume_revalidates_current_knowledge:351` |
| A requester that already observed active equivalent work may adopt the owner's result even with completed caching disabled | CONFIRMED | `active_work.py:366-370,376-381`; `execute_plan` default `cache=False` `execution.py:511`; `test_C1...:117`, `test_C2_C3...:120` (waiters succeed with no cache) |
| One primary key per equivalence identity; `BEGIN IMMEDIATE` with conflict scan and insertion in the same short transaction | CONFIRMED | `active_work.py:119-126,164-193` |
| Generation + random token fence launch, heartbeat and terminal publication | CONFIRMED | `active_work.py:133-138,188-193,195-205,207-236` |
| Mutation conflicts across the workspace instance; readers exclude writers; linked worktrees have different workspace instances | CONFIRMED (code) | `active_work.py:176-183`; `authority.py:380-420` (`workspace_instance = worktree:<git-dir digest>`); tests as in V3. Linked-worktree distinctness is code+authority evidence, not a Master B test (T3-d) |
| Waiters poll at most `wait_seconds`, create coordination events not provider Attempts; timeout never starts duplicate work | CONFIRMED | `active_work.py:378-399`; `activation` events `orchestration_store.py:66-76`; `test_timeout...:172`, `test_R7...:274` (zero waiter attempts), `:104-113` |
| Publication order launch-intent -> acceptance -> persist -> complete; completion cannot reference a missing result; recovery closes the 3-4 window without a worker call; completion idempotent; obsolete generations cannot publish | CONFIRMED | `execution.py:1397-1421`; `active_work.py:207-236,271-276`; `test_P1...:308`, `test_P2...:303`, `test_C7_C8...:248` |
| Vanished READ_ONLY owner recoverable only after lease expiry and proof its process is dead; live non-heartbeating owners not replaced; bounded wait times out | CONFIRMED | `active_work.py:277-280`; `test_live_owner_cannot_be_reclaimed_only_on_lease_expiry:284`, `test_claimed_unlaunched_owner_vanished:256`, `test_timeout...:172` |
| Launched mutation that loses its owner stays `OUTCOME_UNCERTAIN` and keeps excluding; expiry, quota failure and cancellation never authorize replay; prelaunch failure releases for safe contention | CONFIRMED | `active_work.py:238-251,277-286`; `test_M4_M5...:260`, `test_M6...:266`, `test_C6_M3...:243`, `test_cancellation_stops_independent_later_nodes:445` |
| Real mutation durably reusable only with caller-owned postcondition verifier returning boolean `True`; requires explicit `mutation_scope: workspace`; external scopes fail closed; restriction delivered to the worker; known completed mutation not replayed when caching off/refresh; failed current verification blocks for reconciliation; without verifier the success is retained but coordination stays uncertain and resume refuses adoption | CONFIRMED | `active_work.py:43-52,404-424,432-437`; `execution.py:1397-1509`; `test_verified_mutation_reuse...:461`, `test_known_mutation_never_replays_without_current_verifier:473`, `test_external_mutation_is_refused_without_resource_scope:513`, `test_real_mutation_requires_explicit_supported_resource_scope:215`, `test_unverified_mutation_is_not_adopted_on_resume:519` |
| `reconcile` requires proof the old owner stopped and proof of the current postcondition; replacement generation, accepted result and completion committed atomically without replay | CONFIRMED | `active_work.py:294-315`; `test_M7_M8...:289`, `test_reconciliation_requires_both_proofs:297` (truthy strings/numbers refused) |
| SIMULATED adapters need no external-scope authorization; their results cannot satisfy real execution | CONFIRMED (code) | `active_work.py:44`; exact-match identity includes backend/adapter mode `:91-96`. No dedicated Master B test (T3-e) |
| A detected READ_ONLY filesystem violation retains conservative exclusion | CONFIRMED (code) | `active_work.py:440-441` + `fail` `:246-250` (UNCERTAIN claim becomes MUTATING-mode blocker). No dedicated Master B test (T3-c) |
| Accounting: Run/Task exist before execution, real launches create durable Attempts, killed owner's LAUNCHED Attempt becomes `UNKNOWN_OUTCOME`, waiters own no Attempts, resume preserves call counter and revalidates | CONFIRMED (code) | `execution.py:634-660,1397-1421`; `active_work.py:283-288`; `orchestration_store.py:424-426`; `test_R7...:274`, `test_M6...:266`, `test_dogfood_quota...:526`, `test_actual_model_effort_are_not_inferred:374` |
| Coordination events expose found work, waiting, timeout, revalidation failure, adoption, claim recovery and uncertain outcomes | CONFIRMED | `orchestration_store.py:66-76`; used in `active_work.py:186,260,266,278,380,388-397,417,440` |
| Requested/resolved/actual model-effort kept separate; unknown actuals stay null; contradicting actuals cannot reuse | CONFIRMED | `orchestration_store.py:207-212` (migration 4); `active_work.py:152-155`; `test_actual_model_effort_are_not_inferred:374`, `test_reported_effort_mismatch_is_not_reused:504` |
| Deferred items remain deferred (named cross-provider routing, reviewer effort fidelity, Home WSL, no merge / Shared Knowledge Runtime rollout / Master C) | CONFIRMED | `docs/MASTER_B.md:100-118`; no branch code claims them done. (Main later edits cross-provider routing in `912060f`, outside this acceptance) |

## Findings

| ID | Severity | Finding | Reachability |
|---|---|---|---|
| F1 | LOW | Recovery writes failure categories outside `orchestration_store.FAILURE_TAXONOMY` (`OWNER_LOST`, `WAITER_LOST`; also `WAIT_TIMEOUT`, `MUTATION_RECONCILIATION_REQUIRED` appear as report/attempt categories). The constant is currently descriptive only — nothing in the code validates against it (grep: referenced only at its definition) | Only observable by a consumer that adopts the taxonomy constant; normal runs are unaffected. Triggered on any owner/waiter loss or contention timeout |
| F2 | LOW | Test-coverage gaps named in T3/T4: no direct test for reviewer-FAIL/secondary-FALSE inside `test_master_b.py`; LAUNCHED-Attempt `UNKNOWN_OUTCOME` recovery; READ_ONLY_VIOLATION uncertainty exclusion; linked-worktree instance non-join; two distinct READ_ONLY identities concurrently; reader-blocked-by-writer; registry-authority change under contention | Gaps only; code paths were read and are consistent with the documented behaviour. A future regression in those paths would not be caught by this module |
| F3 | LOW | `docs/MASTER_B.md` says "Linked worktrees have different workspace instances"; the only workspace-identity test (`test_same_basename_repo_and_linked_workspace_do_not_join:363`) varies the repository path and asserts different `id`, not different `workspace_instance` for linked worktrees of one repository | Documentation/test-naming mismatch only; the implementation in `authority.py:394-415` does what the doc says |

No HIGH or MEDIUM findings were found in the reviewed scope. No fabricated-success
path, no unfenced publication path, and no missing transaction boundary was found.

## What was NOT proven

- **Live provider/model behaviour.** No live call was made; all evidence is offline
  (fake in-process adapters). Nothing here proves real provider execution.
- **main's current versions of `scripts/common/active_work.py`, `execution.py` and
  `result_cache.py`.** They differ from `965fad5` (Master D and later work) and are
  not covered by this acceptance.
- **Home WSL runtime behaviour** beyond the recorded environment note; the Home WSL
  deferral is unchanged.
- The `tests.<module>` vs discover invocation artifact for `test_stabilization_fix_pass6/7`
  was not re-run or reclassified here.
- V9's reviewer-FAIL/secondary-FALSE scenario is proven from code (`execution.py:114-157`)
  and the pre-existing classifier test (`test_execution_contract.py:183-186`), not from a
  Master B module test.

## Next decision required

Record Master B at `965fad5` as **independently accepted with notes** (this verdict) and
treat the Master B acceptance gate as delivered — or hold? (yes/no)

The fast-forward of Master B into main is **not** decided here; it remains a separate
human call, and this document neither performs nor recommends it.
