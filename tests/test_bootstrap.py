from __future__ import annotations

import contextlib
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
import bootstrap  # noqa: E402
import secret_guard  # noqa: E402


class BootstrapTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.state = self.home / "state"
        self.codex = self.home / ".codex"
        self.codex.mkdir(parents=True)
        self.agents = self.codex / "AGENTS.md"
        self.config = self.codex / "config.toml"
        self.env = os.environ | {
            "HOME": str(self.home),
            "LAI_STATE_DIR": str(self.state),
            "LAI_MACHINE": "work-mac",
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @contextlib.contextmanager
    def patched(self, **extra):
        merged = dict(self.env)
        merged.update(extra)
        with unittest.mock.patch.dict(os.environ, merged):
            yield merged

    def run_cli(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(ROOT / "scripts/bootstrap"), *args], cwd=ROOT,
                              env=env or self.env, text=True, capture_output=True)

    def write_agents(self, text: str) -> None:
        self.agents.write_text(text, encoding="utf-8")

    def write_config(self) -> None:
        self.config.write_text(
            '[projects."/tmp/demo"]\n'
            'trust_level = "trusted"\n\n'
            '[mcp_servers.headroom]\n'
            'command = "/old/bin/headroom"\n'
            'args = ["mcp", "serve", "--proxy-url", "http://127.0.0.1:9999"]\n\n'
            '[mcp_servers.other]\n'
            'command = "/usr/bin/other"\n',
            encoding="utf-8",
        )

    def backup_manifests(self) -> list[pathlib.Path]:
        return sorted((self.state / "backups" / "bootstrap").glob("*/manifest.json"))


class MachineSelectionTests(BootstrapTestBase):
    def test_missing_lai_machine_is_blocked_and_does_not_mutate(self) -> None:
        self.write_agents("keep me\n")
        before = self.agents.read_bytes()
        env = {k: v for k, v in self.env.items() if k != "LAI_MACHINE"}
        result = self.run_cli("apply", env=env)
        self.assertEqual(2, result.returncode)
        self.assertIn("LAI_MACHINE is required", result.stderr)
        self.assertEqual(before, self.agents.read_bytes())
        self.assertFalse(self.state.exists())

    def test_unknown_machine_profile_is_rejected(self) -> None:
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.select_machine({"LAI_MACHINE": "nope"})


class StatusTests(BootstrapTestBase):
    def test_status_is_read_only(self) -> None:
        self.write_agents("# personal\n\nkeep me\n")
        self.write_config()
        agents_before = self.agents.read_bytes()
        config_before = self.config.read_bytes()
        result = self.run_cli("status")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(agents_before, self.agents.read_bytes())
        self.assertEqual(config_before, self.config.read_bytes())
        self.assertFalse((self.state / "backups").exists())
        self.assertIn("codex.guidance", result.stdout)

    def test_local_override_reported(self) -> None:
        (self.state).mkdir(parents=True, exist_ok=True)
        (self.state / "machine.local.yaml").write_text(
            json.dumps({"disabled_units": ["rtk.guidance"], "headroom": {"port": 9999}}),
            encoding="utf-8",
        )
        result = self.run_cli("status")
        self.assertIn("LOCAL-OVERRIDE", result.stdout)
        self.assertIn("rtk.guidance", result.stdout)


