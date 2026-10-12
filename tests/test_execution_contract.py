"""Execution Contract Stabilization V0.1 regression tests.

Deterministic, fake/stub adapters only, temporary repositories, zero real model or
provider calls. Covers the stabilization contract families A-K:
task/context propagation, acceptance, context identity, redaction, cache boundary,
learning provenance, resume, status persistence, workspace authority, deterministic
intent and invocation accounting.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
import unittest.mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import context_retrieval as ctx  # noqa: E402
import deterministic  # noqa: E402
import evidence  # noqa: E402
import execution  # noqa: E402
import failover  # noqa: E402
import orchestration_store as store  # noqa: E402
import orchestrator  # noqa: E402
import outcomes  # noqa: E402
import result_cache as rc  # noqa: E402


BUDGETS = {"TINY": 4000, "SMALL": 12000, "MEDIUM": 40000, "LARGE": 120000}


def _repo(tmp: pathlib.Path) -> pathlib.Path:
    tmp.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=tmp, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.name", "t"], check=True)
    (tmp / "notes.txt").write_text("architecture notes v1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp), "commit", "-m", "init"], check=True, capture_output=True)
    return tmp


def _accept(*criteria) -> dict:
    return {c: True for c in (criteria or ("ok",))}


def _ai(task_id, title, *, deps=None, refs=None, intent="READ_ONLY", acceptance=("ok",),
        family="generic", task_class="NORMAL", contract=None):
    node = {"task_id": task_id, "title": title, "objective_summary": title, "task_family": family,
            "task_class": task_class, "depends_on": list(deps or []), "conditional": False,
            "intent": intent, "executor": "AI",
            "routing": {"tier": "BALANCED", "effort": "medium", "role": "student"},
            "context": {"budget": "SMALL"}, "acceptance_criteria": list(acceptance),
            "risk_signals": [], "knowledge_refs": [], "context_refs": list(refs or [])}
    if contract is not None:
        node["task_contract"] = contract
    return node


def _plan(nodes, *, intent="READ_ONLY"):
    return {"schema_version": 1, "valid": True, "intent": intent,
            "order": [n["task_id"] for n in nodes], "nodes": nodes}


class ContractFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _repo(pathlib.Path(self.tmp.name) / "repo")
        self.state = pathlib.Path(self.tmp.name) / "state"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, plan, backend=None, **kw):
        kw.setdefault("confirm", True)
        kw.setdefault("allow_dirty", True)
        kw.setdefault("record_evidence", False)
        backend = backend or execution.FakeBackend()
        report = execution.execute_plan(plan, backend=backend, state_dir=kw.pop("state_dir", self.state),
                                        repo_root=kw.pop("repo_root", self.root), **kw)
        return report, backend


# --------------------------------------------------------------------------- #
# A. Task -> worker propagation
# --------------------------------------------------------------------------- #
class TaskPropagationTests(ContractFixture):
    def test_distinct_objectives_produce_distinct_worker_inputs(self) -> None:
        prompts = {}
        for objective in ("Fix alpha to return 17", "Fix beta to return 99"):
            tmp = tempfile.TemporaryDirectory()
            root = _repo(pathlib.Path(tmp.name) / "repo")
            backend = execution.FakeBackend()
            execution.execute_plan(orchestrator.decompose(objective), backend=backend,
                                   state_dir=pathlib.Path(tmp.name) / "state", repo_root=root,
                                   confirm=True, allow_dirty=True, record_evidence=False)
            prompts[objective] = " ".join(call["prompt"] for call in backend.calls)
            tmp.cleanup()
        self.assertIn("alpha", prompts["Fix alpha to return 17"])
        self.assertIn("beta", prompts["Fix beta to return 99"])
        self.assertNotEqual(prompts["Fix alpha to return 17"], prompts["Fix beta to return 99"])

    def test_targets_and_context_reach_worker_input(self) -> None:
        plan = _plan([_ai("A1", "inspect notes", refs=[{"path": "notes.txt", "start": 1, "end": 5}],
                          contract={"contract_version": "task-contract-v1", "operation": "inspect",
                                    "objective": "inspect notes", "targets": ["notes.txt"]})])
        report, backend = self._run(plan)
        prompt = backend.calls[0]["prompt"]
        self.assertIn("notes.txt", prompt)
        self.assertIn("architecture notes v1", prompt)  # delivered context reaches the prompt
        self.assertIn("Operation: inspect", prompt)

    def test_context_marker_reaches_opencode_and_codex_adapters(self) -> None:
        plan = _plan([_ai("A1", "inspect notes", refs=[{"path": "notes.txt", "start": 1, "end": 5}])])
        report, backend = self._run(plan)
        prompt = backend.calls[0]["prompt"]
        self.assertIn("architecture notes v1", prompt)
        for name in ("opencode", "codex"):
            command = execution.CliBackend(name, name)._command(
                {"prompt": prompt, "model": "m", "provider": "p", "native_variant": "high",
                 "working_dir": str(self.root), "read_only": False})
            self.assertIn(prompt, command)


# --------------------------------------------------------------------------- #
# B. Acceptance failure despite process exit 0
# --------------------------------------------------------------------------- #
class AcceptanceTests(ContractFixture):
    def _run_output(self, summary: str):
        captured = {}

        class Prose(execution.Backend):
            name = "prose"
            mode = "SUPPORTED"

            def available(self):
                return True

            def execute(self, request):
                captured["request"] = request
                return {"task_id": request["task_id"], "status": "SUCCESS",
                        "completion_signal": "agent_reported", "summary": summary,
                        "acceptance_results": {}, "files_changed": [], "decisions": [],
                        "new_risk_signals": []}

        report, _ = self._run(_plan([_ai("A1", "review")]), backend=Prose())
        return report, captured["request"]

    def test_prose_review_is_not_success(self) -> None:
        report, _ = self._run_output("I reviewed it.")
        self.assertEqual("UNVERIFIED", report["nodes"]["A1"]["status"])
        self.assertEqual("ACCEPTANCE_UNVERIFIED", report["nodes"]["A1"]["failure_category"])

    def test_refusal_prose_is_not_success(self) -> None:
        report, _ = self._run_output("I cannot complete the requested work.")
        self.assertNotEqual("SUCCESS", report["nodes"]["A1"]["status"])

    def test_false_acceptance_claim_fails(self) -> None:
        backend = execution.FakeBackend(script={"A1": [
            {"status": "SUCCESS", "completion_signal": "agent_reported",
             "acceptance_results": {"ok": False}}]})
        report, _ = self._run(_plan([_ai("A1", "review")]), backend=backend)
        self.assertEqual("FAILURE", report["nodes"]["A1"]["status"])
        self.assertEqual("ACCEPTANCE_FAILED", report["nodes"]["A1"]["failure_category"])

    def test_malformed_structured_result_is_unverified(self) -> None:
        backend = execution.FakeBackend(script={"A1": [
            {"status": "SUCCESS", "completion_signal": "agent_reported", "acceptance_results": "not-a-map"}]})
        report, _ = self._run(_plan([_ai("A1", "review")]), backend=backend)
        self.assertEqual("UNVERIFIED", report["nodes"]["A1"]["status"])


# --------------------------------------------------------------------------- #
# C + D. Context identity / redaction
# --------------------------------------------------------------------------- #
class ContextIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "src.txt").write_text("l1\nl2\nl3\nl4\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _pack(self):
        request = ctx.ContextRequest.build(
            task_id="T1", task_family="f", task_class="NORMAL", executor_type="AI",
            context_budget="SMALL", objective_summary="o", acceptance_criteria=[],
            source_refs=[{"path": "src.txt", "start": 1, "end": 2},
                         {"path": "src.txt", "start": 3, "end": 4}])
        return ctx.build_pack(request, root=self.root, budgets=BUDGETS)

    def test_merged_range_content_change_changes_identity(self) -> None:
        first = self._pack()
        (self.root / "src.txt").write_text("l1\nl2\nl3CHANGED\nl4\n", encoding="utf-8")
        second = self._pack()
        item_a = [i for i in first["items"] if i["source_kind"] == "FILE_RANGE"][0]
        item_b = [i for i in second["items"] if i["source_kind"] == "FILE_RANGE"][0]
        self.assertEqual([1, 4], item_b["range"])
        self.assertNotEqual(item_a["content"], item_b["content"])
        self.assertNotEqual(item_a["fingerprint"], item_b["fingerprint"])
        self.assertNotEqual(first["manifest"]["manifest_fingerprint"],
                            second["manifest"]["manifest_fingerprint"])
        self.assertNotEqual(rc.context_identity(first["manifest"]), rc.context_identity(second["manifest"]))

    def test_secret_cannot_reappear_through_merge(self) -> None:
        (self.root / "sec.txt").write_text("safe\napi_key=SuperSecret12345\nmore\n", encoding="utf-8")
        request = ctx.ContextRequest.build(
            task_id="T1", task_family="f", task_class="NORMAL", executor_type="AI",
            context_budget="SMALL", objective_summary="o", acceptance_criteria=[],
            source_refs=[{"path": "sec.txt", "start": 1, "end": 2},
                         {"path": "sec.txt", "start": 2, "end": 3}])
        result = ctx.build_pack(request, root=self.root, budgets=BUDGETS)
        self.assertIn(ctx.REDACTED, json.dumps(result["pack"]))
        self.assertNotIn("SuperSecret12345", json.dumps(result["pack"]))
        self.assertNotIn("SuperSecret12345", json.dumps(result["manifest"]))


# --------------------------------------------------------------------------- #
# E. Cache boundary
# --------------------------------------------------------------------------- #
class CacheBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = pathlib.Path(self.tmp.name) / "state"
        self.store = rc.ResultCache(state_dir=self.state)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def _node(self, objective="analyze the configuration", intent="READ_ONLY"):
        node = _ai("T1", objective)
        node["intent"] = intent
        return node

    def _identity(self, node):
        semantic = rc.task_semantic_fingerprint(node)
        fp = rc.input_fingerprint(project_id="remote:x", task_semantic_fingerprint=semantic,
                                  context_fingerprint="ctx", dependency_fingerprints=[],
                                  freshness_policy=rc.FRESHNESS_INPUT_BOUND)
        return {"task_semantic_fingerprint": semantic, "context_manifest_fingerprint": "ctx",
                "input_fingerprint": fp}

    def _seed(self, node, identity, provenance=rc.PROVENANCE_REAL, acceptance=("ok",)):
        bounded = rc.bounded_result(node, {"status": "SUCCESS", "summary": "done",
                                           "acceptance_results": _accept(*acceptance)})
        self.store.write(project_id="remote:x",
                         task_semantic_fingerprint=identity["task_semantic_fingerprint"],
                         input_fingerprint=identity["input_fingerprint"],
                         context_manifest_fingerprint=identity["context_manifest_fingerprint"],
                         result_fingerprint=rc.result_fingerprint(rc.result_type(node), bounded),
                         result_type=rc.result_type(node), result=bounded,
                         freshness_policy=rc.FRESHNESS_INPUT_BOUND,
                         original_required_tier="BALANCED", original_required_effort="medium",
                         original_task_id="T1", result_provenance=provenance)

    def test_simulation_never_satisfies_real_lookup(self) -> None:
        node = self._node()
        identity = self._identity(node)
        self._seed(node, identity, provenance=rc.PROVENANCE_SIMULATED)
        decision = rc.decide(store=self.store, project_id="remote:x", node=node, identity=identity,
                             required_tier="BALANCED", required_effort="medium",
                             freshness=rc.FRESHNESS_INPUT_BOUND, provenance=rc.PROVENANCE_REAL)
        self.assertEqual(rc.RERUN, decision["decision"])
        self.assertEqual(rc.REASON_SIMULATION_MISMATCH, decision["reason"])

    def test_real_never_satisfies_simulated_lookup_either(self) -> None:
        node = self._node()
        identity = self._identity(node)
        self._seed(node, identity, provenance=rc.PROVENANCE_REAL)
        decision = rc.decide(store=self.store, project_id="remote:x", node=node, identity=identity,
                             required_tier="BALANCED", required_effort="medium",
                             freshness=rc.FRESHNESS_INPUT_BOUND, provenance=rc.PROVENANCE_SIMULATED)
        self.assertEqual(rc.REASON_SIMULATION_MISMATCH, decision["reason"])

    def test_changed_objective_misses(self) -> None:
        node = self._node(objective="analyze the configuration")
        self._seed(node, self._identity(node))
        other = self._node(objective="analyze the routing")
        self.assertNotEqual(rc.task_semantic_fingerprint(node), rc.task_semantic_fingerprint(other))

    def test_changed_acceptance_version_misses(self) -> None:
        node = self._node()
        self.assertNotEqual(rc.task_semantic_fingerprint(node, acceptance_contract="acceptance-v2"),
                            rc.task_semantic_fingerprint(node, acceptance_contract="acceptance-v3"))

    def test_unverified_mutation_is_never_reusable(self) -> None:
        node = self._node(intent="MUTATING")
        self.assertEqual(rc.FRESHNESS_RUNTIME_VOLATILE, rc.freshness_policy(node))
        ok, _ = rc.eligible_for_cache(node, {"status": "SUCCESS", "acceptance_results": _accept("ok")},
                                      freshness=rc.FRESHNESS_RUNTIME_VOLATILE)
        self.assertFalse(ok)

    def test_invalid_postcondition_not_reusable(self) -> None:
        node = self._node()
        self.assertIsNone(rc.bounded_result(node, {"status": "SUCCESS",
                                                   "acceptance_results": {"ok": False}}))

    def test_legacy_schema_entries_are_inert(self) -> None:
        node = self._node()
        identity = self._identity(node)
        self._seed(node, identity)
        self.store.connection.execute("UPDATE cache_entries SET schema_version=1")
        self.store.connection.commit()
        decision = rc.decide(store=self.store, project_id="remote:x", node=node, identity=identity,
                             required_tier="BALANCED", required_effort="medium",
                             freshness=rc.FRESHNESS_INPUT_BOUND)
        self.assertEqual(rc.REASON_CACHE_INVALID, decision["reason"])


# --------------------------------------------------------------------------- #
# F. Learning provenance
# --------------------------------------------------------------------------- #
class LearningProvenanceTests(ContractFixture):
    def test_unaccepted_work_creates_no_positive_evidence(self) -> None:
        backend = execution.FakeBackend(script={"A1": [
            {"status": "SUCCESS", "completion_signal": "agent_reported", "acceptance_results": {},
             "summary": "I reviewed it."}]})
        self._run(_plan([_ai("A1", "review")]), backend=backend, record_evidence=True)
        rows = evidence.all_rows(state_dir=self.state)
        self.assertTrue(rows)
        self.assertFalse(any(row["outcome"] == "SUCCESS" for row in rows))
        self.assertTrue(all(row["outcome"] == "UNKNOWN" for row in rows))

    def test_simulated_success_is_not_production_quality_evidence(self) -> None:
        self._run(_plan([_ai("A1", "review")]), record_evidence=True)
        rows = evidence.all_rows(state_dir=self.state)
        self.assertTrue(any(row["outcome"] == "SUCCESS" for row in rows))  # recorded locally
        self.assertEqual([], outcomes.model_performance(state_dir=self.state))  # not production

    def test_failover_credits_the_actual_accepted_candidate(self) -> None:
        # READ_ONLY: infra failover is allowed to a capability-preserving candidate. A
        # launched MUTATING node now fails closed instead (see pass-4 mutation tests).
        plan = _plan([_ai("A1", "analyze")])
        failed = execution.FakeBackend(script={"A1": [{"task_id": "A1", "status": "FAILURE",
                                                       "failure_category": "RATE_LIMIT"}]})
        failed.name = "codex"
        accepted = execution.FakeBackend(script={"A1": [{"task_id": "A1", "status": "SUCCESS",
                                                         "completion_signal": "agent_reported",
                                                         "acceptance_results": _accept("ok")}]})
        accepted.name = "opencode"
        health = failover.Health(binaries={"codex": True, "opencode": True},
                                 providers={"codex": {"codex"}, "opencode": {"deepseek"}},
                                 codex_model="gpt-x")
        specs = [{"backend": "codex", "provider": "codex", "model": "gpt-x", "native_variant": "high",
                  "tier": "BALANCED", "effort": "medium"},
                 {"backend": "opencode", "provider": "deepseek", "model": "deepseek-flash",
                  "native_variant": "medium", "tier": "BALANCED", "effort": "medium"}]
        with unittest.mock.patch.object(execution.failover, "candidates_for_tier", return_value=specs):
            execution.execute_plan(plan, backend=accepted, backends={"opencode": accepted, "codex": failed},
                                   health=health, constraints={}, repo_root=self.root, confirm=True,
                                   record_evidence=True, infra_retries=0, max_ai_calls=3,
                                   state_dir=self.state, allow_dirty=True)
        runtime = [r for r in evidence.all_rows(state_dir=self.state) if r["source"] == "observed_runtime"]
        self.assertEqual(1, len(runtime))
        self.assertEqual("opencode", runtime[0]["surface"])   # actual accepted candidate, not codex
        self.assertEqual("deepseek", runtime[0]["provider"])
        self.assertEqual("deepseek-flash", runtime[0]["model"])

    def test_cache_hit_creates_no_fresh_model_evidence(self) -> None:
        self._run(_plan([_ai("A1", "review")]), record_evidence=False, cache=True)
        warm, backend = self._run(_plan([_ai("A1", "review")]), record_evidence=True, cache=True)
        self.assertEqual([], backend.calls)
        runtime = [r for r in evidence.all_rows(state_dir=self.state) if r["source"] == "observed_runtime"]
        self.assertEqual([], runtime)


# --------------------------------------------------------------------------- #
# G. Resume
# --------------------------------------------------------------------------- #
class ResumeSafetyTests(ContractFixture):
    def test_same_head_different_objective_refuses(self) -> None:
        first = _plan([_ai("A1", "Fix alpha to return 17", intent="MUTATING")], intent="MUTATING")
        self._run(first, run_id="r1")
        other = _plan([_ai("A1", "Fix beta to return 99", intent="MUTATING")], intent="MUTATING")
        report, _ = self._run(other, run_id="r1", resume=True)
        self.assertIn("REFUSE_RESUME", report.get("error", ""))

    def test_changed_task_contract_refuses(self) -> None:
        first = _plan([_ai("A1", "do work", contract={"contract_version": "task-contract-v1",
                                                      "objective": "do work", "operation": "execute"})])
        self._run(first, run_id="r1")
        changed = _plan([_ai("A1", "do work", contract={"contract_version": "task-contract-v1",
                                                        "objective": "do other work", "operation": "execute"})])
        report, _ = self._run(changed, run_id="r1", resume=True)
        self.assertIn("REFUSE_RESUME", report.get("error", ""))

    def test_compatible_exact_resume_is_allowed(self) -> None:
        plan = _plan([_ai("A1", "review")])
        self._run(plan, run_id="r1")
        report, _ = self._run(plan, run_id="r1", resume=True)
        self.assertNotIn("REFUSE_RESUME", report.get("error", ""))

    def test_uncertain_mutation_refused_when_unverified(self) -> None:
        plan = _plan([_ai("A1", "apply the patch", intent="MUTATING")], intent="MUTATING")
        backend = execution.FakeBackend(script={"A1": [
            {"status": "SUCCESS", "completion_signal": "agent_reported", "acceptance_results": {}}]})
        self._run(plan, backend=backend, run_id="r1")
        report, _ = self._run(plan, run_id="r1", resume=True)
        self.assertIn("REFUSE_RESUME_UNCERTAIN_MUTATION", report.get("error", ""))


# --------------------------------------------------------------------------- #
# H. Status persistence
# --------------------------------------------------------------------------- #
class StatusPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = pathlib.Path(self.tmp.name) / "state"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _persist(self, status, failure_category=None, error=None, intent="MUTATING"):
        plan = {"objective_summary": "run", "intent": intent, "planner_mode": {"mode": "NONE"},
                "planning_budget": {"hard_max_ai_calls": 1, "tool_only_nodes": 0},
                "nodes": [{"task_id": "T1", "title": "t", "executor": "AI", "intent": intent,
                           "depends_on": [], "task_family": "generic", "task_class": "NORMAL",
                           "routing": {"tier": "BALANCED", "effort": "medium"},
                           "context": {"budget": "SMALL"}, "conditional": False}]}
        report = {"run_id": "r-" + status, "head": "abc", "calls_used": 1, "error": error,
                  "final": {"failed": 1 if status in ("FAILURE", "UNVERIFIED") else 0},
                  "nodes": {"T1": {"status": status, "failure_category": failure_category}},
                  "attempts": []}
        s = store.OrchestrationStore(state_dir=self.state)
        try:
            store.persist_run(plan, report, machine_id="work-mac", store=s)
            run = s.get_run("r-" + status)
            task = s.run_tasks("r-" + status)[0]
            return run, task
        finally:
            s.close()

    def test_all_terminal_outcomes_map_and_never_stay_running(self) -> None:
        cases = {"SUCCESS": ("SUCCEEDED", "SUCCESS"),
                 "FAILURE": ("FAILED", "FAILED"),
                 "UNVERIFIED": ("FAILED", "UNVERIFIED"),
                 "READ_ONLY_VIOLATION": ("FAILED", "FAILED"),
                 "CANCELLED": ("FAILED", "CANCELLED"),
                 "NOT_RUN": ("FAILED", "NOT_RUN"),
                 "BLOCKED": ("FAILED", "BLOCKED"),
                 "CONTEXT_REQUIRED_SOURCE_MISSING": ("FAILED", "FAILED"),
                 "WEIRD_UNKNOWN_STATUS": ("FAILED", "FAILED")}
        for status, (run_status, task_status) in cases.items():
            with self.subTest(status=status):
                run, task = self._persist(status)
                self.assertEqual(run_status, run["status"])
                self.assertEqual(task_status, task["status"])
                self.assertNotIn(run["status"], ("RUNNING", "PENDING", "PLANNED"))

    def test_no_failed_task_stays_running(self) -> None:
        run, task = self._persist("FAILURE", failure_category="UNKNOWN_FAILURE")
        self.assertEqual("FAILED", run["status"])
        self.assertEqual("FAILED", task["status"])


# --------------------------------------------------------------------------- #
# I. Workspace authority
# --------------------------------------------------------------------------- #
class WorkspaceAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = _repo(pathlib.Path(self.tmp.name) / "target")
        (self.workspace / "marker.txt").write_text("workspace-marker-v1\n", encoding="utf-8")
        self.state = pathlib.Path(self.tmp.name) / "state"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_workspace_used_for_context_git_cwd_and_mutation(self) -> None:
        captured = {}

        class Probe(execution.Backend):
            name = "probe"
            mode = "SUPPORTED"

            def available(self):
                return True

            def execute(self, request):
                captured["working_dir"] = request["working_dir"]
                captured["prompt"] = request["prompt"]
                return {"task_id": request["task_id"], "status": "SUCCESS",
                        "completion_signal": "agent_reported", "acceptance_results": _accept("ok")}

        plan = _plan([_ai("A1", "inspect marker", refs=[{"path": "marker.txt", "start": 1, "end": 3}])])
        report = execution.execute_plan(plan, backend=Probe(), state_dir=self.state,
                                        repo_root=self.workspace, confirm=True, record_evidence=False,
                                        allow_dirty=True)
        self.assertEqual("SUCCESS", report["nodes"]["A1"]["status"])
        self.assertEqual(str(self.workspace.resolve()), captured["working_dir"])  # worker cwd
        self.assertIn("workspace-marker-v1", captured["prompt"])                 # retrieval root
        head = subprocess.run(["git", "-C", str(self.workspace), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(head, report["head"])                                   # Git checks

    def test_mutation_check_targets_workspace_not_infra_repo(self) -> None:
        class Mutating(execution.Backend):
            name = "mutator"
            mode = "SUPPORTED"

            def available(self):
                return True

            def execute(self, request):
                (self.root / "injected.txt").write_text("x", encoding="utf-8")
                return {"task_id": request["task_id"], "status": "SUCCESS",
                        "completion_signal": "agent_reported", "acceptance_results": _accept("ok")}

        mutator = Mutating()
        mutator.root = self.workspace
        plan = _plan([_ai("A1", "inspect", intent="READ_ONLY")])
        report = execution.execute_plan(plan, backend=mutator, state_dir=self.state,
                                        repo_root=self.workspace, confirm=True, record_evidence=False,
                                        allow_dirty=True)
        self.assertEqual("READ_ONLY_VIOLATION", report["error"])
        self.assertEqual("READ_ONLY_VIOLATION", report["nodes"]["A1"]["status"])


# --------------------------------------------------------------------------- #
# J. Deterministic intent
# --------------------------------------------------------------------------- #
class DeterministicIntentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = _repo(pathlib.Path(self.tmp.name) / "repo")
        (self.root / "data.json").write_text(json.dumps({"a": 1, "b": {"c": "x"}}), encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-m", "cfg"], check=True, capture_output=True)
        self.state = pathlib.Path(self.tmp.name) / "state"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_head_equals_is_actually_compared(self) -> None:
        head = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        tasks = deterministic.classify_objective(f"Verify HEAD equals {head}", root=self.root)
        self.assertEqual("git_head_matches", tasks[0]["operation"])
        self.assertEqual(head, tasks[0]["args"]["expected"])
        match = deterministic.ToolExecutor(root=self.root).execute(tasks[0])
        self.assertTrue(deterministic.ToolExecutor(root=self.root).validate_result(tasks[0], match))
        wrong = deterministic.classify_objective("Verify HEAD equals deadbeef", root=self.root)
        nomatch = deterministic.ToolExecutor(root=self.root).execute(wrong[0])
        self.assertFalse(deterministic.ToolExecutor(root=self.root).validate_result(wrong[0], nomatch))

    def test_config_value_equals_is_actually_evaluated(self) -> None:
        tasks = deterministic.classify_objective(
            "Check config value in data.json key b.c equals x", root=self.root)
        self.assertEqual("config_value_matches", tasks[0]["operation"])
        tool = deterministic.ToolExecutor(root=self.root)
        self.assertTrue(tool.validate_result(tasks[0], tool.execute(tasks[0])))
        bad = deterministic.classify_objective("Check config value in data.json key a equals 999",
                                               root=self.root)
        self.assertFalse(tool.validate_result(bad[0], tool.execute(bad[0])))

    def test_explanatory_objective_is_not_claimed_deterministic(self) -> None:
        self.assertEqual([], deterministic.classify_objective("Explain branch coverage", root=self.root))
        plan = orchestrator.decompose("Explain branch coverage")
        self.assertTrue(any(node.get("executor") == "AI" for node in plan["nodes"]))

    def test_unevaluable_equality_is_not_partially_done(self) -> None:
        # An equality we cannot parse must not degrade into a bare repo-state probe.
        self.assertEqual([], deterministic.classify_objective("Verify the SHA equals something-unknown",
                                                              root=self.root))


# --------------------------------------------------------------------------- #
# K. Invocation accounting
# --------------------------------------------------------------------------- #
class AccountingTests(ContractFixture):
    def test_context_rejection_before_launch_is_zero_calls(self) -> None:
        plan = _plan([_ai("A1", "inspect", refs=[{"path": "missing.txt", "start": 1, "end": 2}])])
        report, backend = self._run(plan)
        self.assertEqual(0, report["calls_used"])
        self.assertEqual([], backend.calls)
        self.assertEqual(0, report["accounting"]["worker_launches"])
        self.assertEqual("CONTEXT_REQUIRED_SOURCE_MISSING", report["nodes"]["A1"]["failure_category"])

    def test_launch_increments_at_boundary(self) -> None:
        report, backend = self._run(_plan([_ai("A1", "review")]))
        self.assertEqual(1, report["calls_used"])
        self.assertEqual(1, report["accounting"]["worker_launches"])

    def test_tool_only_and_cache_hit_are_zero(self) -> None:
        tool_plan = {"schema_version": 1, "valid": True, "intent": "READ_ONLY", "order": ["D1"],
                     "planner_mode": {"mode": "NONE"},
                     "nodes": [{"task_id": "D1", "title": "state", "objective_summary": "state",
                                "task_family": "deterministic_tool", "task_class": "MECHANICAL",
                                "depends_on": [], "conditional": False, "intent": "READ_ONLY",
                                "executor": "TOOL_ONLY",
                                "tool": {"operation": "repository_state", "args": {}},
                                "routing": None, "context": {"budget": "TINY"},
                                "acceptance_criteria": ["ok"], "risk_signals": [], "knowledge_refs": []}]}
        report, backend = self._run(tool_plan)
        self.assertEqual("SUCCESS", report["nodes"]["D1"]["status"])
        self.assertEqual(0, report["calls_used"])
        self.assertEqual([], backend.calls)


if __name__ == "__main__":
    unittest.main()
