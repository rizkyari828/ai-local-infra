"""Execution Contract Stabilization V0.1 — FIX PASS 3 adversarial regressions.

Deterministic, fake/stub adapters only, temporary repositories, synthetic fixtures.
Zero real model/provider calls. One focused regression per Astra blocking invariant,
plus a nearby equivalent variant (adversarial probe) for each.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import unittest.mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import context_retrieval as ctx  # noqa: E402
import evidence  # noqa: E402
import execution  # noqa: E402
import failover  # noqa: E402
import orchestrator  # noqa: E402


def _repo(tmp: pathlib.Path) -> pathlib.Path:
    tmp.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=tmp, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.name", "t"], check=True)
    (tmp / "input.txt").write_text("input v1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp), "commit", "-m", "init"], check=True, capture_output=True)
    return tmp


def _accept(*criteria) -> dict:
    return {c: True for c in (criteria or ("ok",))}


def _ai(task_id, title, *, deps=None, refs=None, intent="READ_ONLY", acceptance=("ok",),
        family="generic", task_class="NORMAL", tier="BALANCED", effort="medium", budget="SMALL",
        contract=None):
    node = {"task_id": task_id, "title": title, "objective_summary": title, "task_family": family,
            "task_class": task_class, "depends_on": list(deps or []), "conditional": False,
            "intent": intent, "executor": "AI",
            "routing": {"tier": tier, "effort": effort, "role": "student"},
            "context": {"budget": budget}, "acceptance_criteria": list(acceptance),
            "risk_signals": [], "knowledge_refs": [], "context_refs": list(refs or [])}
    if contract is not None:
        node["task_contract"] = contract
    return node


def _tool(task_id, operation, *, deps=None, args=None):
    return {"task_id": task_id, "title": f"Deterministic {operation}",
            "objective_summary": f"Deterministic {operation}", "task_family": "deterministic_tool",
            "task_class": "MECHANICAL", "depends_on": list(deps or []), "conditional": False,
            "intent": "READ_ONLY", "executor": "TOOL_ONLY",
            "tool": {"operation": operation, "args": dict(args or {})}, "routing": None,
            "context": {"budget": "SMALL"}, "acceptance_criteria": [f"{operation} check satisfied"],
            "risk_signals": [], "knowledge_refs": [], "context_refs": []}


def _plan(nodes, *, intent="READ_ONLY", planner_mode="NONE"):
    return {"schema_version": 1, "valid": True, "intent": intent, "planner_mode": {"mode": planner_mode},
            "order": [n["task_id"] for n in nodes], "nodes": nodes}


def permit_local_inference(test: unittest.TestCase) -> None:
    """Declare the machine this fixture runs on (see tests/test_stabilization_fix_pass2.py).

    Hard machine policy binds at the shared execution boundary, so a mechanics fixture
    states a machine whose policy permits local inference. No assertion changes.
    """
    patcher = unittest.mock.patch.dict(os.environ, {"LAI_MACHINE": "home-wsl"})
    patcher.start()
    test.addCleanup(patcher.stop)


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _repo(pathlib.Path(self.tmp.name) / "repo")
        self.state = pathlib.Path(self.tmp.name) / "state"
        permit_local_inference(self)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, plan, backend=None, **kw):
        kw.setdefault("confirm", True)
        kw.setdefault("allow_dirty", True)
        kw.setdefault("record_evidence", False)
        # These mutation fixtures operate only on the temporary repository workspace.
        if any(node.get("intent") == "MUTATING" for node in plan["nodes"]):
            kw["constraints"] = {**(kw.get("constraints") or {}), "mutation_scope": "workspace"}
        backend = backend or execution.FakeBackend()
        return execution.execute_plan(plan, backend=backend, state_dir=self.state,
                                      repo_root=self.root, **kw), backend


# --------------------------------------------------------------------------- #
# INVARIANT 1 — resume uses recomputed dependency state (tests 1 + 2)
# --------------------------------------------------------------------------- #
class ResumeCurrentDependencyTests(Fixture):
    def test_recomputed_dependency_invalidates_descendant_reuse(self) -> None:
        plan = _plan([_tool("D1", "repository_state"), _ai("T2", "review the state", deps=["D1"])])
        first, _ = self._run(plan, run_id="r1")
        self.assertEqual("SUCCESS", first["nodes"]["D1"]["status"])
        self.assertEqual("SUCCESS", first["nodes"]["T2"]["status"])
        # Same HEAD, but the tracked working tree changes: D1 recomputes a new fingerprint.
        (self.root / "input.txt").write_text("input v2 changed\n", encoding="utf-8")
        report, backend = self._run(plan, run_id="r1", resume=True)
        self.assertIn("REFUSE_RESUME_INPUT_CHANGED", report.get("error", ""))
        self.assertEqual([], backend.calls)  # zero launches, no silent stale reuse

    def test_dependency_fingerprint_change_alters_descendant_identity(self) -> None:
        node = _ai("T2", "merge", deps=["D1"])
        old = [{"task_id": "D1", "status": "SUCCESS", "result_fingerprint": "FP_OLD", "summary": "17"}]
        new = [{"task_id": "D1", "status": "SUCCESS", "result_fingerprint": "FP_NEW", "summary": "17"}]
        first = execution._resume_node_identity(node, old, self.root, "proj", None, None)
        second = execution._resume_node_identity(node, new, self.root, "proj", None, None)
        self.assertNotEqual(first, second)


# --------------------------------------------------------------------------- #
# INVARIANT 2 — mutating failure uncertainty must not auto-replay (tests 3-5)
# --------------------------------------------------------------------------- #
class MutationUncertaintyTests(Fixture):
    def test_mutation_writes_then_raises_refuses_resume(self) -> None:
        workspace = self.root

        class Mutator(execution.Backend):
            name = "mutator"
            mode = "SUPPORTED"

            def available(self):
                return True

            def execute(self, request):
                (workspace / "mutated.txt").write_text("side effect", encoding="utf-8")
                raise RuntimeError("boom after write")

        plan = _plan([_ai("A1", "apply the patch", intent="MUTATING")], intent="MUTATING")
        first, _ = self._run(plan, backend=Mutator(), run_id="r1")
        self.assertEqual("FAILURE", first["nodes"]["A1"]["status"])
        self.assertTrue(first["nodes"]["A1"]["worker_launched"])
        self.assertEqual("MUTATION_OUTCOME_UNCERTAIN", first["nodes"]["A1"]["mutation_outcome"])
        resume_backend = execution.FakeBackend()
        resume_backend.name = "mutator"  # same execution policy identity as the first run
        report, backend = self._run(plan, backend=resume_backend, run_id="r1", resume=True)
        self.assertIn("REFUSE_RESUME_UNCERTAIN_MUTATION", report.get("error", ""))
        self.assertEqual([], backend.calls)

    def test_cancellation_after_possible_mutation_refuses_resume(self) -> None:
        workspace = self.root

        class CancelMutator(execution.Backend):
            name = "cancelmut"
            mode = "SUPPORTED"

            def available(self):
                return True

            def execute(self, request):
                (workspace / "mutated.txt").write_text("side effect", encoding="utf-8")
                raise KeyboardInterrupt()

        plan = _plan([_ai("A1", "apply the patch", intent="MUTATING")], intent="MUTATING")
        first, _ = self._run(plan, backend=CancelMutator(), run_id="r1")
        self.assertEqual("CANCELLED", first["nodes"]["A1"]["status"])
        resume_backend = execution.FakeBackend()
        resume_backend.name = "cancelmut"  # same execution policy identity as the first run
        report, backend = self._run(plan, backend=resume_backend, run_id="r1", resume=True)
        self.assertIn("REFUSE_RESUME_UNCERTAIN_MUTATION", report.get("error", ""))
        self.assertEqual([], backend.calls)

    def test_pre_launch_mutation_failure_stays_retry_safe(self) -> None:
        plan = _plan([_ai("A1", "apply the patch", intent="MUTATING")], intent="MUTATING")
        first, _ = self._run(plan, run_id="r1", max_ai_calls=0)
        self.assertEqual("CALL_BUDGET_EXHAUSTED", first["nodes"]["A1"]["status"])
        self.assertFalse(first["nodes"]["A1"]["worker_launched"])
        self.assertEqual("SAFE_TO_RETRY", first["nodes"]["A1"]["mutation_outcome"])
        report, _ = self._run(plan, run_id="r1", resume=True)
        self.assertNotIn("REFUSE_RESUME_UNCERTAIN_MUTATION", report.get("error", ""))


# --------------------------------------------------------------------------- #
# INVARIANT 3 — malformed / conflicting acceptance can never PASS (tests 6-9)
# --------------------------------------------------------------------------- #
class StrictAcceptanceTests(Fixture):
    def _status(self, criteria, acceptance):
        backend = execution.FakeBackend(script={"A1": [
            {"status": "SUCCESS", "completion_signal": "agent_reported", "acceptance_results": acceptance}]})
        report, _ = self._run(_plan([_ai("A1", "review", acceptance=criteria)]), backend=backend)
        return report["nodes"]["A1"]["status"], report["nodes"]["A1"].get("failure_category")

    def test_string_false_with_passed_status_is_not_pass(self) -> None:
        status, _ = self._status(("passed",), {"passed": "false", "status": "PASSED"})
        self.assertNotEqual("SUCCESS", status)
        self.assertEqual("UNVERIFIED", status)

    def test_true_with_failed_status_is_not_pass(self) -> None:
        status, _ = self._status(("passed",), {"passed": True, "status": "FAILED"})
        self.assertNotEqual("SUCCESS", status)
        self.assertEqual("UNVERIFIED", status)

    def test_false_with_passed_status_is_not_pass(self) -> None:
        status, _ = self._status(("passed",), {"passed": False, "status": "PASSED"})
        self.assertNotEqual("SUCCESS", status)
        self.assertEqual("UNVERIFIED", status)

    def test_consistent_typed_validator_passes(self) -> None:
        status, _ = self._status(("passed",), {"passed": True, "status": "PASSED"})
        self.assertEqual("SUCCESS", status)

    def test_conflicting_nested_value_is_not_pass(self) -> None:
        status, _ = self._status(("ok",), {"ok": {"passed": True, "status": "FAILED"}})
        self.assertNotEqual("SUCCESS", status)


# --------------------------------------------------------------------------- #
# INVARIANT 4 — never truncate executable task meaning (tests 10 + 11)
# --------------------------------------------------------------------------- #
_LONG_OBJECTIVE = (
    "Refactor the legacy parser so that it fixes the off-by-one boundary bug. Preserve every "
    "existing public interface exactly, keep all current behavior identical for existing "
    "callers, and keep the public names stable so nothing breaks downstream. Do not alter "
    "the output format. The final requirement is that Beta must return 99."
)


class ObjectiveIntegrityTests(Fixture):
    def test_tail_requirement_reaches_worker_unchanged(self) -> None:
        plan = orchestrator.decompose(_LONG_OBJECTIVE)
        captured: list[dict] = []

        class Probe(execution.FakeBackend):
            def execute(self, request):
                captured.append(request)
                return super().execute(request)

        self._run(plan, backend=Probe())
        self.assertTrue(captured)
        for request in captured:
            self.assertIn("Beta must return 99", request["prompt"])

    def test_authoritative_objective_is_not_truncated(self) -> None:
        plan = orchestrator.decompose(_LONG_OBJECTIVE)
        ai_nodes = [n for n in plan["nodes"] if n.get("executor") == "AI"]
        self.assertTrue(ai_nodes)
        authoritative = " ".join(node["task_contract"]["objective"] for node in ai_nodes)
        self.assertIn("Beta must return 99", authoritative)
        self.assertIn("off-by-one boundary bug", authoritative)


# --------------------------------------------------------------------------- #
# INVARIANT 5 — failover evidence matches the actual accepted candidate (12 + 13)
# --------------------------------------------------------------------------- #
class AttributionTests(Fixture):
    def test_infrastructure_failover_final_tier_is_strong(self) -> None:
        plan = _plan([_ai("A1", "analyze architecture", tier="BALANCED", effort="high")])
        failed = execution.FakeBackend(script={"A1": [
            {"task_id": "A1", "status": "FAILURE", "failure_category": "RATE_LIMIT"}]})
        failed.name = "codex"
        accepted = execution.FakeBackend(script={"A1": [
            {"task_id": "A1", "status": "SUCCESS", "completion_signal": "agent_reported",
             "acceptance_results": _accept("ok")}]})
        accepted.name = "opencode"
        health = failover.Health(binaries={"codex": True, "opencode": True},
                                 providers={"codex": {"codex"}, "opencode": {"deepseek"}},
                                 codex_model="gpt-b")
        # A BALANCED candidate first; B is a stronger (STRONG) capability-preserving fallback.
        candidates = [
            {"backend": "codex", "provider": "codex", "model": "gpt-b", "native_variant": "high",
             "tier": "BALANCED", "effort": "high"},
            {"backend": "opencode", "provider": "deepseek", "model": "deepseek-strong",
             "native_variant": "max", "tier": "STRONG", "effort": "max"},
        ]
        with unittest.mock.patch.object(execution.failover, "candidates_for_tier", return_value=candidates):
            report = execution.execute_plan(plan, backend=accepted, backends={"opencode": accepted, "codex": failed},
                                            health=health, constraints={}, repo_root=self.root, confirm=True,
                                            record_evidence=True, infra_retries=0, max_ai_calls=3,
                                            state_dir=self.state, allow_dirty=True)
        self.assertEqual("SUCCESS", report["nodes"]["A1"]["status"])
        self.assertEqual("STRONG", report["nodes"]["A1"]["final_tier"])
        self.assertEqual("BALANCED", report["nodes"]["A1"]["initial_tier"])
        rows = [r for r in evidence.all_rows(state_dir=self.state) if r["source"] == "observed_runtime"]
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual("opencode", row["surface"])
        self.assertEqual("deepseek", row["provider"])
        self.assertEqual("deepseek-strong", row["model"])
        self.assertEqual("max", row["native_variant"])
        self.assertEqual("MAX", row["normalized_effort"])
        self.assertEqual("STRONG", row["final_tier"])
        self.assertEqual("BALANCED", row["initial_tier"])

    def test_reasoning_escalation_final_tier_is_strong(self) -> None:
        plan = _plan([_ai("A1", "analyze architecture", tier="BALANCED", effort="high")])
        failed = execution.FakeBackend(script={"A1": [
            {"task_id": "A1", "status": "FAILURE", "failure_category": "REASONING_INSUFFICIENT"}]})
        failed.name = "codex"
        accepted = execution.FakeBackend(script={"A1": [
            {"task_id": "A1", "status": "SUCCESS", "completion_signal": "agent_reported",
             "acceptance_results": _accept("ok")}]})
        accepted.name = "opencode"
        health = failover.Health(binaries={"codex": True, "opencode": True},
                                 providers={"codex": {"codex"}, "opencode": {"deepseek"}},
                                 codex_model="gpt-b")
        balanced = [{"backend": "codex", "provider": "codex", "model": "gpt-b", "native_variant": "high",
                     "tier": "BALANCED", "effort": "high"}]
        strong = [{"backend": "opencode", "provider": "deepseek", "model": "deepseek-strong",
                   "native_variant": "max", "tier": "STRONG", "effort": "max"}]

        def candidates(tier, config=None):
            return strong if tier == "STRONG" else balanced

        with unittest.mock.patch.object(execution.failover, "candidates_for_tier", side_effect=candidates):
            report = execution.execute_plan(plan, backend=accepted, backends={"opencode": accepted, "codex": failed},
                                            health=health, constraints={}, repo_root=self.root, confirm=True,
                                            record_evidence=False, infra_retries=0, max_ai_calls=3,
                                            state_dir=self.state, allow_dirty=True)
        self.assertEqual("SUCCESS", report["nodes"]["A1"]["status"])
        self.assertEqual("STRONG", report["nodes"]["A1"]["final_tier"])


# --------------------------------------------------------------------------- #
# INVARIANT 6 — secret-bearing mapping keys must not survive persistence (14 + 15)
# --------------------------------------------------------------------------- #
class PersistedRedactionTests(Fixture):
    def test_nested_secret_mapping_key_is_redacted(self) -> None:
        secret_key = "sk-" + "k" * 30
        backend = execution.FakeBackend(script={"A1": [
            {"status": "SUCCESS", "completion_signal": "agent_reported", "acceptance_results": _accept("ok"),
             "recommended_followups": [{"nested": {secret_key: "value"}}, [{"deep": {secret_key: 1}}]]}]})
        self._run(_plan([_ai("A1", "review")]), backend=backend, run_id="redactkey")
        text = (self.state / "orchestration" / "redactkey" / "state.json").read_text(encoding="utf-8")
        self.assertNotIn(secret_key, text)
        self.assertIn("[REDACTED_KEY]", text)

    def test_multiple_secret_keys_collide_safely(self) -> None:
        first = "sk-" + "a" * 30
        second = "sk-" + "b" * 30
        redacted = execution._redact_state({"outer": {first: 1, second: 2, "[REDACTED_KEY]": 3}})
        keys = list(redacted["outer"].keys())
        self.assertNotIn(first, keys)
        self.assertNotIn(second, keys)
        self.assertEqual(len(keys), len(set(keys)))  # deterministic, unique, no secret text restored
        self.assertIn("[REDACTED_KEY]", keys)
        self.assertTrue(any(k.startswith("[REDACTED_KEY]_") for k in keys))


# --------------------------------------------------------------------------- #
# INVARIANT 7 — hard local_only enforced on every resolution path (16 + 17)
# --------------------------------------------------------------------------- #
class HardPolicyTests(Fixture):
    def test_local_only_without_health_rejects_remote(self) -> None:
        plan = _plan([_ai("T1", "analyze architecture", tier="STRONG")])
        bindings = execution.resolve_plan(plan, execution.FakeBackend(), constraints={"local_only": True})
        self.assertFalse(bindings[0]["runnable"])
        self.assertEqual("local_only", bindings[0]["reason"])
        report, backend = self._run(plan, constraints={"local_only": True})
        self.assertEqual([], backend.calls)
        self.assertEqual(0, report["calls_used"])

    def test_local_only_enforced_with_health_and_fallback_path(self) -> None:
        plan = _plan([_ai("T1", "analyze architecture", tier="MAX")])
        health = failover.Health(binaries={"codex": True, "opencode": True},
                                 providers={"codex": {"codex"}, "opencode": {"deepseek"}}, codex_model="gpt-b")
        bindings = execution.resolve_plan(plan, execution.FakeBackend(), health=health,
                                          constraints={"local_only": True}, backends={"opencode": execution.FakeBackend()})
        self.assertFalse(bindings[0]["runnable"])
        report, backend = self._run(plan, constraints={"local_only": True})
        self.assertEqual([], backend.calls)


# --------------------------------------------------------------------------- #
# INVARIANT 8 — delivered identity covers dependency semantics (18 + 19)
# --------------------------------------------------------------------------- #
class DependencyIdentityTests(unittest.TestCase):
    def test_summary_change_without_fingerprint_changes_identity_and_prompt(self) -> None:
        node = _ai("T2", "merge")
        old = [{"task_id": "D1", "status": "SUCCESS", "summary": "value=17"}]
        new = [{"task_id": "D1", "status": "SUCCESS", "summary": "value=99"}]
        self.assertNotEqual(execution.delivered_input_fingerprint(node, old, None),
                            execution.delivered_input_fingerprint(node, new, None))
        self.assertNotEqual(execution._prompt(node, old, manifest=None),
                            execution._prompt(node, new, manifest=None))
        self.assertIn("value=99", execution._prompt(node, new, manifest=None))

    def test_canonical_dependency_ordering_is_deterministic(self) -> None:
        node = _ai("T2", "merge")
        first = [{"task_id": "D2", "status": "SUCCESS", "summary": "b"},
                 {"task_id": "D1", "status": "SUCCESS", "summary": "a"}]
        second = [{"task_id": "D1", "status": "SUCCESS", "summary": "a"},
                  {"task_id": "D2", "status": "SUCCESS", "summary": "b"}]
        self.assertEqual(execution.delivered_input_fingerprint(node, first, None),
                         execution.delivered_input_fingerprint(node, second, None))


# --------------------------------------------------------------------------- #
# INVARIANT 9 — every actual worker launch produces an Attempt row (20-22)
# --------------------------------------------------------------------------- #
class AttemptAccountingTests(Fixture):
    def test_keyboard_interrupt_after_launch_records_attempt(self) -> None:
        plan = _plan([_ai("A1", "review")])
        report, _ = self._run(plan, backend=execution.FakeBackend(cancel_on="A1"))
        self.assertEqual("CANCELLED", report["nodes"]["A1"]["status"])
        self.assertEqual(1, report["calls_used"])
        self.assertEqual(1, len(report["attempts"]))
        self.assertEqual("CANCELLED", report["attempts"][0]["status"])

    def test_adapter_exception_after_launch_records_attempt(self) -> None:
        class Boom(execution.Backend):
            name = "boom"
            mode = "SUPPORTED"

            def available(self):
                return True

            def execute(self, request):
                raise RuntimeError("adapter exploded")

        plan = _plan([_ai("A1", "review")])
        report, _ = self._run(plan, backend=Boom())
        self.assertEqual("FAILURE", report["nodes"]["A1"]["status"])
        self.assertEqual(1, report["calls_used"])
        self.assertEqual(1, len(report["attempts"]))
        self.assertEqual("FAILED", report["attempts"][0]["status"])
        self.assertEqual("UNKNOWN_FAILURE", report["attempts"][0]["failure_category"])

    def test_pre_launch_rejection_records_no_attempt(self) -> None:
        plan = _plan([_ai("A1", "review")])
        with unittest.mock.patch.object(execution.context_retrieval, "build_pack",
                                        side_effect=ctx.ContextError("boom")):
            report, backend = self._run(plan)
        self.assertEqual([], backend.calls)
        self.assertEqual(0, report["calls_used"])
        self.assertEqual([], report["attempts"])

    def test_tool_only_and_cache_hit_record_no_attempt(self) -> None:
        tool_plan = _plan([_tool("D1", "repository_state")])
        report, _ = self._run(tool_plan)
        self.assertEqual([], report["attempts"])
        self.assertEqual(0, report["calls_used"])
        # Cache hit reuses a prior accepted READ_ONLY result with zero launches.
        cache_plan = _plan([_ai("A1", "review")])
        self._run(cache_plan, cache=True, run_id="c1")
        warm, backend = self._run(cache_plan, cache=True, run_id="c2")
        self.assertEqual(0, warm["calls_used"])
        self.assertEqual([], backend.calls)
        self.assertEqual([], warm["attempts"])


if __name__ == "__main__":
    unittest.main()
