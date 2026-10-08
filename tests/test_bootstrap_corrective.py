"""Corrective slice tests: recursive resolution, owned TOML table/key creation.

Covers the three bounded Home WSL blockers:
  A. nested placeholder resolution (bounded, cycle-safe, unresolved-safe)
  B. creating only the owned ``[mcp_servers.headroom]`` table in an existing file
  C. owning only the top-level Codex ``model`` key for a machine that declares it
"""

from __future__ import annotations

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


# --------------------------------------------------------------------------- #
# Fix A — recursive / bounded template resolution
# --------------------------------------------------------------------------- #


class ResolverTests(unittest.TestCase):
    def test_one_level_substitution(self) -> None:
        self.assertEqual("b", bootstrap.resolve("{{a}}", {"a": "b"}))
        self.assertEqual("xby", bootstrap.resolve("x{{a}}y", {"a": "b"}))

    def test_multi_level_substitution(self) -> None:
        self.assertEqual("c", bootstrap.resolve("{{a}}", {"a": "{{b}}", "b": "c"}))

    def test_three_level_substitution(self) -> None:
        ctx = {"a": "{{b}}", "b": "{{c}}", "c": "deep"}
        self.assertEqual("deep", bootstrap.resolve("{{a}}", ctx))

    def test_nested_embedded_value(self) -> None:
        ctx = {"graph_root": "/srv", "repository": {"name": "repo"},
               "graphify": {"graph_dir": "{{graph_root}}/{{repository.name}}/graphify-out"}}
        self.assertEqual("/srv/repo/graphify-out", bootstrap.resolve("{{graphify.graph_dir}}", ctx))

    def test_whole_value_preserves_type(self) -> None:
        self.assertEqual(8787, bootstrap.resolve("{{port}}", {"port": 8787}))
        self.assertEqual(["mcp", "serve"], bootstrap.resolve("{{flags}}", {"flags": ["mcp", "serve"]}))

    def test_unresolved_placeholder_raises(self) -> None:
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.resolve("{{nope}}", {"a": "b"})
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.resolve("x{{nope}}y", {"a": "b"})

    def test_direct_cycle_raises(self) -> None:
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.resolve("{{a}}", {"a": "{{a}}"})

    def test_indirect_cycle_raises(self) -> None:
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.resolve("{{a}}", {"a": "{{b}}", "b": "{{a}}"})

    def test_embedded_cycle_raises_not_hangs(self) -> None:
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.resolve("{{a}}", {"a": "x{{b}}", "b": "y{{a}}"})

    def test_max_depth_protection(self) -> None:
        depth = bootstrap.MAX_RESOLVE_DEPTH + 5
        ctx = {f"v{i}": "{{v%d}}" % (i + 1) for i in range(depth)}
        ctx[f"v{depth}"] = "end"
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.resolve("{{v0}}", ctx)


class GraphResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.state = self.home / "state"
        self.env = os.environ | {
            "HOME": str(self.home),
            "LAI_STATE_DIR": str(self.state),
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _graph_dir(self, machine: str) -> tuple[pathlib.Path, dict]:
        env = self.env | {"LAI_MACHINE": machine}
        with unittest.mock.patch.dict(os.environ, env):
            ctx, _ = bootstrap.load_effective(env)
            return bootstrap.resolve_graph_dir(ctx), ctx

    def test_home_wsl_graph_dir_resolves_externally(self) -> None:
        graph_dir, ctx = self._graph_dir("home-wsl")
        expected = (self.home / ".local/share/local-ai-infra/graphify"
                    / "local-ai-infra/graphify-out")
        self.assertEqual(str(expected), str(graph_dir))
        self.assertNotIn("{{", str(graph_dir))
        self.assertIsNone(bootstrap.graph_path_safety(graph_dir, pathlib.Path(ctx["repository"]["root"])))
        self.assertFalse(str(graph_dir).startswith(str(ROOT) + os.sep))

    def test_work_mac_graph_dir_resolution_unchanged(self) -> None:
        graph_dir, _ = self._graph_dir("work-mac")
        expected = (self.home / ".local/share/local-ai-infra/graphify"
                    / "local-ai-infra/graphify-out")
        self.assertEqual(str(expected), str(graph_dir))


# --------------------------------------------------------------------------- #
# Setup shared by CLI-driven table/key tests
# --------------------------------------------------------------------------- #


class CorrectiveCliBase(unittest.TestCase):
    machine = "home-wsl"

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
            "LAI_MACHINE": self.machine,
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, path: pathlib.Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def cli(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(ROOT / "scripts/bootstrap"), *args], cwd=ROOT,
                              env=env or self.env, text=True, capture_output=True)

    def plan(self, machine: str | None = None) -> dict[str, dict]:
        env = self.env | ({"LAI_MACHINE": machine} if machine else {})
        with unittest.mock.patch.dict(os.environ, env):
            units, _ = bootstrap.build_plan(env)
        return {unit["id"]: unit for unit in units}


# --------------------------------------------------------------------------- #
# Fix B — owned Headroom MCP table may be created, never a whole file
# --------------------------------------------------------------------------- #


