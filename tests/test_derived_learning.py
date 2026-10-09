"""Derived learning V0.1: local SQLite evidence ledger, shared knowledge, promotion
safety, routing integration, and cross-surface neutrality. No model/network calls."""

from __future__ import annotations

import copy
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import evidence  # noqa: E402
import derived_knowledge as knowledge  # noqa: E402
import routing  # noqa: E402


LEARN = ROOT / "scripts" / "learn"
ROUTE = ROOT / "scripts" / "route"


class EvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = pathlib.Path(self.tmp.name) / "state"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_db_is_outside_repo_and_initializes(self) -> None:
        path = evidence.db_path(self.state)
        self.assertNotIn(str(ROOT.resolve()), str(path.resolve()))
        connection = evidence.connect(state_dir=self.state)
        try:
            version = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(evidence.SCHEMA_VERSION, version)

    def test_refuses_db_inside_repository(self) -> None:
        with self.assertRaises(evidence.EvidenceError):
            evidence.connect(ROOT / "evidence.db")

    def test_record_and_query(self) -> None:
        evidence.record({"task_family": "demo", "task_class": "NORMAL", "outcome": "SUCCESS",
                         "source": "observed_runtime", "surface": "opencode"},
                        state_dir=self.state)
        result = evidence.query(task_family="demo", state_dir=self.state)
        self.assertEqual((1, 1, 1), (result["total"], result["success"], result["local"]))

    def test_seed_is_idempotent_with_stable_ids(self) -> None:
        seed = knowledge.load_historical_seed()
        first = evidence.import_seed(seed, state_dir=self.state)
        second = evidence.import_seed(seed, state_dir=self.state)
        self.assertEqual(len(seed), first["inserted"])
        self.assertEqual(0, second["inserted"])
        self.assertEqual(len(seed), evidence.status(state_dir=self.state)["observations"])

    def test_rejects_unknown_and_secret_fields(self) -> None:
        with self.assertRaises(evidence.EvidenceError):
            evidence.record({"task_family": "x", "outcome": "SUCCESS", "source": "observed_runtime",
                             "prompt": "raw"}, state_dir=self.state)
        with self.assertRaises(evidence.EvidenceError):
            evidence.record({"task_family": "sk-" + "a" * 30, "outcome": "SUCCESS",
                             "source": "observed_runtime"}, state_dir=self.state)

    def test_corrupt_db_is_reported_not_silently_recreated(self) -> None:
        path = evidence.db_path(self.state)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"this is not a sqlite database")
        with self.assertRaises(evidence.EvidenceError):
            evidence.connect(state_dir=self.state)

    def test_reset_removes_only_local_db(self) -> None:
        evidence.import_seed(knowledge.load_historical_seed(), state_dir=self.state)
        result = evidence.reset_local(self.state, confirm=True)
        self.assertTrue(result["removed"])
        self.assertFalse(evidence.db_path(self.state).exists())
        self.assertTrue(knowledge.HISTORICAL_SEED_PATH.exists())  # shared knowledge untouched


class KnowledgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.defaults_path = pathlib.Path(self.tmp.name) / "learned-defaults.yaml"
        shutil.copy(knowledge.LEARNED_DEFAULTS_PATH, self.defaults_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_historical_seed_schema(self) -> None:
        rows = knowledge.load_historical_seed()
        self.assertGreaterEqual(len(rows), 6)
        ids = [row["observation_id"] for row in rows]
        self.assertEqual(len(ids), len(set(ids)))
        for row in rows:
            self.assertEqual("historical_verified", row["source"])
            self.assertIn(row["outcome"], knowledge.EVIDENCE_OUTCOMES)

    def test_lesson_and_failure_schemas(self) -> None:
        lessons = knowledge.load_lessons()
        self.assertTrue(all(lesson["surface_neutral"] for lesson in lessons))
        failures = knowledge.load_failures()
        self.assertTrue(all("intelligence_escalation" in row for row in failures))
        self.assertTrue(all("surface" not in row for row in failures))  # tool-neutral

    def test_failure_classification(self) -> None:
        self.assertFalse(knowledge.classify_failure("PROVIDER_503")["intelligence_escalation"])
        self.assertFalse(knowledge.classify_failure("RATE_LIMIT")["intelligence_escalation"])
        self.assertFalse(knowledge.classify_failure("TIMEOUT")["intelligence_escalation"])
        self.assertTrue(knowledge.classify_failure("REASONING_INSUFFICIENT")["intelligence_escalation"])

    def test_learned_defaults_start_conservative(self) -> None:
        data = knowledge.load_learned_defaults()
        self.assertEqual([], data["defaults"])
        self.assertGreaterEqual(data["policy"]["min_samples"], 2)
        self.assertTrue(data["policy"]["require_human_review"])

    def _candidate(self, **overrides) -> dict:
        base = {"destination": "learned-default", "task_family": "demo_family", "tier": "FAST",
                "effort": "low", "samples": 5, "success_ratio": 0.9, "reviewer": "reviewer",
                "human_accepted": True, "lesson": "repeated success at low reasoning"}
        base.update(overrides)
        return base

    def test_promotion_dry_run_then_apply(self) -> None:
        dry = knowledge.promote(self._candidate(), apply=False, defaults_path=self.defaults_path)
        self.assertFalse(dry["applied"])
        self.assertEqual([], knowledge.load_learned_defaults(self.defaults_path)["defaults"])
        applied = knowledge.promote(self._candidate(), apply=True, defaults_path=self.defaults_path)
        self.assertTrue(applied["applied"])
        default = knowledge.learned_default("demo_family", knowledge.load_learned_defaults(self.defaults_path))
        self.assertEqual("FAST", default["tier"])

    def test_promotion_requires_review(self) -> None:
        with self.assertRaises(knowledge.KnowledgeError):
            knowledge.promote(self._candidate(human_accepted=False), defaults_path=self.defaults_path)

    def test_promotion_rejects_single_sample_overgeneralization(self) -> None:
        with self.assertRaises(knowledge.KnowledgeError):
            knowledge.promote(self._candidate(samples=1), defaults_path=self.defaults_path)

    def test_promotion_rejects_secrets_and_raw_like_text(self) -> None:
        with self.assertRaises(knowledge.KnowledgeError):
            knowledge.promote(self._candidate(lesson="sk-" + "a" * 30), defaults_path=self.defaults_path)
        with self.assertRaises(knowledge.KnowledgeError):
            knowledge.promote(self._candidate(lesson="line one\nline two"), defaults_path=self.defaults_path)
        with self.assertRaises(knowledge.KnowledgeError):
            knowledge.promote(self._candidate(lesson="x" * (knowledge.MAX_SHARED_TEXT + 1)),
                              defaults_path=self.defaults_path)


class RoutingIntegrationTests(unittest.TestCase):
    def test_historical_evidence_and_lessons(self) -> None:
        family = knowledge.task_family_for("design a cross-machine config resolver")
        self.assertEqual("configuration_resolution", family)
        self.assertTrue(knowledge.observations_for_family(family))
        self.assertTrue(knowledge.relevant_lessons("align the canonical graphify runtime path"))

    def test_historical_seed_does_not_change_floor(self) -> None:
        rec = routing.recommend("", task_class="CORRECTNESS_SENSITIVE")
        rec["task_family"] = "configuration_resolution"
        before = (rec["recommended_tier"], rec["recommended_effort"])
        knowledge.apply_learned_default(rec)  # no approved defaults
        self.assertEqual(before, (rec["recommended_tier"], rec["recommended_effort"]))
        self.assertFalse(rec["learned_default_applied"])

    def test_approved_default_can_lower_within_floor(self) -> None:
        defaults = {"schema_version": 1,
                    "policy": {"min_samples": 3, "min_success_ratio": 0.8,
                               "safety_floor": {"MECHANICAL": "FAST", "NORMAL": "FAST",
                                                "CORRECTNESS_SENSITIVE": "STRONG", "CRITICAL": "MAX"},
                               "tier_order": list(routing.DEFAULT_TIER_ORDER)},
                    "defaults": [{"task_family": "demo", "tier": "FAST", "effort": "low", "approved": True}]}
        rec = routing.recommend("", task_class="NORMAL")
        rec["task_family"] = "demo"
        knowledge.apply_learned_default(rec, defaults=defaults)
        self.assertEqual("FAST", rec["recommended_tier"])
        self.assertTrue(rec["learned_default_applied"])

    def test_safety_floor_wins_over_weaker_default(self) -> None:
        defaults = {"schema_version": 1,
                    "policy": {"safety_floor": {"CORRECTNESS_SENSITIVE": "STRONG", "CRITICAL": "MAX"},
                               "tier_order": list(routing.DEFAULT_TIER_ORDER)},
                    "defaults": [{"task_family": "demo", "tier": "FAST", "effort": "low", "approved": True}]}
        rec = routing.recommend("", task_class="CORRECTNESS_SENSITIVE")
        rec["task_family"] = "demo"
        knowledge.apply_learned_default(rec, defaults=defaults)
        self.assertEqual("STRONG", rec["recommended_tier"])  # floor wins

    def test_user_override_wins_over_default(self) -> None:
        defaults = {"schema_version": 1,
                    "policy": {"safety_floor": {"MECHANICAL": "FAST"},
                               "tier_order": list(routing.DEFAULT_TIER_ORDER)},
                    "defaults": [{"task_family": "demo", "tier": "FAST", "effort": "low", "approved": True}]}
        rec = routing.recommend("", task_class="MECHANICAL", requested_tier="MAX")
        rec["task_family"] = "demo"
        knowledge.apply_learned_default(rec, defaults=defaults)
        self.assertEqual("MAX", rec["recommended_tier"])


class CrossSurfaceTests(unittest.TestCase):
    def test_lessons_are_tool_neutral_and_shared(self) -> None:
        lesson_ids = {lesson["lesson_id"] for lesson in knowledge.load_lessons()}
        self.assertIn("runtime-state-local", lesson_ids)
        # A lesson promoted for one surface is readable regardless of the querying surface.
        self.assertTrue(knowledge.relevant_lessons("keep runtime state local"))
        self.assertTrue(all("surface" not in row for row in knowledge.load_failures()))

    def test_failure_lesson_is_surface_neutral(self) -> None:
        lessons = knowledge.failure_lessons("PROVIDER_503")
        self.assertEqual(1, len(lessons))
        self.assertFalse(lessons[0]["intelligence_escalation"])


class NoTokenTests(unittest.TestCase):
    def test_sources_make_no_network_calls(self) -> None:
        for path in (ROOT / "scripts/common/evidence.py", ROOT / "scripts/common/derived_knowledge.py", LEARN):
            source = path.read_text(encoding="utf-8")
            for needle in ("urllib", "http.client", "requests", "socket"):
                self.assertNotIn(needle, source, f"{path.name} references {needle}")

    def test_learn_cli_is_offline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {k: v for k, v in os.environ.items() if "API_KEY" not in k and "TOKEN" not in k}
            env["LAI_STATE_DIR"] = tmp
            result = subprocess.run([str(LEARN), "status", "--json"], text=True, capture_output=True, env=env)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_route_json_exposes_knowledge_fields(self) -> None:
        result = subprocess.run([str(ROUTE), "--task", "design a cross-machine config resolver", "--json"],
                                text=True, capture_output=True)
        rec = json.loads(result.stdout)
        for key in ("task_family", "historical_evidence", "relevant_lessons", "learned_default"):
            self.assertIn(key, rec)


if __name__ == "__main__":
    unittest.main()
