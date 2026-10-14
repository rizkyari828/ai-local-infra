# Master B V0.1 — durable execution and conservative reuse

Master B consumes the frozen Master 0, Master A and Shared Knowledge Contract
identities. `execution.execute_plan` coordinates AI nodes using `active_work.py`
and schema 4 of the existing operational SQLite database. TOOL_ONLY operations
remain deterministic; coordination invokes no model. No database service,
background daemon, SFID skill loader or Shared Knowledge Runtime is introduced.

## Current identity

The equivalence key is produced by `knowledge_contract.active_work_equivalence_input`.
It includes canonical repository and workspace-instance identity, relative workspace
scope, exact task/objective/acceptance/permission identity, validated delivered
knowledge identity, current retrieved context, dependency result fingerprints,
requested capability, resolved execution binding, real/simulated provenance and
protocol versions. Run IDs, task IDs, timestamps, PIDs, display titles and agent
branding do not define equivalence. Required knowledge failures refuse before reuse.
An omitted knowledge document means an explicit empty selection, never an inferred
project skill. Callers may supply a `knowledge_provider(node)` to recompute a frozen
contract document. No sources are discovered or loaded by this hook.

Every reuse lookup and waiter adoption recomputes current policy, knowledge and
context. Deterministic dependency evidence is refreshed before adoption. Legacy
cache entries or resume results lacking the current identity are ineligible.
Completed-result reuse honors `cache`/`force_refresh`; a requester that already
observed active equivalent work may adopt the owner's accepted result even with
completed caching disabled. Equality of capability/binding is conservative; no
model equivalence or fuzzy matching is inferred.

The existing cache still stores its bounded results and history. Master B's
accepted-result references are the durable publication authority for coordination.
No old evidence is deleted when it becomes ineligible. The frozen freshness policy
participates in compatibility; volatile READ_ONLY observations remain stored as
history but cannot satisfy completed or waiter reuse.

## Claims and exclusion

`active_work` has one primary key per equivalence identity. Acquisition uses
`BEGIN IMMEDIATE` and performs the conflict scan and insertion in the same short
transaction. A generation plus random token fences every launch, heartbeat and
terminal publication. Different repository mutations conflict across the entire
workspace instance; active readers also exclude writers. Linked worktrees have
different workspace instances. V0.1 is same-machine/same-operational-store only.

The states are CLAIMED, RUNNING, COMPLETED, FAILED_SAFE_TO_RETRY and
OUTCOME_UNCERTAIN. Claims contain owner run/task/PID, mode, workspace, generation,
token, lease and durable launch intent. Worker execution never holds a database
transaction. An owner uses a connection-local heartbeat thread, not a daemon
service. Waiters poll for at most `wait_seconds` and create coordination events,
not provider Attempts. A timeout never starts duplicate work automatically. Recovery
closes a vanished waiter's run/task state without touching the owner or inventing
an Attempt.

## Publication and recovery

Publication order is:

1. Persist launch intent and Attempt ownership before the external call.
2. Execute and independently apply the existing acceptance rules.
3. Persist the accepted result with its current identity and generation.
4. Mark the claim COMPLETED with that durable result reference.

Completion cannot reference a missing result. Recovery can close the window
between steps 3 and 4 without another worker call. Completion is idempotent for
the same generation/token/result; obsolete generations cannot publish.

A vanished READ_ONLY owner can be recovered after both lease expiry and evidence
that its local process is no longer alive. Live but non-heartbeating processes
are not automatically replaced; bounded wait returns a timeout. A launched
mutation that loses its owner remains OUTCOME_UNCERTAIN and continues excluding
other mutations. Neither expiry, quota failure nor cancellation authorizes replay.
A prelaunch failure releases its claim for safe contention.

A real mutation is durably reusable only with a caller-owned
`postcondition_verifier(node, result, workspace)` that verifies the current
postcondition. Worker prose is not that verifier. A real mutation requires the explicit caller constraint `mutation_scope: workspace`
(CLI: `--mutation-scope workspace`). Unsupported or undeclared external resource
scopes fail closed. The workspace restriction is also delivered to the worker as a
trusted constraint. Stop and postcondition verifiers must return the boolean `True`;
truthy strings or numbers are not proof. Current mutation postconditions are checked
under workspace exclusion before completed-result adoption. Explicit external
mutation targets without a proven resource scope are refused. A known completed real
mutation cannot be replayed under the same work identity merely because caching is
disabled or refresh is requested; failed current verification blocks for reconciliation. Without one, a successful
worker report is retained but coordination remains uncertain and resume refuses
adoption. Explicit `WorkStore.reconcile` requires proof the old owner stopped and
proof of the current postcondition; replacement generation, accepted result and
completion are committed atomically, without replay. Pure SIMULATED adapters have
no external mutation to authorize; their results cannot satisfy real execution.
A detected READ_ONLY filesystem violation also retains conservative exclusion.

## Accounting and interruption

Run/Task records exist before execution; real worker launches create durable
Attempts. A killed owner may leave an ambiguous LAUNCHED Attempt, which recovery
marks UNKNOWN_OUTCOME. Waiters/adopters own no fake provider Attempt or provider
usage. Tool-only substeps and per-node state survive interruption in existing run
state; safe READ_ONLY resume preserves the prior call counter and revalidates
completed evidence. Coordination events expose found work, waiting, timeout,
revalidation failure, adoption, claim recovery and uncertain outcomes.

Requested model/effort, resolved model/effort and adapter-reported actual
model/effort have separate persistent fields. Unknown actual values stay null; reported actual values that contradict the resolved
binding cannot satisfy reuse.
Named cross-provider routing and reviewer effort fidelity are deferred engine
issues; Master B neither fixes those issues nor treats a requested value as proof
of actual execution fidelity. Compatibility remains provider-agnostic.

## Verification and gates

`tests/test_master_b.py` uses offline workers, temporary Git/SQLite state,
threading events and publication failpoints. It covers simultaneous requests,
changed knowledge/dependencies while waiting, mutation conflicts, stale claims,
generation fencing, quota interruption, reconciliation and result-publication
crashes. Frozen contract semantics and historical unrelated failures stay intact.

Home WSL Master 0 portability verification remains
DEFERRED_ENVIRONMENT_UNAVAILABLE. Master B is a feature branch for independent
review; no merge, Shared Knowledge Runtime rollout or Master C qualification is
part of this phase.
