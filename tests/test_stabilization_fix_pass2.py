"""Execution Contract Stabilization V0.1 — FIX PASS 2 adversarial regressions.

Deterministic, fake/stub adapters only, temporary repositories, synthetic fixtures.
Zero real model/provider calls. One focused regression per Astra blocking finding.
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
import discovery  # noqa: E402
import evidence  # noqa: E402
import execution  # noqa: E402
import failover  # noqa: E402
import human_acceptance  # noqa: E402
import orchestrator  # noqa: E402
import outcomes  # noqa: E402
import result_cache as rc  # noqa: E402


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


def _plan(nodes, *, intent="READ_ONLY", planner_mode="NONE"):
    return {"schema_version": 1, "valid": True, "intent": intent, "planner_mode": {"mode": planner_mode},
            "order": [n["task_id"] for n in nodes], "nodes": nodes}


def _row(**kw) -> dict:
    base = {"task_family": "codex_model_availability", "task_class": "NORMAL", "surface": "opencode",
            "provider": "p", "model": "m", "native_variant": None, "normalized_effort": None,
            "initial_tier": "BALANCED", "initial_effort": "MEDIUM", "final_tier": "BALANCED",
            "final_effort": "MEDIUM", "outcome": "SUCCESS", "failure_category": None,
            "source": "observed_runtime", "escalated": False, "simulated": 0}
    base.update(kw)
    return base


def permit_local_inference(test: unittest.TestCase) -> None:
    """Declare the machine this fixture runs on.

    These fixtures drive claimed-real (SUPPORTED) stub adapters on a BALANCED node,
    which resolves to the local student role. Hard machine policy binds at the shared
    execution boundary (docs/RUNTIME_AUTHORITY.md), so a mechanics fixture states a
    machine whose policy permits local inference. No assertion changes.
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
# 1 + 2 + 4. Resume must fail closed
# --------------------------------------------------------------------------- #
class ResumeFailClosedTests(Fixture):
    def test_same_head_relevant_input_change_refuses_resume(self) -> None:
        plan = _plan([_ai("A1", "inspect input", refs=[{"path": "input.txt", "start": 1, "end": 1}])])
        first, _ = self._run(plan, run_id="r1")
        self.assertEqual("SUCCESS", first["nodes"]["A1"]["status"])
        (self.root / "input.txt").write_text("input v2 changed\n", encoding="utf-8")  # same HEAD
        report, backend = self._run(plan, run_id="r1", resume=True)
        self.assertIn("REFUSE_RESUME_INPUT_CHANGED", report.get("error", ""))
        self.assertEqual([], backend.calls)  # zero launches, stale SUCCESS never reused

    def test_resume_missing_state_fails_closed(self) -> None:
        plan = _plan([_ai("A1", "review")])
        report, backend = self._run(plan, run_id="never-ran", resume=True)
        self.assertIn("REFUSE_RESUME_STATE_MISSING", report.get("error", ""))
        self.assertEqual([], backend.calls)

    def test_resume_corrupt_state_fails_closed(self) -> None:
        run_dir = self.state / "orchestration" / "bad"
        run_dir.mkdir(parents=True)
        (run_dir / "state.json").write_text("{not valid json", encoding="utf-8")
        plan = _plan([_ai("A1", "review")])
        report, backend = self._run(plan, run_id="bad", resume=True)
        self.assertIn("REFUSE_RESUME_STATE_INVALID", report.get("error", ""))
        self.assertEqual([], backend.calls)

    def test_changed_execution_policy_refuses_resume(self) -> None:
        plan = _plan([_ai("A1", "review")])
        first, _ = self._run(plan, run_id="r1", constraints={"local_only": True})
        self.assertEqual("SUCCESS", first["nodes"]["A1"]["status"])
        report, backend = self._run(plan, run_id="r1", resume=True, constraints={"no_premium": True})
        self.assertIn("REFUSE_RESUME_POLICY_CHANGED", report.get("error", ""))
        self.assertEqual([], backend.calls)