class HeadroomMcpTests(CorrectiveCliBase):
    def base_config(self) -> str:
        return ('[projects."/tmp/demo"]\n'
                'trust_level = "trusted"\n\n'
                '[mcp_servers.other]\n'
                'command = "/usr/bin/other"\n')

    def test_absent_headroom_table_is_created(self) -> None:
        self.write(self.config, self.base_config())
        original = self.config.read_text(encoding="utf-8")
        result = self.cli("apply")
        self.assertEqual(0, result.returncode, result.stderr)
        after = self.config.read_text(encoding="utf-8")
        self.assertIn(original, after)
        self.assertIn("[mcp_servers.headroom]", after)
        self.assertIn(str(self.home / ".local/bin/headroom"), after)

    def test_existing_matching_table_is_aligned(self) -> None:
        self.write(self.config, self.base_config())
        self.cli("apply")
        self.assertEqual("ALIGNED", self.plan()["headroom.mcp"]["state"])

    def test_drifted_table_updated_only_within_owned_table(self) -> None:
        self.write(self.config,
                   '[projects."/p"]\n'
                   'trust_level = "trusted"\n\n'
                   '[mcp_servers.headroom]\n'
                   'command = "/old/bin/headroom"\n'
                   'args = ["mcp", "serve", "--proxy-url", "http://127.0.0.1:9999"]\n\n'
                   '[mcp_servers.other]\n'
                   'command = "/usr/bin/other"\n')
        result = self.cli("apply")
        self.assertEqual(0, result.returncode, result.stderr)
        after = self.config.read_text(encoding="utf-8")
        self.assertNotIn("/old/bin/headroom", after)
        self.assertIn(str(self.home / ".local/bin/headroom"), after)
        self.assertIn("[mcp_servers.other]", after)
        self.assertIn('command = "/usr/bin/other"', after)
        self.assertIn('[projects."/p"]', after)
        self.assertIn('trust_level = "trusted"', after)

    def test_unrelated_mcp_and_trust_preserved_on_create(self) -> None:
        self.write(self.config, self.base_config())
        self.cli("apply")
        after = self.config.read_text(encoding="utf-8")
        self.assertIn('[projects."/tmp/demo"]', after)
        self.assertIn('trust_level = "trusted"', after)
        self.assertIn("[mcp_servers.other]", after)

    def test_duplicate_headroom_table_is_blocked(self) -> None:
        self.write(self.config,
                   '[mcp_servers.headroom]\ncommand = "/a"\n\n'
                   '[mcp_servers.headroom]\ncommand = "/b"\n')
        before = self.config.read_bytes()
        status = self.cli("status")
        self.assertEqual(1, status.returncode)
        self.assertIn("BLOCKED", status.stdout)
        apply = self.cli("apply")
        self.assertEqual(1, apply.returncode)
        self.assertEqual(before, self.config.read_bytes())

    def test_dry_run_does_not_mutate_or_backup(self) -> None:
        self.write(self.config, self.base_config())
        before = self.config.read_bytes()
        result = self.cli("apply", "--dry-run")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("DRY RUN", result.stdout)
        self.assertEqual(before, self.config.read_bytes())
        self.assertFalse((self.state / "backups").exists())

    def test_second_apply_is_noop(self) -> None:
        self.write(self.config, self.base_config())
        self.cli("apply")
        after_first = self.config.read_bytes()
        second = self.cli("apply")
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertIn("No action taken", second.stdout)
        self.assertEqual(after_first, self.config.read_bytes())

    def test_absent_config_is_missing_not_invented(self) -> None:
        self.assertFalse(self.config.exists())
        result = self.cli("apply")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(self.config.exists())
        status = self.cli("status")
        self.assertIn("MISSING", status.stdout)
        self.assertNotIn("BLOCKED", status.stdout)


# --------------------------------------------------------------------------- #
# Fix C — owned top-level Codex model key, machine-scoped
# --------------------------------------------------------------------------- #