class ApplyTests(BootstrapTestBase):
    def test_apply_then_aligned_and_idempotent(self) -> None:
        self.write_agents("# personal\n\nkeep me\n")
        self.write_config()
        first = self.run_cli("apply")
        self.assertEqual(0, first.returncode, first.stderr)
        after_first = self.agents.read_bytes()
        self.assertIn(b"local-ai-infra managed: codex-guidance", after_first)
        status = self.run_cli("status")
        self.assertIn("ALIGNED", status.stdout)
        second = self.run_cli("apply")
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertIn("No action taken", second.stdout)
        self.assertEqual(after_first, self.agents.read_bytes())

    def test_dry_run_writes_nothing(self) -> None:
        self.write_agents("# personal\n")
        agents_before = self.agents.read_bytes()
        result = self.run_cli("apply", "--dry-run")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("DRY RUN", result.stdout)
        self.assertEqual(agents_before, self.agents.read_bytes())
        self.assertFalse((self.state / "backups").exists())
        self.assertFalse(self.config.exists())

    def test_drifted_block_is_rewritten_and_outside_content_preserved(self) -> None:
        self.write_agents("# personal\n\nkeep me\n")
        self.run_cli("apply")
        text = self.agents.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# personal\n\nkeep me\n"))
        tampered = text.replace("RTK: prefix shell commands", "TAMPERED")
        self.agents.write_text(tampered, encoding="utf-8")
        status = self.run_cli("status")
        self.assertIn("DRIFTED", status.stdout)
        self.run_cli("apply")
        repaired = self.agents.read_text(encoding="utf-8")
        self.assertTrue(repaired.startswith("# personal\n\nkeep me\n"))
        self.assertIn("RTK: prefix shell commands", repaired)
        self.assertNotIn("TAMPERED", repaired)

    def test_duplicate_markers_are_blocked_and_apply_refuses(self) -> None:
        self.write_agents(
            "<!-- >>> local-ai-infra managed: codex-guidance >>> -->\n"
            "one\n"
            "<!-- >>> local-ai-infra managed: codex-guidance >>> -->\n"
            "<!-- <<< local-ai-infra managed: codex-guidance <<< -->\n"
        )
        before = self.agents.read_bytes()
        status = self.run_cli("status")
        self.assertEqual(1, status.returncode)
        self.assertIn("BLOCKED", status.stdout)
        apply = self.run_cli("apply")
        self.assertEqual(1, apply.returncode)
        self.assertIn("refusing to apply", apply.stdout)
        self.assertEqual(before, self.agents.read_bytes())

    def test_unrelated_config_and_mcp_and_trust_preserved(self) -> None:
        self.write_config()
        self.run_cli("apply")
        config = self.config.read_text(encoding="utf-8")
        self.assertIn('[projects."/tmp/demo"]', config)
        self.assertIn('trust_level = "trusted"', config)
        self.assertIn("[mcp_servers.other]", config)
        self.assertIn('command = "/usr/bin/other"', config)
        self.assertIn('command = "%s"' % (self.home / ".local/bin/headroom"), config)
        self.assertNotIn("/old/bin/headroom", config)

    def test_backup_manifest_before_mutation(self) -> None:
        self.write_agents("# personal\n")
        self.write_config()
        self.run_cli("apply")
        manifests = self.backup_manifests()
        self.assertTrue(manifests)
        manifest = json.loads(manifests[-1].read_text(encoding="utf-8"))
        self.assertEqual("work-mac", manifest["machine"])
        paths = {item["path"] for item in manifest["files"]}
        self.assertIn(str(self.agents), paths)
        self.assertIn(str(self.config), paths)
        agents_item = next(item for item in manifest["files"] if item["path"] == str(self.agents))
        self.assertTrue(agents_item["existed"])
        self.assertTrue(agents_item["checksum"].startswith("sha256:"))

    def test_rollback_restores_originals_and_removes_created(self) -> None:
        self.write_agents("# personal\n\nkeep me\n")
        self.write_config()
        agents_original = self.agents.read_text(encoding="utf-8")
        config_original = self.config.read_text(encoding="utf-8")
        self.run_cli("apply")
        self.assertNotEqual(agents_original, self.agents.read_text(encoding="utf-8"))
        self.run_cli("rollback")
        self.assertEqual(agents_original, self.agents.read_text(encoding="utf-8"))
        self.assertEqual(config_original, self.config.read_text(encoding="utf-8"))

    def test_created_target_removed_on_rollback(self) -> None:
        self.write_agents("# personal\n")
        self.run_cli("apply")
        created = self.state / "headroom" / "desired.json"
        self.assertTrue(created.exists())
        self.run_cli("rollback")
        self.assertFalse(created.exists())

    def test_dry_run_rollback_previews_without_change(self) -> None:
        self.write_agents("# personal\n")
        self.run_cli("apply")
        applied = self.agents.read_bytes()
        result = self.run_cli("rollback", "--dry-run")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("DRY RUN", result.stdout)
        self.assertEqual(applied, self.agents.read_bytes())