# --------------------------------------------------------------------------- #
# 3. Uncertain mutation cannot auto-replay
# --------------------------------------------------------------------------- #
class MutationUncertaintyTests(Fixture):
    def test_mutation_intent_marker_persisted_before_launch(self) -> None:
        captured = {}
        state = self.state

        class Probe(execution.Backend):
            name = "probe"
            mode = "SUPPORTED"

            def available(self):
                return True

            def execute(self, request):
                path = state / "orchestration" / "r1" / "state.json"
                captured["status"] = json.loads(path.read_text(encoding="utf-8"))["nodes"]["A1"]["status"]
                return {"task_id": request["task_id"], "status": "SUCCESS",
                        "completion_signal": "agent_reported", "acceptance_results": _accept("ok")}

        plan = _plan([_ai("A1", "apply the patch", intent="MUTATING")], intent="MUTATING")
        self._run(plan, backend=Probe(), run_id="r1")
        self.assertEqual("RUNNING", captured["status"])  # durable intent precedes the launch

    def test_workspace_mutated_then_lost_completion_refuses_replay(self) -> None:
        plan = _plan([_ai("A1", "apply the patch", intent="MUTATING")], intent="MUTATING")
        self._run(plan, run_id="r1")  # produces durable state
        state_path = self.state / "orchestration" / "r1" / "state.json"
        blob = json.loads(state_path.read_text(encoding="utf-8"))
        blob["nodes"]["A1"] = {"status": "RUNNING", "task_id": "A1"}  # crashed before record
        state_path.write_text(json.dumps(blob), encoding="utf-8")
        (self.root / "mutated.txt").write_text("side effect", encoding="utf-8")
        report, backend = self._run(plan, run_id="r1", resume=True)
        self.assertIn("REFUSE_RESUME_UNCERTAIN_MUTATION", report.get("error", ""))
        self.assertEqual([], backend.calls)  # no automatic replay of a mutation


# --------------------------------------------------------------------------- #
# 5 + 6 + 7. Strict acceptance types
# --------------------------------------------------------------------------- #
class StrictAcceptanceTests(Fixture):
    def _status(self, criteria, acceptance):
        backend = execution.FakeBackend(script={"A1": [
            {"status": "SUCCESS", "completion_signal": "agent_reported", "acceptance_results": acceptance}]})
        report, _ = self._run(_plan([_ai("A1", "review", acceptance=criteria)]), backend=backend)
        return report["nodes"]["A1"]["status"], report["nodes"]["A1"].get("failure_category")

    def test_string_false_is_not_pass(self) -> None:
        status, _ = self._status(("ok",), {"ok": "false"})
        self.assertNotEqual("SUCCESS", status)
        self.assertEqual("UNVERIFIED", status)

    def test_string_true_is_not_pass_without_validator_contract(self) -> None:
        status, _ = self._status(("ok",), {"ok": "true"})
        self.assertEqual("UNVERIFIED", status)

    def test_boolean_false_with_empty_criteria_is_not_vacuous_pass(self) -> None:
        status, _ = self._status((), {"passed": False})
        self.assertNotEqual("SUCCESS", status)
        self.assertEqual("UNVERIFIED", status)

    def test_boolean_false_with_criterion_fails(self) -> None:
        status, category = self._status(("passed",), {"passed": False})
        self.assertEqual("FAILURE", status)
        self.assertEqual("ACCEPTANCE_FAILED", category)

    def test_typed_boolean_true_still_passes(self) -> None:
        status, _ = self._status(("ok",), {"ok": True})
        self.assertEqual("SUCCESS", status)

    def test_model_self_claim_without_validator_is_unverified(self) -> None:
        backend = execution.FakeBackend(script={"A1": [
            {"status": "SUCCESS", "completion_signal": "agent_reported", "acceptance_results": {},
             "summary": "I passed. All good."}]})
        report, _ = self._run(_plan([_ai("A1", "review")]), backend=backend)
        self.assertEqual("UNVERIFIED", report["nodes"]["A1"]["status"])


