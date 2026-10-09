"""Adaptive model routing V0.1: deterministic classification, routing, escalation,
CLI, and safety. No model or network calls anywhere in this module."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import routing  # noqa: E402
import infra  # noqa: E402


ROUTE = ROOT / "scripts" / "route"


class ClassificationTests(unittest.TestCase):
    def test_mechanical_examples(self) -> None:
        for task in ("git status and push already-reviewed commits",
                     "fix a typo in documentation",
                     "rename the config key",
                     "rerun tests for the parser"):
            self.assertEqual("MECHANICAL", routing.classify(task), task)

    def test_normal_examples(self) -> None:
        for task in ("implement a bounded retry helper",
                     "add one bounded config field with tests",
                     "refactor the loader without changing behavior"):
            self.assertEqual("NORMAL", routing.classify(task), task)

    def test_correctness_sensitive_examples(self) -> None:
        for task in ("design a cross-machine config resolver",
                     "fix a concurrency race condition in the cache",
                     "add a database migration for the new column"):
            self.assertEqual("CORRECTNESS_SENSITIVE", routing.classify(task), task)

    def test_critical_examples(self) -> None:
        for task in ("review a security-sensitive authentication architecture",
                     "resolve repeated failure in a cross-system data integrity path"):
            self.assertEqual("CRITICAL", routing.classify(task), task)

    def test_uncertain_input_chooses_safer_class(self) -> None:
        self.assertEqual("NORMAL", routing.classify("do the thing"))
        self.assertEqual("NORMAL", routing.classify(""))

    def test_explicit_override_wins(self) -> None:
        self.assertEqual("CRITICAL", routing.classify("git status", override="critical"))
        self.assertEqual("MECHANICAL", routing.classify("design security architecture", override="Mechanical"))

    def test_unknown_override_is_rejected(self) -> None:
        with self.assertRaises(routing.RoutingError):
            routing.classify("x", override="banana")


class RoutingTests(unittest.TestCase):
    def test_class_to_tier_and_effort(self) -> None:
        expected = {
            "MECHANICAL": ("FAST", "low"),
            "NORMAL": ("BALANCED", "medium"),
            "CORRECTNESS_SENSITIVE": ("STRONG", "high"),
            "CRITICAL": ("MAX", "max"),
        }
        for cls, (tier, effort) in expected.items():
            rec = routing.recommend("", task_class=cls)
            self.assertEqual(tier, rec["recommended_tier"], cls)
            self.assertEqual(effort, rec["recommended_effort"], cls)

    def test_role_and_model_come_from_roles_yaml(self) -> None:
        rec = routing.recommend("rename a variable")
        self.assertEqual("student", rec["role"])
        self.assertEqual("qwen3.6:27b-coding", rec["model"])

    def test_requested_effort_floor_is_preserved(self) -> None:
        rec = routing.recommend("rename a variable", requested_effort="high")
        self.assertEqual("high", rec["recommended_effort"])
        self.assertEqual("STRONG", rec["recommended_tier"])

    def test_requested_tier_floor_is_preserved(self) -> None:
        rec = routing.recommend("rename a variable", requested_tier="MAX")
        self.assertEqual("MAX", rec["recommended_tier"])
        self.assertEqual("max", rec["recommended_effort"])

    def test_lower_request_never_downgrades(self) -> None:
        rec = routing.recommend("", task_class="CRITICAL", requested_effort="low")
        self.assertEqual("MAX", rec["recommended_tier"])
        self.assertEqual("max", rec["recommended_effort"])

    def test_critical_floor_preserved(self) -> None:
        rec = routing.recommend("", task_class="CRITICAL")
        self.assertEqual("MAX", rec["recommended_tier"])
        self.assertEqual("max", rec["recommended_effort"])
        self.assertIsNone(rec["escalation_to"])

    def test_surface_capability_is_recommend_only(self) -> None:
        rec = routing.recommend("rename", surface="codex-cli")
        self.assertEqual("RECOMMEND_ONLY", rec["capability"])


class EscalationTests(unittest.TestCase):
    def test_success_stops(self) -> None:
        step = routing.escalation_step("MECHANICAL", succeeded=True)
        self.assertEqual("stop", step["action"])

    def test_mechanical_failure_retries_then_escalates(self) -> None:
        first = routing.escalation_step("MECHANICAL", same_tier_attempts=0)
        self.assertEqual("retry", first["action"])
        second = routing.escalation_step("MECHANICAL", same_tier_attempts=1)
        self.assertEqual("escalate", second["action"])
        self.assertEqual("NORMAL", second["escalation_to"])

    def test_repeated_failure_does_not_loop(self) -> None:
        # Many failures still produce a single bounded step, never an unbounded retry.
        step = routing.escalation_step("MECHANICAL", same_tier_attempts=50)
        self.assertEqual("escalate", step["action"])
        self.assertEqual("NORMAL", step["escalation_to"])

    def test_escalation_chain_is_bounded(self) -> None:
        self.assertEqual("CORRECTNESS_SENSITIVE",
                         routing.escalation_step("NORMAL", same_tier_attempts=9)["escalation_to"])
        self.assertEqual("CRITICAL",
                         routing.escalation_step("CORRECTNESS_SENSITIVE", same_tier_attempts=9)["escalation_to"])

    def test_max_tier_stops_rather_than_retrying(self) -> None:
        step = routing.escalation_step("CRITICAL", same_tier_attempts=99)
        self.assertEqual("stop", step["action"])
        self.assertIsNone(step["escalation_to"])


class PolicyTests(unittest.TestCase):
    def test_schema_and_references(self) -> None:
        policy = routing.load_policy()
        self.assertEqual(1, policy["schema_version"])
        for cls, entry in policy["task_classes"].items():
            self.assertIn(cls, routing.CLASSES)
            self.assertIn(entry["tier"], policy["tiers"])
            self.assertIn(entry["effort"], policy["effort_order"])
            self.assertTrue(entry["escalates_to"] is None or entry["escalates_to"] in policy["task_classes"])

    def test_tiers_reference_real_roles(self) -> None:
        policy = routing.load_policy()
        roles = {role["id"] for role in infra.load_json(routing.ROLES_PATH)["roles"]}
        for tier, entry in policy["tiers"].items():
            self.assertIn(entry["role"], roles, tier)

    def test_all_surfaces_declared(self) -> None:
        policy = routing.load_policy()
        self.assertEqual(
            {"opencode", "codex-cli", "codex-vscode", "codex-desktop"},
            set(policy["surfaces"]))
        self.assertTrue(all(v == "RECOMMEND_ONLY" for v in policy["surfaces"].values()))


class CliTests(unittest.TestCase):
    def cli(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(ROUTE), *args], text=True, capture_output=True, env=env)

    def test_route_human_output(self) -> None:
        result = self.cli("--task", "rename a config key")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("CLASSIFICATION: MECHANICAL", result.stdout)
        self.assertIn("MODEL TIER: FAST", result.stdout)
        self.assertIn("EFFORT: low", result.stdout)

    def test_route_json_is_deterministic(self) -> None:
        one = self.cli("--task", "design concurrency-safe cache", "--json")
        two = self.cli("--task", "design concurrency-safe cache", "--json")
        self.assertEqual(one.stdout, two.stdout)
        rec = json.loads(one.stdout)
        for key in ("task_class", "recommended_tier", "recommended_effort", "reason",
                    "escalation_from", "escalation_to"):
            self.assertIn(key, rec)

    def test_route_is_offline(self) -> None:
        env = {k: v for k, v in os.environ.items() if "API_KEY" not in k and "TOKEN" not in k}
        result = self.cli("--task", "fix a typo", "--json", env=env)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_route_never_echoes_the_raw_task_or_secret(self) -> None:
        secret = "sk-" + "a" * 30
        result = self.cli("--task", f"rotate {secret} token in the {secret} config")
        self.assertNotIn(secret, result.stdout)
        self.assertNotIn("sk-", result.stdout)

    def test_routing_sources_make_no_network_calls(self) -> None:
        for path in (ROOT / "scripts/common/routing.py", ROUTE):
            source = path.read_text(encoding="utf-8")
            for needle in ("urllib", "http.client", "requests", "socket"):
                self.assertNotIn(needle, source, f"{path.name} references {needle}")


if __name__ == "__main__":
    unittest.main()