class CodexModelTests(CorrectiveCliBase):
    unrelated = ('model_reasoning_effort = "high"\n'
                 'personality = "p"\n\n'
                 '[projects."/p"]\n'
                 'trust_level = "trusted"\n\n'
                 '[mcp_servers.other]\n'
                 'command = "/usr/bin/other"\n')

    def test_missing_model_key_added_before_first_table(self) -> None:
        self.write(self.config, self.unrelated)
        result = self.cli("apply")
        self.assertEqual(0, result.returncode, result.stderr)
        after = self.config.read_text(encoding="utf-8")
        self.assertIn('model = "gpt-5.6-sol"', after)
        self.assertIn('model_reasoning_effort = "high"', after)
        self.assertIn('[projects."/p"]', after)
        self.assertIn("[mcp_servers.other]", after)
        lines = after.splitlines()
        model_line = next(i for i, line in enumerate(lines) if line.startswith("model ="))
        first_table = next(i for i, line in enumerate(lines) if line.lstrip().startswith("["))
        self.assertLess(model_line, first_table)

    def test_drifted_model_updated_preserving_unrelated(self) -> None:
        self.write(self.config,
                   'model = "gpt-5.4"\n'
                   'model_reasoning_effort = "high"\n\n'
                   '[projects."/p"]\n'
                   'trust_level = "trusted"\n\n'
                   '[mcp_servers.other]\n'
                   'command = "/usr/bin/other"\n')
        result = self.cli("apply")
        self.assertEqual(0, result.returncode, result.stderr)
        after = self.config.read_text(encoding="utf-8")
        self.assertIn('model = "gpt-5.6-sol"', after)
        self.assertNotIn("gpt-5.4", after)
        self.assertIn('model_reasoning_effort = "high"', after)
        self.assertIn("[mcp_servers.other]", after)
        self.assertIn('trust_level = "trusted"', after)

    def test_matching_model_is_aligned(self) -> None:
        self.write(self.config, 'model = "gpt-5.6-sol"\n')
        self.assertEqual("ALIGNED", self.plan()["codex.model"]["state"])

    def test_table_scoped_model_is_not_confused(self) -> None:
        self.write(self.config, '[tui]\nmodel = "other-model"\n')
        result = self.cli("apply")
        self.assertEqual(0, result.returncode, result.stderr)
        after = self.config.read_text(encoding="utf-8")
        self.assertIn('[tui]\nmodel = "other-model"', after)
        self.assertIn('model = "gpt-5.6-sol"', after)

    def test_top_level_model_table_is_blocked(self) -> None:
        self.write(self.config, '[model]\nprovider = "x"\n')
        before = self.config.read_bytes()
        status = self.cli("status")
        self.assertEqual(1, status.returncode)
        self.assertIn("BLOCKED", status.stdout)
        self.cli("apply")
        self.assertEqual(before, self.config.read_bytes())

    def test_malformed_toml_is_blocked(self) -> None:
        self.write(self.config, 'model = "unterminated\n')
        before = self.config.read_bytes()
        status = self.cli("status")
        self.assertEqual(1, status.returncode)
        self.assertIn("BLOCKED", status.stdout)
        apply = self.cli("apply")
        self.assertEqual(1, apply.returncode)
        self.assertEqual(before, self.config.read_bytes())

    def test_dry_run_zero_mutation(self) -> None:
        self.write(self.config, 'model = "gpt-5.4"\n')
        before = self.config.read_bytes()
        result = self.cli("apply", "--dry-run")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(before, self.config.read_bytes())
        self.assertFalse((self.state / "backups").exists())

    def test_apply_idempotent(self) -> None:
        self.write(self.config, 'model = "gpt-5.4"\n')
        self.cli("apply")
        after_first = self.config.read_bytes()
        second = self.cli("apply")
        self.assertIn("No action taken", second.stdout)
        self.assertEqual(after_first, self.config.read_bytes())


# --------------------------------------------------------------------------- #
# Regression guards
# --------------------------------------------------------------------------- #


class WorkMacRegressionTests(CorrectiveCliBase):
    machine = "work-mac"

    def test_model_unit_is_not_managed_on_work_mac(self) -> None:
        units = self.plan()
        self.assertEqual(bootstrap.OWNERSHIP_NOT_MANAGED, units["codex.model"]["ownership"])

    def test_work_mac_config_model_left_untouched(self) -> None:
        self.write(self.config, 'model = "mac-model"\n\n[projects."/p"]\ntrust_level = "trusted"\n')
        self.cli("apply")
        after = self.config.read_text(encoding="utf-8")
        self.assertIn('model = "mac-model"', after)
        self.assertIn('trust_level = "trusted"', after)
        self.assertIn("[mcp_servers.headroom]", after)

    def test_opencode_remains_not_managed(self) -> None:
        self.assertEqual(bootstrap.OWNERSHIP_NOT_MANAGED, self.plan()["opencode.config"]["ownership"])

    def test_work_mac_managed_units_align_after_apply(self) -> None:
        self.write(self.agents, "# personal\n")
        self.write(self.config, '[projects."/p"]\ntrust_level = "trusted"\n')
        self.cli("apply")
        units = self.plan()
        self.assertEqual("ALIGNED", units["codex.guidance"]["state"])
        self.assertEqual("ALIGNED", units["headroom.mcp"]["state"])
        self.assertEqual("ALIGNED", units["headroom.defaults"]["state"])
        self.assertNotEqual("BLOCKED", units["graphify.freshness"]["state"])


class NoSideEffectRegressionTests(CorrectiveCliBase):
    def graph_dir(self) -> pathlib.Path:
        return (self.home / ".local/share/local-ai-infra/graphify"
                / "local-ai-infra/graphify-out")

    def test_apply_never_rebuilds_graph(self) -> None:
        self.cli("apply")
        self.assertFalse(self.graph_dir().exists())

    def test_apply_never_starts_headroom_process(self) -> None:
        self.cli("apply")
        self.assertFalse((self.state / "headroom.pid").exists())

    def test_home_wsl_graph_is_missing_not_blocked(self) -> None:
        status = self.cli("status")
        self.assertNotIn("BLOCKED", status.stdout)
        self.assertIn("MISSING", status.stdout)


if __name__ == "__main__":
    unittest.main()