# --------------------------------------------------------------------------- #
# 8. Human acceptance binds to exact identities
# --------------------------------------------------------------------------- #
class HumanAcceptanceTests(Fixture):
    def test_binding_and_staleness(self) -> None:
        entry = human_acceptance.record(run_id="r", task_id="A1", decision=human_acceptance.ACCEPTED,
                                        reviewer="alice", task_contract_identity="c1",
                                        delivered_input_identity="i1", result_identity="res1",
                                        state_dir=self.state)
        self.assertEqual("ACCEPTED", entry["decision"])
        self.assertTrue(human_acceptance.verify(entry, task_contract_identity="c1",
                                                delivered_input_identity="i1", result_identity="res1"))
        # Any bound identity change (stale approval) is refused.
        self.assertFalse(human_acceptance.verify(entry, task_contract_identity="c1",
                                                 delivered_input_identity="i2", result_identity="res1"))
        self.assertFalse(human_acceptance.verify(entry, task_contract_identity="c1",
                                                 delivered_input_identity="i1", result_identity="res2"))
        self.assertFalse(human_acceptance.is_accepted(run_id="r", task_id="A1",
                                                      task_contract_identity="c1",
                                                      delivered_input_identity="i1",
                                                      result_identity="res2", state_dir=self.state))

    def test_cli_records_acceptance_for_a_real_run(self) -> None:
        plan = _plan([_ai("A1", "architecture review of input",
                          refs=[{"path": "input.txt", "start": 1, "end": 1}], acceptance=("ok",))])
        self._run(plan, run_id="cli1")
        script = ROOT / "scripts" / "orchestrate"
        env = os.environ | {"LAI_PYTHON": sys.executable}
        result = subprocess.run([str(script), "accept", "--run-id", "cli1", "--task-id", "A1",
                                 "--reviewer", "bob", "--accept", "--state-dir", str(self.state), "--json"],
                                capture_output=True, text=True, env=env)
        self.assertEqual(0, result.returncode, result.stderr)
        entry = human_acceptance.load(run_id="cli1", task_id="A1", state_dir=self.state)
        self.assertIsNotNone(entry)
        self.assertEqual("ACCEPTED", entry["decision"])
        self.assertEqual("bob", entry["reviewer"])
        self.assertTrue(entry["result_identity"])


# --------------------------------------------------------------------------- #
# 9 + 10. Complete task requirement coverage
# --------------------------------------------------------------------------- #
class CoverageTests(unittest.TestCase):
    def test_compound_objective_keeps_every_requirement(self) -> None:
        plan = orchestrator.decompose("Fix alpha to return 17 and beta to return 99; update docs; run tests")
        blob = " ".join(node["objective_summary"] for node in plan["nodes"]).lower()
        for token in ("alpha", "17", "beta", "99", "docs", "test"):
            self.assertIn(token, blob, token)

    def test_partial_deterministic_objective_is_not_zero_ai(self) -> None:
        plan = orchestrator.decompose("Check current HEAD and calculate 2 plus 2")
        self.assertTrue(any(node.get("executor") == "AI" for node in plan["nodes"]))
        blob = " ".join(node["objective_summary"] for node in plan["nodes"]).lower()
        self.assertIn("calculate", blob)
        # Integration: it cannot be completed as a zero-AI deterministic run.
        with tempfile.TemporaryDirectory() as tmp:
            root = _repo(pathlib.Path(tmp) / "repo")
            backend = execution.FakeBackend()
            report = execution.execute_plan(plan, backend=backend, state_dir=pathlib.Path(tmp) / "state",
                                            repo_root=root, confirm=True, allow_dirty=True,
                                            record_evidence=False)
            self.assertGreaterEqual(report["calls_used"], 1)


