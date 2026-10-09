"""Outcome Learning V0.2: bounded outcome capture, aggregation, candidates, storage
observability, cross-surface behavior, and portability. No model/network calls."""

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
import evidence  # noqa: E402
import outcomes  # noqa: E402
import derived_knowledge  # noqa: E402


LEARN = ROOT / "scripts" / "learn"


def row(**kw) -> dict:
    base = {"observation_id": kw.get("observation_id", os.urandom(6).hex()),
            "recorded_at": kw.get("recorded_at", "2026-10-09T00:00:00Z"),
            "task_family": "codex_model_availability", "task_class": "NORMAL", "surface": "opencode",
            "provider": None, "model": None, "native_variant": None, "normalized_effort": None,
            "initial_tier": "BALANCED", "initial_effort": "MEDIUM", "final_tier": "BALANCED",
            "final_effort": "MEDIUM", "outcome": "SUCCESS", "failure_category": None,
            "source": "observed_runtime", "escalated": False}
    base.update(kw)
    return base


class OutcomeCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = pathlib.Path(self.tmp.name) / "state"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_start_finish_creates_one_final_row(self) -> None:
        decision = evidence.start(task_family="safe_git_operation", initial_tier="FAST",
                                  initial_effort="LOW", state_dir=self.state)
        evidence.finish(decision, outcome="SUCCESS", completion_signal="task_finalized",
                        final_tier="FAST", final_effort="LOW", state_dir=self.state)
        self.assertEqual(1, evidence.query(state_dir=self.state)["total"])
        self.assertEqual(0, evidence.status(state_dir=self.state)["pending"])

    def test_duplicate_finalization_refused(self) -> None:
        decision = evidence.start(task_family="safe_git_operation", state_dir=self.state)
        evidence.finish(decision, outcome="SUCCESS", completion_signal="task_finalized",
                        state_dir=self.state)
        with self.assertRaises(evidence.EvidenceError):
            evidence.finish(decision, outcome="SUCCESS", completion_signal="task_finalized",
                            state_dir=self.state)

    def test_success_requires_completion_signal(self) -> None:
        with self.assertRaises(evidence.EvidenceError):
            evidence.outcome(decision_id="d1", outcome="SUCCESS", task_family="x", state_dir=self.state)
        evidence.outcome(decision_id="d2", outcome="SUCCESS", completion_signal="tests_passed",
                         task_family="x", state_dir=self.state)

    def test_unknown_outcome_allowed(self) -> None:
        evidence.outcome(decision_id="u1", outcome="UNKNOWN", task_family="x", state_dir=self.state)
        self.assertEqual(1, evidence.query(state_dir=self.state)["unknown"])

    def test_retry_escalation_correlated_in_one_row(self) -> None:
        decision = evidence.start(task_family="configuration_resolution", initial_tier="BALANCED",
                                  initial_effort="MEDIUM", state_dir=self.state)
        evidence.finish(decision, outcome="SUCCESS", completion_signal="acceptance_passed",
                        final_tier="STRONG", final_effort="HIGH", attempt_count=2, state_dir=self.state)
        records = evidence.all_rows(state_dir=self.state)
        self.assertEqual(1, len(records))
        self.assertEqual(("BALANCED", "STRONG"), (records[0]["initial_tier"], records[0]["final_tier"]))
        self.assertEqual(1, records[0]["escalated"])
        self.assertEqual(2, records[0]["attempt_count"])

    def test_user_override_captured(self) -> None:
        evidence.outcome(decision_id="o1", outcome="SUCCESS", completion_signal="task_finalized",
                         task_family="x", initial_tier="BALANCED", final_tier="STRONG",
                         user_override_tier="STRONG", state_dir=self.state)
        self.assertEqual("STRONG", evidence.all_rows(state_dir=self.state)[0]["user_override_tier"])

    def test_no_raw_prompt_field(self) -> None:
        with self.assertRaises(evidence.EvidenceError):
            evidence.record({"task_family": "x", "outcome": "SUCCESS", "source": "observed_runtime",
                             "completion_signal": "task_finalized", "prompt": "raw"}, state_dir=self.state)

    def test_decision_id_is_opaque(self) -> None:
        decision = evidence.start(task_family="safe_git_operation", state_dir=self.state)
        self.assertTrue(decision)
        self.assertNotIn("git", decision)


