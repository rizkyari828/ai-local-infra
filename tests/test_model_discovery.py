"""Model capability discovery V0.1: catalog-only discovery, status model, effort
variants, registry portability, failure classification, CLI, and safety. No model
or network call is made anywhere in this module."""

from __future__ import annotations

import copy
import json
import os
import pathlib
import subprocess
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import discovery  # noqa: E402
import routing  # noqa: E402


MODELS = ROOT / "scripts" / "models"


def _config(**overrides) -> dict:
    config = copy.deepcopy(discovery.load_config())
    config.update(overrides)
    return config


def _snapshot(*records) -> dict:
    return {"records": list(records)}


def _roles(**entries) -> dict:
    return dict(entries)


class DiscoveryTests(unittest.TestCase):
    def test_known_model_from_registry(self) -> None:
        rows = discovery.discover(config=_config(), snapshot=_snapshot(),
                                  roles=_roles(r={"provider": "ollama", "model_reference": "m1"}))
        self.assertEqual(1, len(rows))
        self.assertEqual(("ollama", "m1", discovery.KNOWN), (rows[0]["provider"], rows[0]["model"], rows[0]["status"]))
        self.assertIn("registry", rows[0]["sources"])

    def test_unknown_model_is_unknown(self) -> None:
        cap = discovery.capability("nope", "nope", "opencode", config=_config(),
                                   snapshot=_snapshot(), roles=_roles())
        self.assertEqual(discovery.UNKNOWN, cap["status"])
        self.assertFalse(cap["known"])

    def test_multiple_providers(self) -> None:
        rows = discovery.discover(
            config=_config(), snapshot=_snapshot(),
            roles=_roles(a={"provider": "ollama", "model_reference": "qa"},
                         b={"provider": "deepseek", "model_reference": "ds"}))
        self.assertEqual({("ollama", "qa"), ("deepseek", "ds")},
                         {(row["provider"], row["model"]) for row in rows})

    def test_provider_alias_is_canonical(self) -> None:
        config = _config()
        self.assertEqual("anthropic", discovery.canonical_provider("claude", config))
        rows = discovery.discover(config=config, snapshot=_snapshot(),
                                  roles=_roles(r={"provider": "claude", "model_reference": "c1"}))
        self.assertEqual("anthropic", rows[0]["provider"])

    def test_surface_specific_availability(self) -> None:
        config = _config()
        roles = _roles(r={"provider": "ollama", "model_reference": "m1"})
        snap = _snapshot({"surface": "opencode", "provider": "ollama", "model": "m1",
                          "status": discovery.AVAILABLE, "source": "discovered", "variants": []})
        self.assertEqual(discovery.AVAILABLE,
                         discovery.capability("ollama", "m1", "opencode", config=config, snapshot=snap, roles=roles)["status"])
        self.assertEqual(discovery.UNKNOWN,
                         discovery.capability("ollama", "m1", "codex-cli", config=config, snapshot=snap, roles=roles)["status"])

    def test_shared_config_reuses_availability_not_verification(self) -> None:
        config = _config()
        roles = _roles(r={"provider": "ollama", "model_reference": "m1"})
        snap = _snapshot({"surface": "codex-cli", "provider": "ollama", "model": "m1",
                          "status": discovery.VERIFIED, "source": "observed_runtime",
                          "variants": [{"native": "high", "verified": True}]})
        # vscode shares codex-cli config
        shared = discovery.capability("ollama", "m1", "codex-vscode", config=config, snapshot=snap, roles=roles)
        self.assertEqual(discovery.AVAILABLE, shared["status"])
        self.assertTrue(str(shared["source"]).startswith("shared_config:"))
        # verification did NOT transfer
        self.assertEqual(discovery.UNKNOWN,
                         discovery.effort_status("ollama", "m1", "codex-vscode", "HIGH",
                                                 config=config, snapshot=snap, roles=roles))
        self.assertEqual(discovery.VERIFIED,
                         discovery.effort_status("ollama", "m1", "codex-cli", "HIGH",
                                                 config=config, snapshot=snap, roles=roles))

    def test_discovery_sources_make_no_network_calls(self) -> None:
        for path in (ROOT / "scripts/common/discovery.py", MODELS):
            source = path.read_text(encoding="utf-8")
            for needle in ("urllib", "http.client", "requests", "socket", "subprocess"):
                self.assertNotIn(needle, source, f"{path.name} references {needle}")