# --------------------------------------------------------------------------- #
# 11. Mutation cache safety is non-overridable
# --------------------------------------------------------------------------- #
class MutationCacheTests(Fixture):
    def test_input_bound_override_cannot_reuse_mutation(self) -> None:
        node = _ai("A1", "apply the patch", intent="MUTATING")
        node["freshness_policy"] = rc.FRESHNESS_INPUT_BOUND
        self.assertEqual(rc.FRESHNESS_RUNTIME_VOLATILE, rc.freshness_policy(node))
        ok, reason = rc.eligible_for_cache(node, {"status": "SUCCESS", "acceptance_results": _accept("ok")},
                                           freshness=rc.FRESHNESS_INPUT_BOUND)
        self.assertFalse(ok)

    def test_mutation_never_cache_hits_end_to_end(self) -> None:
        plan = _plan([_ai("A1", "apply the patch", intent="MUTATING")], intent="MUTATING")
        cold, _ = self._run(plan, cache=True)
        self.assertEqual(0, cold["cache"]["writes"])
        warm, backend = self._run(plan, cache=True)
        self.assertEqual(0, warm["cache"]["hits"])
        self.assertEqual(1, len(backend.calls))  # executed again, no stale reuse
        reasons = [d["reason"] for d in warm["cache"]["decisions"] if d.get("decision") == "RERUN"]
        self.assertIn(rc.REASON_MUTATION_UNVERIFIED, reasons)


# --------------------------------------------------------------------------- #
# 12 + 13. Learning denominators
# --------------------------------------------------------------------------- #
class LearningDenominatorTests(unittest.TestCase):
    def test_unverified_population_blocks_lower_effort(self) -> None:
        rows = [_row(final_tier="BALANCED", final_effort="LOW") for _ in range(10)]
        rows += [_row(final_tier="BALANCED", final_effort="LOW", outcome="UNKNOWN") for _ in range(90)]
        kinds = {c["type"] for c in outcomes.candidates(rows)["candidates"]}
        self.assertNotIn("LOWER_EFFORT_SUFFICIENT", kinds)

    def test_unverified_population_blocks_cheaper_tier(self) -> None:
        rows = [_row(final_tier="FAST", final_effort="LOW") for _ in range(10)]
        rows += [_row(final_tier="FAST", final_effort="LOW", outcome="UNKNOWN") for _ in range(90)]
        kinds = {c["type"] for c in outcomes.candidates(rows)["candidates"]}
        self.assertNotIn("CHEAPER_TIER_SUFFICIENT", kinds)

    def test_unverified_population_blocks_model_variant_effective(self) -> None:
        rows = [_row(provider="p", model="good", native_variant="high") for _ in range(10)]
        rows += [_row(provider="p", model="good", native_variant="high", outcome="UNKNOWN")
                 for _ in range(90)]
        kinds = {c["type"] for c in outcomes.candidates(rows)["candidates"]}
        self.assertNotIn("MODEL_VARIANT_EFFECTIVE", kinds)

    def test_clean_evidence_still_qualifies(self) -> None:
        rows = [_row(final_tier="BALANCED", final_effort="LOW") for _ in range(6)]
        kinds = {c["type"] for c in outcomes.candidates(rows)["candidates"]}
        self.assertIn("LOWER_EFFORT_SUFFICIENT", kinds)


# --------------------------------------------------------------------------- #
# 14 + 15. Context budget contracts
# --------------------------------------------------------------------------- #
class ContextBudgetContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.budgets = dict(ctx.DEFAULT_BUDGETS)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _request(self, **kw):
        base = dict(task_id="T1", task_family="f", task_class="NORMAL", executor_type="AI",
                    context_budget="LARGE", objective_summary="o", acceptance_criteria=["ok"])
        base.update(kw)
        return ctx.ContextRequest.build(**base)

    def test_required_truncation_is_not_context_ready(self) -> None:
        (self.root / "big.txt").write_text("y" * (ctx.MAX_ITEM_CHARS + 5000), encoding="utf-8")
        result = ctx.build_pack(self._request(source_refs=[{"path": "big.txt", "start": 1, "end": 1,
                                                            "required": True}]),
                                root=self.root, budgets=self.budgets)
        self.assertNotEqual(ctx.STATUS_READY, result["status"])
        self.assertTrue(result["manifest"]["required_truncated"])

    def test_optional_items_never_exceed_budget(self) -> None:
        required = {"priority": 5, "size": 100, "required": True}
        optional = {"priority": 0, "size": 50, "required": False}
        kept, omitted, fits = ctx.enforce_budget([optional, required], "SMALL", {"SMALL": 120})
        self.assertTrue(fits)
        self.assertLessEqual(sum(i["size"] for i in kept), 120)
        self.assertIn(optional, omitted)

    def test_build_pack_optional_cannot_overflow(self) -> None:
        (self.root / "opt.txt").write_text("z" * 2000, encoding="utf-8")
        result = ctx.build_pack(self._request(context_budget="TINY",
                                              source_refs=[{"path": "opt.txt", "start": 1, "end": 1,
                                                            "required": False}]),
                                root=self.root, budgets={"TINY": 300, "SMALL": 4000, "MEDIUM": 40000,
                                                         "LARGE": 120000})
        self.assertLessEqual(result["manifest"]["size"], 300)


