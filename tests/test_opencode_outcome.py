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
import infra  # noqa: E402
import opencode_outcome  # noqa: E402
import telemetry  # noqa: E402

RECORDER = ROOT / "scripts/record-opencode-outcome"
SESSION_ID = "ses_f4af195bbffeLnPmKyrNmTP0pP"
SESSION = {
    "id": SESSION_ID,
    "projectID": "proj-1",
    "model": {"id": "deepseek-flash", "providerID": "deepseek", "variant": "default"},
    "cost": 0.00157227,
    "tokens": {"input": 8953, "output": 18, "reasoning": 361, "cache": {"read": 640, "write": 0}},
    "outcome": "succeeded",
    "time": {"created": 1789744015945, "updated": 1789744018557, "idle": 1789744017429},
    "title": "OUTCOME-BRIDGE-OK",
    "subpath": "local-ai-infra",
}


def build(**overrides: object) -> dict:
    session = json.loads(json.dumps(SESSION))
    session.update(overrides)
    return opencode_outcome.build_outcome(session)


def session_entry(base: pathlib.Path, *, updated: int = 1789744018557,
                  created: int = 1789744015945) -> dict:
    return {"id": SESSION_ID, "title": "OUTCOME-BRIDGE-OK", "updated": updated,
            "created": created, "directory": str(base)}


class OpenCodeOutcomeBuildTests(unittest.TestCase):
    def test_semantic_success_stays_unknown(self) -> None:
        record = build()
        self.assertEqual("succeeded", record["status"])
        self.assertIsNone(record["success"])
        self.assertIsNone(record["tests_passed"])
        self.assertIsNone(record["error_category"])
        self.assertIsNone(record["error_code"])

    def test_failed_execution_is_not_semantic_failure(self) -> None:
        record = build(outcome="failed")
        self.assertEqual("failed", record["status"])
        self.assertIsNone(record["success"])
        self.assertIsNone(record["error_category"])

    def test_interrupted_maps_to_cancelled(self) -> None:
        record = build(outcome="interrupted")
        self.assertEqual("cancelled", record["status"])
        self.assertIsNone(record["success"])

    def test_missing_outcome_is_unknown(self) -> None:
        record = build(outcome="something-new")
        self.assertEqual("unknown", record["status"])
        self.assertIsNone(record["success"])

    def test_provider_model_cost_latency_correlated(self) -> None:
        record = build()
        self.assertEqual("deepseek", record["initial_provider"])
        self.assertEqual("deepseek", record["final_provider"])
        self.assertEqual("deepseek-flash", record["final_model"])
        self.assertEqual("default", record["final_variant"])
        self.assertAlmostEqual(0.00157227, record["provider_reported_cost"], places=8)
        self.assertEqual(float(1789744017429 - 1789744015945), record["total_latency_ms"])
        self.assertEqual("2026-09-18T15:06:55.945000Z", record["started_at"])
        self.assertEqual("2026-09-18T15:06:57.429000Z", record["completed_at"])
        self.assertFalse(record["escalated"])
        self.assertIsNone(record["attempt_count"])

    def test_no_prompt_title_or_message_stored(self) -> None:
        record = build()
        self.assertEqual(set(telemetry.TASK_OUTCOME_FIELDS), set(record))
        raw = json.dumps(record)
        self.assertNotIn("OUTCOME-BRIDGE-OK", raw)
        for forbidden in ("prompt", "response", "content", "title", "message"):
            self.assertNotIn(forbidden, record)

    def test_task_id_is_stable_per_session(self) -> None:
        first = opencode_outcome.task_id_for_session(SESSION_ID)
        second = opencode_outcome.task_id_for_session(SESSION_ID)
        other = opencode_outcome.task_id_for_session("ses_other")
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertEqual(first, build()["task_id"])
        telemetry.validate_task_outcome(build())


