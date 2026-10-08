"""Graphify runtime path alignment: one canonical graph path per machine.

Covers the Home WSL alignment slice:
  * the bootstrap-generated runtime descriptor resolves to
    ``<resolved graphify.graph_dir>/graph.json`` for both machines
  * the Codex managed guidance and the OpenCode plugin never carry the legacy
    graph path (``.../local-ai-infra/graph.json``)
  * no repository source hardcodes a user-specific absolute graph path
  * ``apply`` writes the canonical descriptor and the canonical OpenCode plugin
    without rebuilding the graph or inventing a whole user config
"""

from __future__ import annotations

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


LEGACY_SUFFIX = "graphify/local-ai-infra/graph.json"
CANONICAL_SUFFIX = "graphify/local-ai-infra/graphify-out/graph.json"
PLUGIN_TEMPLATE = ROOT / "templates/opencode/plugins/graphify.ts"


class RuntimeDescriptorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.state = self.home / "state"
        self.env = os.environ | {"HOME": str(self.home), "LAI_STATE_DIR": str(self.state)}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def plan(self, machine: str) -> dict[str, dict]:
        env = self.env | {"LAI_MACHINE": machine}
        with unittest.mock.patch.dict(os.environ, env):
            units, _ = bootstrap.build_plan(env)
        return {unit["id"]: unit for unit in units}

    def test_home_wsl_descriptor_is_canonical(self) -> None:
        unit = self.plan("home-wsl")["graphify.runtime"]
        self.assertEqual("generated-json", unit["mode"])
        desired = unit["desired"]
        self.assertNotIn("{{", json.dumps(desired))
        self.assertTrue(desired["graph_path"].endswith(CANONICAL_SUFFIX))
        self.assertNotIn(LEGACY_SUFFIX, desired["graph_path"])
        self.assertEqual(desired["graph_dir"] + "/graph.json", desired["graph_path"])
        self.assertEqual(desired["graph_dir"] + "/lai-freshness.json", desired["manifest_path"])

    def test_work_mac_descriptor_is_canonical(self) -> None:
        desired = self.plan("work-mac")["graphify.runtime"]["desired"]
        self.assertTrue(desired["graph_path"].endswith(CANONICAL_SUFFIX))
        self.assertNotIn(LEGACY_SUFFIX, desired["graph_path"])
        self.assertNotIn("{{", json.dumps(desired))

    def test_descriptor_matches_freshness_target(self) -> None:
        units = self.plan("home-wsl")
        self.assertEqual(units["graphify.runtime"]["desired"]["graph_path"],
                         units["graphify.freshness"]["target"])

    def test_descriptor_path_matches_resolve_graph_dir(self) -> None:
        env = self.env | {"LAI_MACHINE": "home-wsl"}
        with unittest.mock.patch.dict(os.environ, env):
            ctx, _ = bootstrap.load_effective(env)
            expected = str(bootstrap.resolve_graph_dir(ctx) / "graph.json")
        self.assertEqual(expected, self.plan("home-wsl")["graphify.runtime"]["desired"]["graph_path"])


class ManagedGuidanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.env = os.environ | {"HOME": str(self.home),
                                 "LAI_STATE_DIR": str(self.home / "state")}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def plan(self, machine: str) -> dict[str, dict]:
        env = self.env | {"LAI_MACHINE": machine}
        with unittest.mock.patch.dict(os.environ, env):
            units, _ = bootstrap.build_plan(env)
        return {unit["id"]: unit for unit in units}

    def test_codex_guidance_uses_canonical_graph(self) -> None:
        for machine in ("home-wsl", "work-mac"):
            desired = self.plan(machine)["codex.guidance"]["desired"]
            self.assertIn(CANONICAL_SUFFIX, desired.replace("`", ""))
            self.assertNotIn(LEGACY_SUFFIX, desired)
            self.assertNotIn("{{", desired)

    def test_opencode_plugin_is_a_template_rendered_file(self) -> None:
        unit = self.plan("home-wsl")["opencode.graphify"]
        self.assertEqual("managed-file", unit["mode"])
        self.assertEqual(str(self.home / ".config/opencode/plugins/graphify.ts"), unit["target"])
        self.assertEqual(PLUGIN_TEMPLATE.read_text(encoding="utf-8"), unit["desired"])

    def test_opencode_plugin_never_hardcodes_a_graph_path(self) -> None:
        text = PLUGIN_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("graphify/runtime.json", text)   # resolves via the managed descriptor
        self.assertNotIn("graph.json", text)           # no hardcoded graph file
        self.assertNotIn(LEGACY_SUFFIX, text)
        self.assertNotIn("/home/", text)               # no user-specific absolute path

    def test_repo_sources_do_not_reference_legacy_graph(self) -> None:
        offenders: list[str] = []
        for area in ("config", "machines", "templates"):
            for path in (ROOT / area).rglob("*"):
                if path.is_file() and LEGACY_SUFFIX in path.read_text(encoding="utf-8", errors="ignore"):
                    offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual([], offenders)

    def test_graph_status_and_rebuild_semantics_unchanged(self) -> None:
        # The freshness unit still resolves the same graphify.graph_dir and stays read-only.
        plan = self.plan("home-wsl")
        freshness = plan["graphify.freshness"]
        self.assertFalse(freshness["changes"])
        self.assertEqual(plan["graphify.runtime"]["desired"]["graph_dir"], freshness["graph_dir"])
        self.assertEqual(plan["graphify.runtime"]["desired"]["graph_path"], freshness["target"])


class RuntimeApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.state = self.home / "state"
        self.codex = self.home / ".codex"
        self.codex.mkdir(parents=True)
        self.agents = self.codex / "AGENTS.md"
        self.config = self.codex / "config.toml"
        self.plugin = self.home / ".config/opencode/plugins/graphify.ts"
        self.env = os.environ | {"HOME": str(self.home), "LAI_STATE_DIR": str(self.state),
                                 "LAI_MACHINE": "home-wsl"}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, path: pathlib.Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(ROOT / "scripts/bootstrap"), *args], cwd=ROOT,
                              env=self.env, text=True, capture_output=True)

    def test_apply_writes_canonical_descriptor_and_plugin(self) -> None:
        self.write(self.agents, "# personal\n")
        self.write(self.config, '[projects."/p"]\ntrust_level = "trusted"\n')
        self.write(self.plugin, 'const GRAPH = "/home/tama/.local/share/local-ai-infra/'
                                'graphify/local-ai-infra/graph.json"\n')
        result = self.cli("apply")
        self.assertEqual(0, result.returncode, result.stderr)

        descriptor = json.loads((self.state / "graphify/runtime.json").read_text(encoding="utf-8"))
        self.assertTrue(descriptor["graph_path"].endswith(CANONICAL_SUFFIX))

        installed = self.plugin.read_text(encoding="utf-8")
        self.assertIn("graphify/runtime.json", installed)
        self.assertNotIn(LEGACY_SUFFIX, installed)

        agents = self.agents.read_text(encoding="utf-8")
        self.assertIn(CANONICAL_SUFFIX, agents)
        self.assertNotIn("{{", agents)
        self.assertIn("# personal", agents)

        # Rebuild is never implicit.
        graph_dir = self.home / ".local/share/local-ai-infra/graphify/local-ai-infra/graphify-out"
        self.assertFalse(graph_dir.exists())

    def test_apply_is_idempotent_and_aligned(self) -> None:
        self.write(self.agents, "# personal\n")
        self.write(self.config, '[projects."/p"]\ntrust_level = "trusted"\n')
        self.assertEqual(0, self.cli("apply").returncode)
        second = self.cli("apply")
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertIn("No action taken", second.stdout)

    def test_dry_run_mutates_nothing(self) -> None:
        self.write(self.agents, "# personal\n")
        self.write(self.config, '[projects."/p"]\ntrust_level = "trusted"\n')
        before = self.agents.read_bytes()
        result = self.cli("apply", "--dry-run")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("DRY RUN", result.stdout)
        self.assertEqual(before, self.agents.read_bytes())
        self.assertFalse(self.plugin.exists())
        self.assertFalse((self.state / "graphify").exists())


if __name__ == "__main__":
    unittest.main()