# --------------------------------------------------------------------------- #
# 16 + 17. Retrieval error and final delivered budget
# --------------------------------------------------------------------------- #
class DeliveredInputContractTests(Fixture):
    def test_retrieval_exception_stops_before_launch(self) -> None:
        plan = _plan([_ai("A1", "review")])
        with unittest.mock.patch.object(execution.context_retrieval, "build_pack",
                                        side_effect=ctx.ContextError("boom")):
            report, backend = self._run(plan)
        self.assertEqual([], backend.calls)
        self.assertEqual(0, report["calls_used"])
        self.assertEqual("FAILURE", report["nodes"]["A1"]["status"])

    def test_final_delivered_input_budget_enforced(self) -> None:
        limit = ctx.load_budgets()["TINY"]
        (self.root / "huge.txt").write_text("w" * (limit - 200), encoding="utf-8")
        plan = _plan([_ai("A1", "inspect huge", refs=[{"path": "huge.txt", "start": 1, "end": 1}],
                          budget="TINY")])
        report, backend = self._run(plan)
        self.assertEqual([], backend.calls)  # failed closed before any launch
        self.assertEqual(0, report["calls_used"])
        self.assertEqual("CONTEXT_BUDGET_INSUFFICIENT", report["nodes"]["A1"]["failure_category"])


# --------------------------------------------------------------------------- #
# 18 + 19 + 20. Canonical delivered semantics + authority boundary
# --------------------------------------------------------------------------- #
class CanonicalSemanticsTests(unittest.TestCase):
    def _node(self, *, summary="A", contract):
        node = _ai("A1", summary)
        node["task_contract"] = contract
        return node

    def test_constraints_change_identity_and_prompt(self) -> None:
        first = self._node(contract={"objective": "A", "operation": "execute", "targets": [],
                                     "constraints": ["intent:READ_ONLY"], "permissions": "READ_ONLY"})
        second = self._node(contract={"objective": "A", "operation": "execute", "targets": [],
                                      "constraints": ["intent:READ_ONLY", "local_only"],
                                      "permissions": "READ_ONLY"})
        self.assertNotEqual(execution.delivered_input_fingerprint(first, [], None),
                            execution.delivered_input_fingerprint(second, [], None))
        prompt_first = execution._prompt(first, [], manifest=None)
        prompt_second = execution._prompt(second, [], manifest=None)
        self.assertNotEqual(prompt_first, prompt_second)
        self.assertIn("local_only", prompt_second)

    def test_conflicting_objective_fields_one_canonical_semantics(self) -> None:
        node = self._node(summary="SUMMARY_WRONG",
                          contract={"objective": "CONTRACT_RIGHT", "operation": "execute",
                                    "targets": [], "constraints": [], "permissions": "READ_ONLY"})
        prompt = execution._prompt(node, [], manifest=None)
        self.assertIn("Objective: CONTRACT_RIGHT", prompt)
        self.assertNotIn("SUMMARY_WRONG", prompt)

    def test_retrieved_data_authority_delimiter_present(self) -> None:
        pack = {"objective_summary": "o", "acceptance_criteria": ["ok"],
                "items": [{"source_kind": "FILE_RANGE", "source_id": "a.txt", "content": "data"}]}
        node = self._node(summary="o", contract={"objective": "o", "operation": "execute",
                                                 "targets": [], "constraints": [], "permissions": "READ_ONLY"})
        prompt = execution._prompt(node, [], context_pack=pack, manifest=None)
        self.assertIn(execution.TASK_AUTHORITY_HEADER, prompt)
        self.assertIn(execution.DATA_AUTHORITY_HEADER, prompt)
        self.assertIn(execution.DATA_AUTHORITY_NOTE, prompt)