class VariantTests(unittest.TestCase):
    def test_different_effort_vocabularies(self) -> None:
        config = _config()
        self.assertEqual("VERY_HIGH", discovery.normalize_effort("anthropic", "xhigh", config))
        self.assertEqual("HIGH", discovery.normalize_effort("deepseek", "high", config))
        self.assertEqual("VERY_HIGH", discovery.normalize_effort("openai", "xhigh", config))

    def test_native_variant_is_preserved(self) -> None:
        config = _config()
        roles = _roles(r={"provider": "anthropic", "model_reference": "c1"})
        snap = _snapshot({"surface": "opencode", "provider": "anthropic", "model": "c1",
                          "status": discovery.AVAILABLE, "source": "discovered",
                          "variants": [{"native": "xhigh", "verified": False}]})
        cap = discovery.capability("anthropic", "c1", "opencode", config=config, snapshot=snap, roles=roles)
        self.assertEqual("xhigh", cap["variants"][0]["native"])
        self.assertEqual("VERY_HIGH", cap["variants"][0]["normalized"])

    def test_unknown_effort_variant_is_unknown(self) -> None:
        self.assertIsNone(discovery.normalize_effort("anthropic", "nonsense", _config()))
        config = _config()
        roles = _roles(r={"provider": "anthropic", "model_reference": "c1"})
        snap = _snapshot({"surface": "opencode", "provider": "anthropic", "model": "c1",
                          "status": discovery.AVAILABLE, "source": "discovered",
                          "variants": [{"native": "nonsense", "verified": False}]})
        cap = discovery.capability("anthropic", "c1", "opencode", config=config, snapshot=snap, roles=roles)
        self.assertEqual("UNKNOWN", cap["variants"][0]["normalized"])

    def test_one_verified_variant_does_not_verify_others(self) -> None:
        config = _config()
        roles = _roles(r={"provider": "ollama", "model_reference": "m1"})
        snap = _snapshot({"surface": "opencode", "provider": "ollama", "model": "m1",
                          "status": discovery.VERIFIED, "source": "observed_runtime",
                          "variants": [{"native": "high", "verified": True},
                                       {"native": "low", "verified": False}]})
        self.assertEqual(discovery.VERIFIED, discovery.effort_status("ollama", "m1", "opencode", "HIGH",
                                                                     config=config, snapshot=snap, roles=roles))
        self.assertEqual(discovery.UNVERIFIED, discovery.effort_status("ollama", "m1", "opencode", "LOW",
                                                                       config=config, snapshot=snap, roles=roles))
        self.assertEqual(discovery.UNKNOWN, discovery.effort_status("ollama", "m1", "opencode", "MAX",
                                                                    config=config, snapshot=snap, roles=roles))


class RegistryPortabilityTests(unittest.TestCase):
    def test_config_only_model_replacement(self) -> None:
        rec_before = routing.recommend("", task_class="CORRECTNESS_SENSITIVE")
        rows_a = discovery.discover(config=_config(), snapshot=_snapshot(),
                                    roles=_roles(s={"provider": "provider-a", "model_reference": "model-a"}))
        rows_b = discovery.discover(config=_config(), snapshot=_snapshot(),
                                    roles=_roles(s={"provider": "provider-b", "model_reference": "model-b"}))
        self.assertEqual("model-a", rows_a[0]["model"])
        self.assertEqual("model-b", rows_b[0]["model"])
        # routing decision is unchanged by the model swap
        self.assertEqual(("STRONG", "high"), (rec_before["recommended_tier"], rec_before["recommended_effort"]))

    def test_qwen_like_model(self) -> None:
        rows = discovery.discover(config=_config(), snapshot=_snapshot(),
                                  roles=_roles(s={"provider": "ollama", "model_reference": "qwen3.6:27b-coding"}))
        self.assertEqual(("ollama", "qwen3.6:27b-coding"), (rows[0]["provider"], rows[0]["model"]))

    def test_claude_like_model(self) -> None:
        rows = discovery.discover(config=_config(), snapshot=_snapshot(),
                                  roles=_roles(s={"provider": "claude", "model_reference": "claude-opus-x"}))
        self.assertEqual(("anthropic", "claude-opus-x"), (rows[0]["provider"], rows[0]["model"]))

    def test_unknown_future_provider_and_model(self) -> None:
        rows = discovery.discover(config=_config(), snapshot=_snapshot(),
                                  roles=_roles(s={"provider": "future-provider", "model_reference": "future-model-x"}))
        self.assertEqual(("future-provider", "future-model-x"), (rows[0]["provider"], rows[0]["model"]))
        self.assertEqual(discovery.KNOWN, rows[0]["status"])

    def test_manual_override_provider_alias(self) -> None:
        config = _config(manual_overrides=[{"provider": "claude", "model": "c1",
                                            "surfaces": ["opencode"], "status": discovery.AVAILABLE,
                                            "efforts": ["high"]}])
        rows = discovery.discover(config=config, snapshot=_snapshot(), roles=_roles())
        self.assertEqual("anthropic", rows[0]["provider"])
        self.assertEqual(discovery.AVAILABLE, rows[0]["surfaces"]["opencode"]["status"])


