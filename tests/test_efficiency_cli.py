"""Efficiency CLI + baseline tests: read-only surface, comparable snapshot, no content.

Everything runs against a temporary fixture state dir (``LAI_STATE_DIR`` pointed at a
temp dir), so the real machine state is never read or written. Deterministic and
offline; no AI or provider call. The CLI is exercised both in-process by module path and
through the real ``scripts/efficiency`` launcher, which is what proves interpreter
selection.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import efficiency  # noqa: E402
import orchestration_store as store_mod  # noqa: E402

CLI = ROOT / "scripts/common/efficiency_cli.py"
ENTRY = ROOT / "scripts/efficiency"

# Deliberately injected into a fixture's free-text fields: it must never reach a baseline.
SENTINEL = "CANARY-7f31c2-must-not-leak"


def _tool(task_id, op, title=None):
    return {"task_id": task_id, "title": title or op, "objective_summary": title or op,
            "executor": "TOOL_ONLY", "task_family": "deterministic_tool", "task_class": "MECHANICAL",
            "depends_on": [], "conditional": False, "intent": "READ_ONLY",
            "tool": {"operation": op, "args": {}}, "routing": None,
            "context": {"budget": "TINY"}, "acceptance_criteria": ["ok"]}


def _ai(task_id, title=None):
    return {"task_id": task_id, "title": title or task_id, "objective_summary": title or task_id,
            "executor": "AI", "task_family": "generic", "task_class": "NORMAL", "depends_on": [],
            "conditional": False, "intent": "READ_ONLY",
            "routing": {"tier": "BALANCED", "effort": "medium"},
            "context": {"budget": "SMALL"}, "acceptance_criteria": ["ok"]}


def _plan(nodes, objective="synthetic"):
    return {"schema_version": 1, "valid": True, "intent": "READ_ONLY",
            "order": [n["task_id"] for n in nodes], "nodes": nodes,
            "planner_mode": {"mode": "NONE"}, "objective_summary": objective,
            "planning_budget": {"hard_max_ai_calls": None, "planned_ai_calls": 1,
                                "minimum_required_ai_calls": 1, "tool_only_nodes": 1}}


def _attempt(task_id, n, status, *, provider="deepseek", model="deepseek-flash"):
    return {"task_id": task_id, "attempt_no": n, "backend": "opencode", "provider": provider,
            "model": model, "native_effort": "medium", "normalized_effort": "MEDIUM",
            "status": status, "required_tier": "BALANCED", "required_effort": "medium",
            "failure_category": None, "is_retry": False, "is_fallback": False,
            "completion_signal": "agent_reported" if status == "SUCCESS" else None}


def _run_fixture(run_id: str, *, canary: bool = False):
    """A minimal persisted run. Only the canary run carries the sentinel string."""
    title = SENTINEL if canary else "reused"
    nodes = [_tool("D1", "repository_state"), _ai("A1", title=title), _ai("A2", title="reused")]
    plan = _plan(nodes, objective=f"objective {SENTINEL}" if canary else "synthetic")
    report = {"run_id": run_id, "head": "abc", "calls_used": 1, "error": None,
              "entry_mode": "DIRECT_CLI", "final": {"failed": 0},
              "nodes": {"D1": {"status": "SUCCESS", "execution_source": "TOOL_ONLY"},
                        "A1": {"status": "SUCCESS", "execution_source": "EXECUTED",
                               "input_fingerprint": "i1", "result_fingerprint": "r1"},
                        "A2": {"status": "SUCCESS", "execution_source": "CACHE",
                               "cache_entry_id": "c2", "reused_from_run_id": "r0",
                               "reused_from_task_id": "A2", "input_fingerprint": "i2",
                               "result_fingerprint": "r2"}},
              "attempts": [_attempt("A1", 1, "SUCCESS")],
              "cache": {"decisions": [
                  {"task_id": "A2", "decision": "REUSE", "cache_entry_id": "c2",
                   "reason": "UNCHANGED_INPUTS"},
                  {"task_id": "A1", "decision": "RERUN", "reason": "CACHE_MISS"},
                  {"task_id": "A1", "decision": "CACHE_WRITTEN", "cache_entry_id": "c1"}]}}
    return plan, report


class CliTestBase(unittest.TestCase):
    """A temp state dir seeded before any CLI run; the store is closed before each run."""

    #: (run_id, machine_id, project_id); the first is the canary run.
    RUNS = (("run-1", "work-mac", "ai-local-infra"),)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name)
        self.state = self.home / "state"
        self.db = store_mod.db_path(self.state)
        self.seed(self.RUNS)

    def seed(self, runs) -> None:
        store = store_mod.OrchestrationStore(state_dir=self.state)
        try:
            for index, (run_id, machine_id, project_id) in enumerate(runs):
                plan, report = _run_fixture(run_id, canary=index == 0)
                store_mod.persist_run(plan, report, machine_id=machine_id, store=store,
                                      entry_mode="DIRECT_CLI", project_id=project_id)
            # The fixture really does carry the canary: the module's per-run record shows it.
            self.assertIn(SENTINEL, efficiency.get_run_efficiency(
                runs[0][0], store=store)["objective_summary"])
        finally:
            store.close()

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["LAI_STATE_DIR"] = str(self.state)
        env.pop("LAI_PYTHON", None)
        return subprocess.run([sys.executable, str(CLI), *args], text=True,
                              capture_output=True, env=env, cwd=str(ROOT))

    def json_cli(self, *args: str):
        result = self.cli(*args)
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def db_stat(self) -> tuple[int, int]:
        stat = self.db.stat()
        return stat.st_mtime_ns, stat.st_size


class SurfaceTests(CliTestBase):
    def test_e1_summary_counts_and_no_write(self) -> None:
        before = self.db_stat()
        data = self.json_cli("summary", "--json")
        self.assertEqual(1, data["totals"]["runs"])
        self.assertEqual(2, data["totals"]["logical_ai_tasks"])
        self.assertEqual(1, data["totals"]["cache_reused_ai_tasks"])
        self.assertEqual(1, data["totals"]["real_ai_invocation_attempts"])
        self.assertEqual(1, data["totals"]["tool_only_tasks"])
        self.assertEqual(["run-1"], [r["run_id"] for r in data["runs"]])
        self.assertEqual(before, self.db_stat())

    def test_e1b_summary_limit_bounds_the_window(self) -> None:
        self.assertEqual(0, self.json_cli("summary", "--json", "--limit", "0")["totals"]["runs"])
        self.assertEqual("work-mac", self.json_cli("summary", "--json")["runs"][0]["machine_id"])

    def test_e2_run_returns_per_run_record(self) -> None:
        data = self.json_cli("run", "run-1", "--json")
        self.assertEqual("run-1", data["run_id"])
        self.assertEqual("ai-local-infra", data["project_id"])
        self.assertEqual(2, data["tasks"]["logical_ai"])
        self.assertEqual(1, data["tasks"]["cache_reused_ai"])

    def test_models_cache_context_are_module_shaped(self) -> None:
        models = self.json_cli("models", "--json")
        self.assertEqual(1, models["total_invocations"])
        self.assertEqual("deepseek-flash", models["groups"][0]["model"])
        cache = self.json_cli("cache", "--json")
        self.assertEqual(1, cache["misses"])
        self.assertEqual(1, cache["written"])
        context = self.json_cli("context", "run-1", "--json")
        self.assertFalse(context["available"])


class BaselineTests(CliTestBase):
    def _baseline(self, out: pathlib.Path):
        result = self.cli("baseline", "--out", str(out))
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(out.read_text(encoding="utf-8"))

    def test_e3_baseline_writes_documented_blocks(self) -> None:
        document = self._baseline(self.home / "b1.json")
        self.assertEqual(1, document["schema_version"])
        self.assertRegex(document["captured_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        for block in ("summary", "models", "cache", "source", "not_included"):
            self.assertIn(block, document)
        self.assertEqual(1, document["source"]["runs_included"])
        self.assertEqual(str(self.state), document["source"]["state_dir"])
        self.assertEqual("work-mac", document["machine_id"])
        self.assertEqual(["tokens", "cost", "RTK savings", "Headroom savings",
                          "prompts", "responses", "transcripts"], document["not_included"])
        # The blocks are the module's own output, not reshaped.
        self.assertEqual(self.json_cli("summary", "--json"), document["summary"])
        self.assertEqual(self.json_cli("models", "--json"), document["models"])
        self.assertEqual(self.json_cli("cache", "--json"), document["cache"])

    def test_e3b_default_output_path_is_state_scoped(self) -> None:
        result = self.cli("baseline")
        self.assertEqual(0, result.returncode, result.stderr)
        written = sorted((self.state / "efficiency").glob("baseline-*.json"))
        self.assertEqual(1, len(written), written)
        self.assertIn(str(written[0]), result.stdout)

    def test_e4_determinism_two_runs_differ_only_in_captured_at(self) -> None:
        first = self._baseline(self.home / "d1.json")
        second = self._baseline(self.home / "d2.json")
        first.pop("captured_at")
        second.pop("captured_at")
        self.assertEqual(first, second)

    def test_e5_no_content_leak(self) -> None:
        path = self.home / "leak.json"
        document = self._baseline(path)
        raw = path.read_text(encoding="utf-8")
        self.assertNotIn(SENTINEL, raw)
        # The canary is absent because this surface never prints free text — proven by the
        # module exposing it in the one place it does (the per-run record).
        self.assertIn(SENTINEL, self.cli("run", "run-1", "--json").stdout)
        self.assertFalse(any(SENTINEL in json.dumps(block) for block in
                             (document["summary"], document["models"], document["cache"])))
        self.assertTrue(document["not_included"])


class ReadOnlyTests(CliTestBase):
    def test_e6_store_untouched_by_every_subcommand(self) -> None:
        calls = [("summary", "--json"), ("run", "run-1", "--json"), ("models", "--json"),
                 ("cache", "--json"), ("context", "run-1", "--json"),
                 ("baseline", "--out", str(self.home / "ro.json"))]
        for args in calls:
            with self.subTest(subcommand=args[0]):
                before = self.db_stat()
                result = self.cli(*args)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(before, self.db_stat(), f"{args[0]} modified the store")

    def test_read_only_command_refuses_to_create_a_missing_store(self) -> None:
        empty = self.home / "empty-state"
        env = dict(os.environ)
        env["LAI_STATE_DIR"] = str(empty)
        result = subprocess.run([sys.executable, str(CLI), "summary"], text=True,
                                capture_output=True, env=env)
        self.assertEqual(1, result.returncode)
        self.assertIn("no operational DB", result.stderr)
        self.assertFalse(empty.exists(), "a read-only command created state")


MIXED_RUNS = tuple((f"run-{i}", "work-mac" if i % 2 else "unknown", f"proj-{i % 2}")
                   for i in range(7))


class WindowTests(CliTestBase):
    """One window for every block, and the composition is declared."""

    RUNS = MIXED_RUNS

    def _baseline(self, *args):
        out = self.home / f"w{len(args)}{abs(hash(args)) % 999}.json"
        result = self.cli("baseline", "--out", str(out), *args)
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(out.read_text(encoding="utf-8"))

    def test_f1_every_unexcepted_block_covers_the_limit(self) -> None:
        document = self._baseline("--limit", "3")
        self.assertEqual(3, document["window"]["runs_included"])
        self.assertEqual(3, document["summary"]["totals"]["runs"])
        self.assertEqual(3, document["cache"]["runs_considered"])
        self.assertEqual(["models"], [e["block"] for e in document["window"]["exceptions"]])
        # The whole DB is larger than the window, so this is a real bound, not a coincidence.
        self.assertEqual(["run-6", "run-5", "run-4"],
                         [r["run_id"] for r in document["summary"]["runs"]])

    def test_f1b_a_larger_limit_covers_more_runs(self) -> None:
        self.assertEqual(5, self._baseline("--limit", "5")["window"]["runs_included"])
        self.assertEqual(7, self._baseline("--limit", "50")["window"]["runs_included"])

    def test_f2_window_block_declares_the_selection(self) -> None:
        window = self._baseline("--limit", "4")["window"]
        self.assertEqual(4, window["limit"])
        self.assertEqual(4, window["runs_included"])
        self.assertIsNone(window["machine_filter"])
        self.assertEqual("most recent 4 runs by created_at", window["selection"])
        self.assertIsInstance(window["exceptions"], list)

    def test_f3_machine_filter_is_declared_in_the_window(self) -> None:
        document = self._baseline("--limit", "50", "--machine", "work-mac")
        self.assertEqual("work-mac", document["window"]["machine_filter"])
        self.assertEqual(3, document["window"]["runs_included"])   # run-1, run-3, run-5
        self.assertEqual(3, document["summary"]["totals"]["runs"])
        self.assertEqual(3, document["cache"]["runs_considered"])

    def test_f3b_the_unlimitable_block_is_declared_not_silent(self) -> None:
        exceptions = self._baseline("--limit", "4")["window"]["exceptions"]
        self.assertEqual(1, len(exceptions))
        self.assertEqual("models", exceptions[0]["block"])
        self.assertTrue(exceptions[0]["reason"].strip())

    def test_f5_two_baselines_at_one_limit_differ_only_in_captured_at(self) -> None:
        first = self._baseline("--limit", "4")
        second = self._baseline("--limit", "4")
        first.pop("captured_at")
        second.pop("captured_at")
        self.assertEqual(first, second)

    def test_f6_read_only_holds_with_the_new_flags(self) -> None:
        before = self.db_stat()
        self._baseline("--limit", "3")
        self._baseline("--limit", "50", "--machine", "unknown")
        self.assertEqual(before, self.db_stat())


class MachineTests(CliTestBase):
    """A baseline never reports a bare null identity."""

    RUNS = (("run-a", "work-mac", "proj-1"),
            ("run-b", "work-mac", "proj-1"),
            ("run-c", "unknown", "proj-2"))

    def _baseline(self, *args):
        out = self.home / f"m{abs(hash(args)) % 999}.json"
        result = self.cli("baseline", "--out", str(out), "--limit", "50", *args)
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(out.read_text(encoding="utf-8"))

    def test_f4_mixed_machine_ids_resolve_to_the_dominant_with_a_breakdown(self) -> None:
        document = self._baseline()
        self.assertEqual("work-mac", document["machine_id"])
        resolution = document["machine_resolution"]
        self.assertEqual("dominant", resolution["mode"])
        self.assertEqual({"work-mac": 2, "unknown": 1}, resolution["covered_runs"])
        self.assertIn("mixed", resolution["note"])
        self.assertEqual("proj-1", document["project_id"])
        self.assertEqual({"proj-1": 2, "proj-2": 1}, document["project_resolution"]["covered_runs"])

    def test_explicit_machine_filter_is_reported_as_explicit(self) -> None:
        document = self._baseline("--machine", "unknown")
        self.assertEqual("unknown", document["machine_id"])
        self.assertEqual("explicit", document["machine_resolution"]["mode"])
        self.assertEqual("unknown", document["window"]["machine_filter"])
        self.assertEqual(1, document["window"]["runs_included"])


class AllUnknownTests(CliTestBase):
    """When the window is entirely unattributable, null is explained, never bare."""

    RUNS = (("run-a", "unknown", "proj-1"), ("run-b", "unknown", "proj-1"))

    def test_f4c_all_unknown_reports_null_plus_breakdown(self) -> None:
        out = self.home / "unknown.json"
        result = self.cli("baseline", "--out", str(out), "--limit", "50")
        self.assertEqual(0, result.returncode, result.stderr)
        document = json.loads(out.read_text(encoding="utf-8"))
        self.assertIsNone(document["machine_id"])
        self.assertEqual("unresolved", document["machine_resolution"]["mode"])
        self.assertEqual({"unknown": 2}, document["machine_resolution"]["covered_runs"])
        self.assertEqual(2, document["window"]["runs_included"])


class LauncherTests(unittest.TestCase):
    """E7: mirror tests/test_cli_launcher.py — the entrypoint delegates, and the shared
    launcher refuses an interpreter below 3.11 rather than failing with an import error."""

    def test_entrypoint_is_sh_wrapper_over_launcher_and_module(self) -> None:
        self.assertTrue(ENTRY.exists(), ENTRY)
        body = ENTRY.read_bytes()
        self.assertEqual(b"#!/bin/sh", body.split(b"\n", 1)[0])
        self.assertIn(b"common/launch", body)
        self.assertIn(b"common/efficiency_cli.py", body)
        self.assertNotEqual(ENTRY, CLI)
        self.assertTrue(CLI.exists(), CLI)

    def test_launcher_refuses_unsupported_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fakebin = pathlib.Path(tmp) / "bin"
            fakebin.mkdir()
            fake = fakebin / "python3"
            fake.write_text(f"#!{sys.executable}\nimport sys\n"
                            "if sys.argv[1:2] == ['-c']:\n    sys.exit(1)\n")
            fake.chmod(0o755)
            env = {k: v for k, v in os.environ.items() if k != "LAI_PYTHON"}
            env["PATH"] = str(fakebin)
            result = subprocess.run([str(ENTRY), "summary"], text=True,
                                    capture_output=True, env=env)
            self.assertEqual(127, result.returncode, result.stderr)
            self.assertIn("3.11", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_real_launcher_selects_a_supported_interpreter(self) -> None:
        if sys.version_info < (3, 11):
            self.skipTest("test runner Python is unsupported")
        env = {k: v for k, v in os.environ.items() if k != "LAI_PYTHON"}
        result = subprocess.run([str(ENTRY), "--help"], text=True, capture_output=True, env=env)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("usage:", result.stdout)
        for name in ("summary", "run", "models", "cache", "context", "baseline"):
            self.assertIn(name, result.stdout)


if __name__ == "__main__":
    unittest.main()