# --------------------------------------------------------------------------- #
# 21. Failover tier/effort attribution
# --------------------------------------------------------------------------- #
class AttributionTests(Fixture):
    def test_escalation_evidence_matches_actual_accepted_candidate(self) -> None:
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
            execution.execute_plan(plan, backend=accepted, backends={"opencode": accepted, "codex": failed},
                                   health=health, constraints={}, repo_root=self.root, confirm=True,
                                   record_evidence=True, infra_retries=0, max_ai_calls=3,
                                   state_dir=self.state, allow_dirty=True)
        runtime = [r for r in evidence.all_rows(state_dir=self.state) if r["source"] == "observed_runtime"]
        self.assertEqual(1, len(runtime))
        row = runtime[0]
        self.assertEqual("opencode", row["surface"])
        self.assertEqual("deepseek", row["provider"])
        self.assertEqual("deepseek-strong", row["model"])
        self.assertEqual("max", row["native_variant"])
        self.assertEqual("MAX", row["normalized_effort"])
        self.assertEqual("STRONG", row["final_tier"])
        self.assertEqual("BALANCED", row["initial_tier"])


# --------------------------------------------------------------------------- #
# 22. Complete persisted-result redaction
# --------------------------------------------------------------------------- #
class PersistedRedactionTests(Fixture):
    def test_nested_secret_is_redacted_in_persisted_state(self) -> None:
        secret = "sk-" + "a" * 30
        backend = execution.FakeBackend(script={"A1": [
            {"status": "SUCCESS", "completion_signal": "agent_reported", "acceptance_results": _accept("ok"),
             "summary": f"used token {secret}",
             "recommended_followups": [{"step": f"rotate {secret}"}, ["nested", f"key={secret}"]]}]})
        self._run(_plan([_ai("A1", "review")]), backend=backend, run_id="redact")
        text = (self.state / "orchestration" / "redact" / "state.json").read_text(encoding="utf-8")
        self.assertNotIn(secret, text)
        self.assertIn("[REDACTED]", text)


# --------------------------------------------------------------------------- #
# 23 + 24. Accounting
# --------------------------------------------------------------------------- #
class AccountingRegressionTests(Fixture):
    def test_read_only_violation_records_attempt_row(self) -> None:
        workspace = self.root

        class Mutating(execution.Backend):
            name = "mutator"
            mode = "SUPPORTED"

            def available(self):
                return True

            def execute(self, request):
                (workspace / "injected.txt").write_text("x", encoding="utf-8")
                return {"task_id": request["task_id"], "status": "SUCCESS",
                        "completion_signal": "agent_reported", "acceptance_results": _accept("ok")}

        plan = _plan([_ai("A1", "inspect", intent="READ_ONLY")])
        report, _ = self._run(plan, backend=Mutating())
        self.assertEqual("READ_ONLY_VIOLATION", report["error"])
        self.assertEqual(1, report["calls_used"])
        attempts = report["attempts"]
        self.assertEqual(1, len(attempts))
        self.assertEqual("FAILED", attempts[0]["status"])
        self.assertEqual("READ_ONLY_VIOLATION", attempts[0]["failure_category"])

    def test_no_actual_planner_invocation_counts_zero(self) -> None:
        plan = _plan([_ai("A1", "review")], planner_mode="AI")
        report, _ = self._run(plan)
        self.assertEqual(0, report["accounting"]["planner_calls"])


if __name__ == "__main__":
    unittest.main()
