"""Operational Data Contract V0.1 tests: schema, migrations, lifecycle, attempt
semantics, retry/failover, transactions, queries, security, and fixtures.
No real AI calls."""

from __future__ import annotations

import pathlib
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import orchestration_store as store  # noqa: E402


class SchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = pathlib.Path(self.tmp.name) / "state"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_fresh_db_creation_and_version(self) -> None:
        s = store.OrchestrationStore(state_dir=self.state)
        tables = {row[0] for row in s.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for expected in ("projects", "runs", "tasks", "task_dependencies", "attempts", "run_events", "meta"):
            self.assertIn(expected, tables)
        self.assertEqual(store.SCHEMA_VERSION, s.status()["schema_version"])
        self.assertEqual("WAL", s.connection.execute("PRAGMA journal_mode").fetchone()[0].upper())
        self.assertEqual(1, s.connection.execute("PRAGMA foreign_keys").fetchone()[0])
        s.close()

    def test_required_indexes_present(self) -> None:
        s = store.OrchestrationStore(state_dir=self.state)
        indexes = {row[0] for row in s.connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        for expected in ("idx_runs_project", "idx_runs_machine", "idx_tasks_run_status",
                         "idx_attempts_task_no", "idx_events_run"):
            self.assertIn(expected, indexes)
        s.close()

    def test_migration_idempotent_and_nondestructive(self) -> None:
        s = store.OrchestrationStore(state_dir=self.state)
        s.ensure_project("remote:x")
        s.create_run("r1", project_id="remote:x", machine_id="work-mac")
        s.close()
        s2 = store.OrchestrationStore(state_dir=self.state)  # reopening must not recreate/drop
        self.assertIsNotNone(s2.get_run("r1"))
        self.assertEqual(store.SCHEMA_VERSION, s2.status()["schema_version"])
        s2.close()

    def test_v1_to_latest_migration_is_additive(self) -> None:
        # Build a v1 DB (migration 1 only), then reopen and confirm an incremental,
        # non-destructive upgrade to latest adds reuse (v2) and entry-mode (v3) fields.
        v1 = [(1, store._MIGRATIONS[0][1])]
        with unittest.mock.patch.object(store, "_MIGRATIONS", v1):
            s = store.OrchestrationStore(state_dir=self.state)
            s.ensure_project("remote:x")
            # A genuine v1 row (no v2/v3 columns exist yet).
            s.connection.execute(
                "INSERT INTO runs(run_id, project_id, machine_id, status, created_at, started_at,"
                " schema_version) VALUES('r1','remote:x','work-mac','RUNNING','now','now',1)")
            s.connection.commit()
            self.assertEqual(1, s.status()["schema_version"])
            s.close()
        s2 = store.OrchestrationStore(state_dir=self.state)  # migrates 1 -> latest
        self.assertEqual(store.SCHEMA_VERSION, s2.status()["schema_version"])
        self.assertIsNotNone(s2.get_run("r1"))  # prior data preserved, not recreated
        task_columns = {row[1] for row in s2.connection.execute("PRAGMA table_info(tasks)")}
        for expected in ("execution_source", "cache_entry_id", "reused_from_run_id",
                         "reused_from_task_id", "input_fingerprint", "result_fingerprint"):
            self.assertIn(expected, task_columns)
        run_columns = {row[1] for row in s2.connection.execute("PRAGMA table_info(runs)")}
        for expected in ("entry_mode", "controller_ai_calls"):
            self.assertIn(expected, run_columns)
        s2.close()

    def test_v2_to_v3_entry_mode_migration(self) -> None:
        v2 = list(store._MIGRATIONS[:2])
        with unittest.mock.patch.object(store, "_MIGRATIONS", v2):
            s = store.OrchestrationStore(state_dir=self.state)
            self.assertEqual(2, s.status()["schema_version"])
            s.close()
        s2 = store.OrchestrationStore(state_dir=self.state)  # migrates 2 -> 3
        self.assertEqual(3, s2.status()["schema_version"])
        columns = {row[1] for row in s2.connection.execute("PRAGMA table_info(runs)")}
        self.assertIn("entry_mode", columns)
        self.assertIn("controller_ai_calls", columns)
        s2.close()

    def test_migration_rollback_on_failure(self) -> None:
        bad = [(1, ["CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
                    "CREATE TABLE IF NOT EXISTS only_one (x TEXT)"]),
               (2, ["THIS IS NOT VALID SQL"])]
        with unittest.mock.patch.object(store, "_MIGRATIONS", bad):
            with self.assertRaises(store.StoreError):
                s = store.OrchestrationStore(state_dir=self.state)
            # migration 1 applied, migration 2 rolled back -> version stays 1, no partial table
        conn = sqlite3.connect(str(store.db_path(self.state)))
        self.assertEqual(1, int(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]))
        conn.close()

    def test_no_raw_prompt_or_response_columns(self) -> None:
        s = store.OrchestrationStore(state_dir=self.state)
        columns = set()
        for table in ("runs", "tasks", "attempts", "run_events"):
            columns |= {row[1] for row in s.connection.execute(f"PRAGMA table_info({table})")}
        for forbidden in ("prompt", "response", "transcript", "log", "token", "api_key"):
            self.assertNotIn(forbidden, columns)
        s.close()


class LifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = pathlib.Path(self.tmp.name) / "state"
        self.s = store.OrchestrationStore(state_dir=self.state)

    def tearDown(self) -> None:
        self.s.close()
        self.tmp.cleanup()

    def test_project_run_task_attempt_event(self) -> None:
        self.s.ensure_project("remote:example", repository_id="remote:example", name="repo")
        self.s.create_run("r1", project_id="remote:example", machine_id="work-mac", status="RUNNING",
                          intent="MUTATING", planner_mode="NONE", hard_max_ai_calls=2,
                          minimum_required_ai_calls=2, planned_ai_calls=2, tool_only_count=2)
        self.s.create_task("r1", "T1", title="inspect", executor_type="TOOL_ONLY", status="SUCCESS",
                           task_class="MECHANICAL", tool_operation="repository_state")
        self.s.create_task("r1", "T2", title="arch", executor_type="AI", task_class="CORRECTNESS_SENSITIVE",
                           required_tier="STRONG", required_effort="high")
        self.s.add_dependency("r1", "T2", "T1")
        self.s.start_task("r1", "T2")
        self.s.record_attempt("r1", "T2", attempt_no=1, backend="opencode", provider="deepseek",
                              model="deepseek-flash", native_effort="high", normalized_effort="HIGH",
                              status="SUCCESS", required_tier="STRONG", required_effort="high",
                              completion_signal="agent_reported")
        self.s.finish_task("r1", "T2", "SUCCESS")
        self.s.finish_run("r1", "SUCCEEDED", used_ai_calls=1)
        self.assertEqual(1, len(self.s.recent_runs()))
        self.assertEqual(2, len(self.s.run_tasks("r1")))
        self.assertEqual([{"task_id": "T2", "depends_on_task_id": "T1"}], self.s.run_dependencies("r1"))
        self.assertEqual(1, len(self.s.task_attempts("T2")))
        events = self.s.run_events("r1")
        self.assertEqual("RUN_CREATED", events[0]["event_type"])
        self.assertEqual("RUN_COMPLETED", events[-1]["event_type"])
        summary = self.s.execution_summary("r1")
        self.assertEqual(2, summary["tasks_by_status"]["SUCCESS"])  # T1 (tool) + T2 (ai)

    def test_no_change_required_and_deterministic_finalizer(self) -> None:
        self.s.ensure_project("remote:example")
        self.s.create_run("r2", project_id="remote:example", machine_id="work-mac")
        self.s.create_task("r2", "D1", title="cfg", executor_type="TOOL_ONLY", status="NO_CHANGE_REQUIRED",
                           tool_operation="config_value_matches")
        self.s.create_task("r2", "DF", title="finalize", executor_type="TOOL_ONLY", status="SUCCESS",
                           tool_operation="finalize")
        events = [e["event_type"] for e in self.s.run_events("r2")]
        self.assertIn("TASK_CREATED", events)


class AttemptSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = pathlib.Path(self.tmp.name) / "state"
        self.s = store.OrchestrationStore(state_dir=self.state)
        self.s.ensure_project("remote:x")
        self.s.create_run("r", project_id="remote:x", machine_id="work-mac")

    def tearDown(self) -> None:
        self.s.close()
        self.tmp.cleanup()

    def test_tier_and_effort_independent(self) -> None:
        self.s.create_task("r", "T", title="x", executor_type="AI")
        self.s.record_attempt("r", "T", attempt_no=1, backend="codex", provider="codex",
                              model="gpt-6.1-sol", native_effort="high", normalized_effort="HIGH",
                              status="SUCCESS", required_tier="MAX", required_effort="max")
        attempt = self.s.task_attempts("T")[0]
        self.assertEqual("MAX", attempt["required_tier"])
        self.assertEqual("high", attempt["native_effort"])
        self.assertEqual("HIGH", attempt["normalized_effort"])  # never MAX
        self.assertNotEqual("MAX", attempt["normalized_effort"])

    def test_balanced_medium(self) -> None:
        self.s.create_task("r", "T", title="x", executor_type="AI")
        self.s.record_attempt("r", "T", attempt_no=1, backend="codex", provider="codex",
                              model="gpt-6.1-sol", native_effort="medium", normalized_effort="MEDIUM",
                              status="SUCCESS", required_tier="BALANCED", required_effort="medium")
        attempt = self.s.task_attempts("T")[0]
        self.assertEqual(("BALANCED", "medium", "MEDIUM"),
                         (attempt["required_tier"], attempt["native_effort"], attempt["normalized_effort"]))

    def test_failover_same_task_two_attempts(self) -> None:
        self.s.create_task("r", "T2", title="arch", executor_type="AI", required_tier="STRONG")
        self.s.record_attempt("r", "T2", attempt_no=1, backend="codex", provider="codex", model="gpt-6.1-sol",
                              native_effort="high", normalized_effort="HIGH", status="FAILED",
                              failure_category="RATE_LIMIT", required_tier="STRONG", required_effort="high")
        self.s.record_attempt("r", "T2", attempt_no=2, backend="opencode", provider="deepseek",
                              model="deepseek-flash", native_effort="high", normalized_effort="HIGH",
                              status="SUCCESS", is_fallback=True, required_tier="STRONG",
                              required_effort="high", completion_signal="agent_reported")
        attempts = self.s.task_attempts("T2")
        self.assertEqual([1, 2], [a["attempt_no"] for a in attempts])
        self.assertEqual(0, attempts[0]["is_retry"] + attempts[0]["is_fallback"])  # first attempt
        self.assertEqual(1, attempts[1]["is_fallback"])
        self.assertEqual(1, len(self.s.run_tasks("r")))  # same task, not two


class TransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = pathlib.Path(self.tmp.name) / "state"
        self.s = store.OrchestrationStore(state_dir=self.state)
        self.s.ensure_project("remote:x")
        self.s.create_run("r", project_id="remote:x", machine_id="work-mac")

    def tearDown(self) -> None:
        self.s.close()
        self.tmp.cleanup()

    def test_invalid_status_leaves_no_partial_state(self) -> None:
        self.s.create_task("r", "T", title="x", executor_type="AI")
        before = len(self.s.task_attempts("T"))
        with self.assertRaises(store.StoreError):
            self.s.finish_task("r", "T", "NOT_A_STATUS")
        self.assertEqual(before, len(self.s.task_attempts("T")))

    def test_secret_payload_rejected_atomically(self) -> None:
        self.s.create_task("r", "T", title="x", executor_type="AI")
        events_before = len(self.s.run_events("r"))
        with self.assertRaises(store.StoreError):
            self.s.record_attempt("r", "T", attempt_no=1, backend="opencode", provider="deepseek",
                                  model="sk-" + "a" * 30, native_effort="high", normalized_effort="HIGH",
                                  status="SUCCESS")
        self.assertEqual(events_before, len(self.s.run_events("r")))  # no partial events

    def test_unknown_event_type_rejected(self) -> None:
        with self.assertRaises(store.StoreError):
            self.s._event("r", "NOT_AN_EVENT")


class QueryAndFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = pathlib.Path(self.tmp.name) / "state"
        self.s = store.OrchestrationStore(state_dir=self.state)

    def tearDown(self) -> None:
        self.s.close()
        self.tmp.cleanup()

    def _mixed_plan(self):
        return {"objective_summary": "mixed", "intent": "MUTATING",
                "planner_mode": {"mode": "NONE"},
                "planning_budget": {"hard_max_ai_calls": 2, "planned_ai_calls": 2,
                                    "minimum_required_ai_calls": 2, "tool_only_nodes": 2},
                "nodes": [
                    {"task_id": "T1", "title": "inspect", "executor": "TOOL_ONLY", "intent": "READ_ONLY",
                     "tool": {"operation": "repository_state"}, "depends_on": [],
                     "task_family": "repository_inspection", "task_class": "MECHANICAL"},
                    {"task_id": "T2", "title": "arch", "executor": "AI", "intent": "READ_ONLY",
                     "depends_on": ["T1"], "task_family": "multi_machine_acceptance",
                     "task_class": "CORRECTNESS_SENSITIVE", "routing": {"tier": "STRONG", "effort": "high"},
                     "context": {"budget": "MEDIUM"}, "conditional": False},
                    {"task_id": "T2S", "title": "security", "executor": "AI", "intent": "READ_ONLY",
                     "depends_on": ["T2"], "task_family": "security_sensitive_design",
                     "task_class": "CRITICAL", "routing": {"tier": "MAX", "effort": "max"},
                     "context": {"budget": "MEDIUM"}, "conditional": False},
                    {"task_id": "T6", "title": "finalize", "executor": "TOOL_ONLY", "intent": "READ_ONLY",
                     "tool": {"operation": "finalize"}, "depends_on": ["T2S"],
                     "task_family": "report_recovery_validation", "task_class": "NORMAL"}]}

    def _mixed_report(self):
        return {"run_id": "run-mixed", "head": "abc", "calls_used": 2, "error": None,
                "final": {"failed": 0},
                "nodes": {"T1": {"status": "SUCCESS"}, "T2": {"status": "SUCCESS"},
                          "T2S": {"status": "SUCCESS"}, "T6": {"status": "SUCCESS"}},
                "attempts": [
                    {"task_id": "T2", "attempt_no": 1, "backend": "opencode", "provider": "deepseek",
                     "model": "deepseek-flash", "native_effort": "high", "normalized_effort": "HIGH",
                     "status": "SUCCESS", "required_tier": "STRONG", "required_effort": "high",
                     "completion_signal": "agent_reported"},
                    {"task_id": "T2S", "attempt_no": 1, "backend": "codex", "provider": "codex",
                     "model": "gpt-6.1-sol", "native_effort": "high", "normalized_effort": "HIGH",
                     "status": "SUCCESS", "required_tier": "MAX", "required_effort": "max",
                     "completion_signal": "agent_reported"}]}

    def test_persist_mixed_run_represents_real_behavior(self) -> None:
        run_id = store.persist_run(self._mixed_plan(), self._mixed_report(), machine_id="work-mac",
                                   store=self.s)
        run = self.s.get_run(run_id)
        self.assertEqual("SUCCEEDED", run["status"])
        self.assertEqual(2, run["used_ai_calls"])
        tasks = {t["task_id"]: t for t in self.s.run_tasks(run_id)}
        self.assertEqual("TOOL_ONLY", tasks["T1"]["executor_type"])
        self.assertIsNone(tasks["T1"]["required_tier"])
        self.assertEqual("CRITICAL", tasks["T2S"]["task_class"])
        t2s = self.s.task_attempts("T2S")[0]
        self.assertEqual(("MAX", "high", "HIGH"),
                         (t2s["required_tier"], t2s["native_effort"], t2s["normalized_effort"]))
        self.assertEqual([], self.s.task_attempts("T1"))  # TOOL_ONLY has no attempt row

    def test_event_cursor(self) -> None:
        run_id = store.persist_run(self._mixed_plan(), self._mixed_report(), machine_id="work-mac",
                                   store=self.s)
        first = self.s.run_events(run_id)
        self.assertGreater(len(first), 1)
        cursor = first[0]["event_id"]
        after = self.s.run_events(run_id, after=cursor)
        self.assertTrue(all(e["event_id"] > cursor for e in after))
        self.assertEqual(len(first) - 1, len(after))

    def test_legacy_observation_flag(self) -> None:
        self.assertTrue(self.s.legacy_observation_excluded("868087de1dda47059713f076ce26a5d9"))
        self.assertFalse(self.s.legacy_observation_excluded("other"))


if __name__ == "__main__":
    unittest.main()