class PrecedenceTests(BootstrapTestBase):
    def test_lifecycle_is_wrapper_not_service_manager(self) -> None:
        with self.patched(LAI_MACHINE="work-mac"):
            mac, _ = bootstrap.load_effective(self.env)
            self.assertEqual("wrapper", mac["headroom"]["lifecycle"])
            self.assertNotIn(mac["headroom"]["lifecycle"], {"launchd", "systemd"})
        with self.patched(LAI_MACHINE="home-wsl"):
            wsl, _ = bootstrap.load_effective(self.env | {"LAI_MACHINE": "home-wsl"})
            self.assertEqual("wrapper", wsl["headroom"]["lifecycle"])
            self.assertNotIn(wsl["headroom"]["lifecycle"], {"launchd", "systemd"})

    def test_service_manager_is_capability_only(self) -> None:
        with self.patched(LAI_MACHINE="work-mac"):
            mac, _ = bootstrap.load_effective(self.env)
            self.assertEqual(["launchd"], mac["lifecycle_capabilities"])
        with self.patched(LAI_MACHINE="home-wsl"):
            wsl, _ = bootstrap.load_effective(self.env | {"LAI_MACHINE": "home-wsl"})
            self.assertEqual(["systemd"], wsl["lifecycle_capabilities"])

    def test_local_override_wins_per_key(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "machine.local.yaml").write_text(
            json.dumps({"headroom": {"port": 9999}}), encoding="utf-8")
        with self.patched():
            effective, _ = bootstrap.load_effective(self.env)
        self.assertEqual(9999, effective["headroom"]["port"])
        self.assertEqual("127.0.0.1", effective["headroom"]["host"])

    def test_machine_difference_is_not_local_override(self) -> None:
        self.write_agents("# personal\n")
        with self.patched():
            plan, _ = bootstrap.build_plan(self.env)
        guidance = next(unit for unit in plan if unit["id"] == "codex.guidance")
        self.assertNotEqual("LOCAL-OVERRIDE", guidance["state"])

    def test_paths_expand_without_literal_tilde(self) -> None:
        with self.patched():
            plan, meta = bootstrap.build_plan(self.env)
        guidance = next(unit for unit in plan if unit["id"] == "codex.guidance")
        self.assertTrue(guidance["target"].startswith(str(self.home)))
        self.assertNotIn("~", guidance["target"])


class SecretGateTests(BootstrapTestBase):
    def test_obvious_secret_is_detected(self) -> None:
        findings = secret_guard.scan_text('api_key = "sk-abcdefghijklmnopqrstuvwxyz1234"')
        self.assertTrue(findings)

    def test_placeholder_is_allowed(self) -> None:
        self.assertEqual([], secret_guard.scan_text('api_key = "${DEEPSEEK_API_KEY}"'))
        self.assertEqual([], secret_guard.scan_text('token = "{{headroom.token}}"'))

    def test_generated_values_secret_blocks_apply(self) -> None:
        ctx = {"units": [{"id": "x", "mode": "generated-json",
                          "values": {"token": "ghp_" + "a" * 30}}]}
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.secret_gate(ctx)

    def test_repo_managed_sources_are_clean(self) -> None:
        self.assertEqual(0, bootstrap.cmd_secrets())


class RepositoryHygieneTests(unittest.TestCase):
    def test_no_graphify_graph_committed(self) -> None:
        result = subprocess.run(["git", "ls-files"], cwd=ROOT, text=True, capture_output=True)
        tracked = result.stdout.splitlines()
        offenders = [path for path in tracked if "graphify-out" in path or path.endswith("graph.json")]
        self.assertEqual([], offenders)

    def test_no_runtime_or_secret_paths_committed(self) -> None:
        result = subprocess.run(["git", "ls-files"], cwd=ROOT, text=True, capture_output=True)
        tracked = result.stdout.splitlines()
        forbidden = (".codex/", ".opencode/", "backups/", "auth.json", ".env", "headroom.pid")
        offenders = [path for path in tracked if any(token in path for token in forbidden)]
        self.assertEqual([], offenders)

    def test_machine_local_override_is_gitignored(self) -> None:
        result = subprocess.run(["git", "check-ignore", "-q", "machines/machine.local.yaml"],
                                cwd=ROOT)
        self.assertEqual(0, result.returncode)


if __name__ == "__main__":
    unittest.main()