class AggregationTests(unittest.TestCase):
    def test_success_and_escalation(self) -> None:
        rows = [row(outcome="SUCCESS"), row(outcome="FAILURE", failure_category="REASONING_INSUFFICIENT", escalated=True)]
        result = outcomes.family_learning("codex_model_availability", rows)
        self.assertEqual((2, 1, 1, 1), (result["samples"], result["success"],
                                       result["reasoning_failures"], result["escalations"]))

    def test_infra_failure_not_counted_as_intelligence(self) -> None:
        rows = [row(outcome="FAILURE", failure_category="PROVIDER_503"),
                row(outcome="FAILURE", failure_category="RATE_LIMIT")]
        result = outcomes.family_learning("codex_model_availability", rows)
        self.assertEqual(0, result["reasoning_failures"])
        self.assertEqual(2, result["non_intelligence_failures"])

    def test_historical_vs_runtime_separation(self) -> None:
        rows = [row(source="observed_runtime"), row(source="historical_verified", provider=None)]
        result = outcomes.family_learning("codex_model_availability", rows)
        self.assertEqual((2, 1), (result["samples"], result["observed_runtime"]))

    def test_tier_and_effort_aggregation(self) -> None:
        rows = [row(final_tier="FAST", final_effort="LOW"), row(final_tier="FAST", final_effort="LOW")]
        result = outcomes.family_learning("codex_model_availability", rows)
        self.assertEqual(2, result["success_by_final_tier"]["FAST"])
        self.assertEqual(2, result["success_by_effort"]["LOW"])

    def test_model_variant_and_surface_aggregation(self) -> None:
        rows = [row(surface="opencode", provider="deepseek", model="m", native_variant="high",
                    normalized_effort="HIGH"),
                row(surface="codex_cli", provider="codex", model="g", native_variant="xhigh",
                    normalized_effort="VERY_HIGH")]
        models = outcomes.model_performance(rows)
        self.assertEqual(2, len(models))
        self.assertEqual({"opencode", "codex_cli"}, {m["surface"] for m in models})
        self.assertTrue(all(m["insufficient"] for m in models))


class CandidateTests(unittest.TestCase):
    def test_cheaper_tier_candidate(self) -> None:
        rows = [row(final_tier="FAST", final_effort="LOW", success=1) for _ in range(5)]
        found = outcomes.candidates(rows)["candidates"]
        kinds = {c["type"] for c in found}
        self.assertIn("CHEAPER_TIER_SUFFICIENT", kinds)

    def test_lower_effort_candidate(self) -> None:
        rows = [row(final_tier="BALANCED", final_effort="LOW") for _ in range(5)]
        kinds = {c["type"] for c in outcomes.candidates(rows)["candidates"]}
        self.assertIn("LOWER_EFFORT_SUFFICIENT", kinds)

    def test_current_tier_too_weak(self) -> None:
        rows = [row(outcome="FAILURE", failure_category="REASONING_INSUFFICIENT", escalated=True)
                for _ in range(2)]
        candidates = {c["type"]: c for c in outcomes.candidates(rows)["candidates"]}
        self.assertIn("CURRENT_TIER_TOO_WEAK", candidates)

    def test_recurring_non_intelligence_failure(self) -> None:
        rows = [row(outcome="FAILURE", failure_category="PROVIDER_503") for _ in range(3)]
        candidates = {c["type"]: c for c in outcomes.candidates(rows)["candidates"]}
        self.assertIn("RECURRING_NON_INTELLIGENCE_FAILURE", candidates)
        self.assertNotIn("CURRENT_TIER_TOO_WEAK", candidates)

    def test_insufficient_evidence(self) -> None:
        kinds = {c["type"] for c in outcomes.candidates([row()])["candidates"]}
        self.assertIn("INSUFFICIENT_EVIDENCE", kinds)

    def test_critical_cannot_downgrade(self) -> None:
        rows = [row(task_family="security_sensitive_design", task_class="CRITICAL",
                    final_tier="FAST", final_effort="LOW") for _ in range(6)]
        found = outcomes.candidates(rows)["candidates"]
        cheaper = [c for c in found if c["type"] == "CHEAPER_TIER_SUFFICIENT"]
        self.assertTrue(cheaper)
        self.assertTrue(all("CRITICAL never auto-downgrades" in c["blocking"] for c in cheaper))

    def test_single_sample_no_unsafe_change(self) -> None:
        found = outcomes.candidates([row(final_tier="FAST", final_effort="LOW")])["candidates"]
        self.assertEqual({"INSUFFICIENT_EVIDENCE"}, {c["type"] for c in found})

    def test_model_variant_effective_and_unreliable(self) -> None:
        effective = [row(provider="p", model="good", native_variant="high") for _ in range(3)]
        unreliable = [row(provider="p", model="bad", outcome="FAILURE",
                          failure_category="REASONING_INSUFFICIENT", escalated=True) for _ in range(3)]
        kinds = {c["type"] for c in outcomes.candidates(effective + unreliable)["candidates"]}
        self.assertIn("MODEL_VARIANT_EFFECTIVE", kinds)
        self.assertIn("MODEL_VARIANT_UNRELIABLE", kinds)


class StorageObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = pathlib.Path(self.tmp.name) / "state"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_status_empty_db(self) -> None:
        result = evidence.status(state_dir=self.state)
        self.assertFalse(result["exists"])
        self.assertEqual(0, result["observations"])
        self.assertEqual(0, result["db_size_bytes"])

    def test_status_seeded_db_has_size_and_span(self) -> None:
        evidence.import_seed(derived_knowledge.load_historical_seed(), state_dir=self.state)
        result = evidence.status(state_dir=self.state)
        self.assertGreater(result["db_size_bytes"], 0)
        self.assertIsNotNone(result["oldest"])
        self.assertIsNotNone(result["newest"])

    def test_prune_dry_run_does_not_mutate(self) -> None:
        evidence.import_seed(derived_knowledge.load_historical_seed(), state_dir=self.state)
        before = evidence.status(state_dir=self.state)["observations"]
        report = outcomes.prune_dry_run(state_dir=self.state)
        self.assertTrue(report["dry_run"])
        self.assertFalse(report["policy"]["automatic_prune"])
        self.assertEqual(before, evidence.status(state_dir=self.state)["observations"])

    def test_prunable_estimate_counts_redundant_successes(self) -> None:
        for index in range(55):
            evidence.outcome(decision_id=f"s{index}", outcome="SUCCESS", completion_signal="task_finalized",
                             task_family="safe_git_operation", final_tier="FAST", final_effort="LOW",
                             state_dir=self.state)
        report = outcomes.prune_dry_run(keep_per_group=50, state_dir=self.state)
        self.assertEqual(5, report["eligible_rows"])


class CrossSurfaceTests(unittest.TestCase):
    def test_all_surfaces_share_one_local_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp) / "state"
            for surface in ("opencode", "codex_cli", "codex_vscode", "codex_desktop"):
                evidence.outcome(decision_id=f"d-{surface}", outcome="SUCCESS", completion_signal="task_finalized",
                                 task_family="safe_git_operation", surface=surface, state_dir=state)
            aggregate = outcomes.aggregate(state_dir=state)
            self.assertEqual({"opencode", "codex_cli", "codex_vscode", "codex_desktop"},
                             set(aggregate["by_surface"]))
            self.assertEqual(4, outcomes.family_learning("safe_git_operation", state_dir=state)["observed_runtime"])


class PortabilityTests(unittest.TestCase):
    def test_fictitious_models_preserved_without_routing_change(self) -> None:
        rows = [row(provider="future-provider", model="future-model-x", native_variant="xhigh",
                    normalized_effort="VERY_HIGH")]
        models = outcomes.model_performance(rows)
        self.assertEqual(("future-provider", "future-model-x", "xhigh", "VERY_HIGH"),
                         (models[0]["provider"], models[0]["model"], models[0]["native_variant"],
                          models[0]["normalized_effort"]))
        # unknown variant is preserved, never guessed
        self.assertEqual("xhigh", models[0]["native_variant"])


class CliTests(unittest.TestCase):
    def _run(self, *args: str, state: str) -> subprocess.CompletedProcess[str]:
        env = os.environ | {"LAI_STATE_DIR": state}
        return subprocess.run([str(LEARN), *args], text=True, capture_output=True, env=env)

    def test_start_finish_models_prune_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            started = self._run("start", "--task-family", "safe_git_operation", "--tier", "FAST",
                                "--effort", "LOW", "--json", state=tmp)
            decision = json.loads(started.stdout)["decision_id"]
            finalized = self._run("finish", "--decision-id", decision, "--outcome", "SUCCESS",
                                  "--completion-signal", "task_finalized", "--final-tier", "FAST",
                                  "--final-effort", "LOW", "--json", state=tmp)
            self.assertEqual(0, finalized.returncode, finalized.stderr)
            models = self._run("models", "--json", state=tmp)
            self.assertEqual(0, models.returncode, models.stderr)
            prune = self._run("prune", "--dry-run", "--json", state=tmp)
            self.assertTrue(json.loads(prune.stdout)["dry_run"])

    def test_prune_apply_is_deferred(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run("prune", "--apply", state=tmp)
            self.assertEqual(2, result.returncode)
            self.assertIn("deferred", result.stderr)

    def test_learn_is_offline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {k: v for k, v in os.environ.items() if "API_KEY" not in k and "TOKEN" not in k}
            env["LAI_STATE_DIR"] = tmp
            result = subprocess.run([str(LEARN), "status", "--json"], text=True, capture_output=True, env=env)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_sources_make_no_network_calls(self) -> None:
        for name in ("evidence.py", "outcomes.py"):
            source = (ROOT / "scripts/common" / name).read_text(encoding="utf-8")
            for needle in ("urllib", "http.client", "requests", "socket"):
                self.assertNotIn(needle, source, f"{name} references {needle}")


if __name__ == "__main__":
    unittest.main()