class StatusTests(unittest.TestCase):
    def test_states_are_distinct(self) -> None:
        config = _config()
        roles = _roles(r={"provider": "ollama", "model_reference": "m1"})
        # KNOWN (catalog only) -> no surface evidence
        self.assertEqual(discovery.KNOWN, discovery.capability("ollama", "m1", config=config, snapshot=_snapshot(), roles=roles)["status"])
        snap = _snapshot({"surface": "opencode", "provider": "ollama", "model": "m1",
                          "status": discovery.AVAILABLE, "source": "discovered", "variants": []})
        # AVAILABLE (surface) but not VERIFIED
        self.assertEqual(discovery.AVAILABLE, discovery.capability("ollama", "m1", "opencode", config=config, snapshot=snap, roles=roles)["status"])
        self.assertEqual(discovery.UNKNOWN, discovery.effort_status("ollama", "m1", "opencode", "HIGH", config=config, snapshot=snap, roles=roles))

    def test_unavailable_and_unverified(self) -> None:
        config = _config()
        roles = _roles(r={"provider": "ollama", "model_reference": "m1"})
        snap = _snapshot({"surface": "opencode", "provider": "ollama", "model": "m1",
                          "status": discovery.UNAVAILABLE, "source": "observed_runtime",
                          "variants": [{"native": "high", "verified": False}]})
        self.assertEqual(discovery.UNAVAILABLE, discovery.capability("ollama", "m1", "opencode", config=config, snapshot=snap, roles=roles)["status"])
        self.assertEqual(discovery.UNAVAILABLE, discovery.effort_status("ollama", "m1", "opencode", "HIGH", config=config, snapshot=snap, roles=roles))


class FailureTests(unittest.TestCase):
    def test_account_model_unsupported_is_availability(self) -> None:
        result = discovery.classify_failure("ACCOUNT_MODEL_UNSUPPORTED")
        self.assertEqual("availability", result["scope"])
        self.assertFalse(result["intelligence_evidence"])

    def test_provider_503_not_model_weakness(self) -> None:
        self.assertFalse(discovery.classify_failure("PROVIDER_503")["intelligence_evidence"])

    def test_timeout_not_reasoning_failure(self) -> None:
        self.assertFalse(discovery.classify_failure("TIMEOUT")["intelligence_evidence"])

    def test_stale_session_is_lifecycle(self) -> None:
        self.assertEqual("lifecycle", discovery.classify_failure("STALE_SESSION")["scope"])

    def test_reasoning_insufficient_is_evidence(self) -> None:
        self.assertTrue(discovery.classify_failure("REASONING_INSUFFICIENT")["intelligence_evidence"])

    def test_unknown_category_is_not_evidence(self) -> None:
        self.assertFalse(discovery.classify_failure("WHATEVER")["intelligence_evidence"])


class CliTests(unittest.TestCase):
    def cli(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(MODELS), *args], text=True, capture_output=True, env=env)

    def test_detect_human_output(self) -> None:
        result = self.cli("detect", "--surface", "opencode")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("SURFACE: opencode", result.stdout)

    def test_list_json_is_deterministic(self) -> None:
        one = self.cli("list", "--json")
        two = self.cli("list", "--json")
        self.assertEqual(one.stdout, two.stdout)
        rows = json.loads(one.stdout)
        self.assertTrue(all({"provider", "model", "status", "surfaces", "variants"} <= set(r) for r in rows))

    def test_provider_alias_filter(self) -> None:
        result = self.cli("list", "--provider", "local", "--json")
        rows = json.loads(result.stdout)
        self.assertTrue(rows)
        self.assertTrue(all(r["provider"] == "ollama" for r in rows))

    def test_cli_is_offline(self) -> None:
        env = {k: v for k, v in os.environ.items() if "API_KEY" not in k and "TOKEN" not in k}
        result = self.cli("detect", "--json", env=env)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_cli_never_prints_secrets(self) -> None:
        secret = "sk-" + "a" * 30
        env = os.environ | {"DEEPSEEK_API_KEY": secret, "OPENCODE_TOKEN": secret}
        result = self.cli("list", env=env)
        self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertNotIn("sk-", result.stdout)


if __name__ == "__main__":
    unittest.main()