class SelectSessionTests(unittest.TestCase):
    def test_filters_directory_and_start_window(self) -> None:
        base = "/tmp/project-a"
        sessions = [
            {"id": "new", "updated": 2000, "directory": base},
            {"id": "old", "updated": 500, "directory": base},
            {"id": "elsewhere", "updated": 3000, "directory": "/tmp/project-b"},
        ]
        self.assertEqual("new", opencode_outcome.select_session(sessions, base, 1000)["id"])

    def test_no_match_returns_none(self) -> None:
        sessions = [{"id": "x", "updated": 100, "directory": "/tmp/p"}]
        self.assertIsNone(opencode_outcome.select_session(sessions, "/tmp/p", 1000))
        self.assertIsNone(opencode_outcome.select_session(sessions, "/tmp/p", 0))

    def test_concurrent_matches_are_ambiguous(self) -> None:
        sessions = [
            {"id": "a", "updated": 2000, "directory": "/tmp/p"},
            {"id": "b", "updated": 2001, "directory": "/tmp/p"},
        ]
        self.assertIsNone(opencode_outcome.select_session(sessions, "/tmp/p", 1000))


def _fake_opencode(directory: pathlib.Path, *, detail: dict, sessions: list) -> pathlib.Path:
    fake = directory / "opencode"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"payload = {json.dumps({'sessions': sessions, 'detail': detail})!r}\n"
        "data = json.loads(payload)\n"
        "if len(sys.argv) > 1 and sys.argv[1] == 'session':\n"
        "    print(json.dumps(data['sessions']))\n"
        "elif len(sys.argv) > 1 and sys.argv[1] == 'api':\n"
        "    if data['detail'] is None:\n"
        "        sys.exit(1)\n"
        "    print(json.dumps({'data': data['detail']}))\n"
        "else:\n"
        "    sys.exit(2)\n",
        encoding="utf-8",
    )
    os.chmod(fake, 0o755)
    return fake


def _run_recorder(base: pathlib.Path, fake: pathlib.Path, *, boundary: str, since_ms: int,
                  output: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(RECORDER), "--boundary", boundary, "--since-ms", str(since_ms),
         "--directory", str(base), "--opencode-path", str(fake), "--output", str(output), "--json"],
        cwd=ROOT, text=True, capture_output=True,
    )


class RecorderTests(unittest.TestCase):
    def test_one_shot_records_once_and_dedupes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = pathlib.Path(temp)
            output = base / "outcomes.jsonl"
            fake = _fake_opencode(base, detail=SESSION, sessions=[session_entry(base)])

            first = _run_recorder(base, fake, boundary="run", since_ms=1789744015000, output=output)
            self.assertEqual(0, first.returncode, first.stderr)
            self.assertTrue(json.loads(first.stdout)["recorded"])
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(1, len(rows))
            self.assertIsNone(rows[0]["success"])
            self.assertEqual("succeeded", rows[0]["status"])

            second = _run_recorder(base, fake, boundary="run", since_ms=1789744015000, output=output)
            self.assertEqual(0, second.returncode, second.stderr)
            self.assertFalse(json.loads(second.stdout)["recorded"])
            self.assertEqual(1, len(output.read_text(encoding="utf-8").splitlines()))

    def test_interactive_is_never_finalized(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = pathlib.Path(temp)
            output = base / "outcomes.jsonl"
            fake = _fake_opencode(base, detail=SESSION, sessions=[session_entry(base)])

            proc = _run_recorder(base, fake, boundary="interactive", since_ms=1789744015000, output=output)
            self.assertEqual(0, proc.returncode, proc.stderr)
            self.assertFalse(json.loads(proc.stdout)["recorded"])
            self.assertFalse(output.exists())

    def test_no_matching_session_records_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = pathlib.Path(temp)
            output = base / "outcomes.jsonl"
            fake = _fake_opencode(base, detail=SESSION, sessions=[session_entry(base)])

            proc = _run_recorder(base, fake, boundary="run", since_ms=1789744099999, output=output)
            self.assertEqual(0, proc.returncode, proc.stderr)
            self.assertFalse(json.loads(proc.stdout)["recorded"])
            self.assertFalse(output.exists())

    def test_unavailable_session_detail_records_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = pathlib.Path(temp)
            output = base / "outcomes.jsonl"
            fake = _fake_opencode(base, detail=None, sessions=[session_entry(base)])

            proc = _run_recorder(base, fake, boundary="run", since_ms=1789744015000, output=output)
            self.assertEqual(0, proc.returncode, proc.stderr)
            self.assertFalse(json.loads(proc.stdout)["recorded"])
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
