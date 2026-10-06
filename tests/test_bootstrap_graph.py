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
import secret_guard  # noqa: E402


class GraphTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.state = self.home / "state"
        self.repo_root = self.home / "repo"
        self.repo_root.mkdir(parents=True, exist_ok=True)
        self.env = os.environ | {
            "HOME": str(self.home),
            "LAI_STATE_DIR": str(self.state),
            "LAI_MACHINE": "work-mac",
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def repository(self, head: str = "a" * 40, identity: str = "remote:https://example.com/o/r.git",
                   root: pathlib.Path | None = None) -> dict:
        return {"name": "r", "identity": identity,
                "root": str(root or self.repo_root), "head": head}

    def make_graph(self, graph_dir: pathlib.Path, head: str, version: str,
                   identity: str = "remote:https://example.com/o/r.git",
                   manifest_name: str = bootstrap.DEFAULT_MANIFEST_NAME) -> pathlib.Path:
        graph_dir.mkdir(parents=True, exist_ok=True)
        graph = graph_dir / "graph.json"
        graph.write_text(json.dumps({"built_at_commit": head, "nodes": [], "links": []}),
                         encoding="utf-8")
        repo = self.repository(head=head, identity=identity)
        bootstrap.write_graph_manifest(graph_dir, repo, version, graph, manifest_name=manifest_name)
        return graph

    def graph_dir(self) -> pathlib.Path:
        return self.home / "graphs" / "graphify-out"


class FreshnessTests(GraphTestBase):
    def test_current(self) -> None:
        d = self.graph_dir()
        self.make_graph(d, "a" * 40, "1.2.3")
        result = bootstrap.evaluate_graph_freshness(d, self.repository(), "1.2.3", True)
        self.assertEqual("CURRENT", result["state"])

    def test_head_mismatch_is_stale(self) -> None:
        d = self.graph_dir()
        self.make_graph(d, "a" * 40, "1.2.3")
        result = bootstrap.evaluate_graph_freshness(d, self.repository(head="b" * 40), "1.2.3", True)
        self.assertEqual("STALE", result["state"])
        self.assertIn("HEAD", result["reason"])

    def test_version_mismatch_is_stale(self) -> None:
        d = self.graph_dir()
        self.make_graph(d, "a" * 40, "1.2.3")
        result = bootstrap.evaluate_graph_freshness(d, self.repository(), "9.9.9", True)
        self.assertEqual("STALE", result["state"])
        self.assertIn("version", result["reason"])

    def test_binary_unavailable_is_stale_not_current(self) -> None:
        d = self.graph_dir()
        self.make_graph(d, "a" * 40, "1.2.3")
        result = bootstrap.evaluate_graph_freshness(d, self.repository(), None, False)
        self.assertEqual("STALE", result["state"])

    def test_graph_missing(self) -> None:
        d = self.graph_dir()
        d.mkdir(parents=True, exist_ok=True)
        result = bootstrap.evaluate_graph_freshness(d, self.repository(), "1.2.3", True)
        self.assertEqual("MISSING", result["state"])

    def test_manifest_missing_is_missing_not_current(self) -> None:
        d = self.graph_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "graph.json").write_text(json.dumps({"built_at_commit": "a" * 40}), encoding="utf-8")
        result = bootstrap.evaluate_graph_freshness(d, self.repository(), "1.2.3", True)
        self.assertEqual("MISSING", result["state"])
        self.assertNotEqual("CURRENT", result["state"])
        self.assertIn("manifest", result["reason"])

    def test_malformed_manifest_is_blocked(self) -> None:
        d = self.graph_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "graph.json").write_text("{}", encoding="utf-8")
        (d / bootstrap.DEFAULT_MANIFEST_NAME).write_text("{not json", encoding="utf-8")
        result = bootstrap.evaluate_graph_freshness(d, self.repository(), "1.2.3", True)
        self.assertEqual("BLOCKED", result["state"])

    def test_manifest_missing_field_is_blocked(self) -> None:
        d = self.graph_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "graph.json").write_text("{}", encoding="utf-8")
        (d / bootstrap.DEFAULT_MANIFEST_NAME).write_text(
            json.dumps({"schema_version": 1}), encoding="utf-8")
        result = bootstrap.evaluate_graph_freshness(d, self.repository(), "1.2.3", True)
        self.assertEqual("BLOCKED", result["state"])

    def test_identity_mismatch_is_blocked(self) -> None:
        d = self.graph_dir()
        self.make_graph(d, "a" * 40, "1.2.3", identity="remote:https://example.com/other/repo.git")
        result = bootstrap.evaluate_graph_freshness(d, self.repository(), "1.2.3", True)
        self.assertEqual("BLOCKED", result["state"])
        self.assertIn("identity", result["reason"])


class GraphPathSafetyTests(GraphTestBase):
    def test_graph_dir_inside_repo_is_blocked(self) -> None:
        inside = self.repo_root / "graphify-out"
        inside.mkdir(parents=True, exist_ok=True)
        (inside / "graph.json").write_text("{}", encoding="utf-8")
        result = bootstrap.evaluate_graph_freshness(inside, self.repository(), "1.2.3", True)
        self.assertEqual("BLOCKED", result["state"])
        self.assertIn("inside repository", result["reason"])

    def test_symlink_into_repo_is_blocked(self) -> None:
        (self.repo_root / "graphify-out").mkdir(parents=True, exist_ok=True)
        link = self.home / "graph-link"
        link.symlink_to(self.repo_root / "graphify-out")
        result = bootstrap.evaluate_graph_freshness(link, self.repository(), "1.2.3", True)
        self.assertEqual("BLOCKED", result["state"])

    def test_write_manifest_refuses_inside_repo(self) -> None:
        inside = self.repo_root / "graphify-out"
        inside.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.write_graph_manifest(inside, self.repository(), "1.2.3", inside / "graph.json")


class IdentityTests(GraphTestBase):
    def test_remote_credentials_are_sanitized(self) -> None:
        self.assertEqual("https://github.com/o/r.git",
                         bootstrap.sanitize_remote("https://token@github.com/o/r.git"))
        self.assertEqual("https://github.com/o/r.git",
                         bootstrap.sanitize_remote("https://user:pass@github.com/o/r.git"))
        self.assertEqual("git@github.com:o/r.git",
                         bootstrap.sanitize_remote("git@github.com:o/r.git"))

    def test_identity_does_not_depend_on_absolute_path(self) -> None:
        d = self.graph_dir()
        self.make_graph(d, "a" * 40, "1.2.3")
        other_root = self.home / "elsewhere"
        other_root.mkdir()
        result = bootstrap.evaluate_graph_freshness(
            d, self.repository(root=other_root), "1.2.3", True)
        self.assertEqual("CURRENT", result["state"])

    def test_manifest_contains_no_secret(self) -> None:
        d = self.graph_dir()
        d.mkdir(parents=True, exist_ok=True)
        graph = d / "graph.json"
        graph.write_text("{}", encoding="utf-8")
        repo = self.repository(identity="remote:https://token@example.com/o/r.git")
        repo["identity"] = "remote:" + bootstrap.sanitize_remote("https://token@example.com/o/r.git")
        manifest = bootstrap.write_graph_manifest(d, repo, "1.2.3", graph)
        self.assertEqual([], secret_guard.scan_values(manifest, "manifest"))


class OwnershipReportingTests(GraphTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.codex = self.home / ".codex"
        self.codex.mkdir(parents=True, exist_ok=True)
        (self.codex / "AGENTS.md").write_text("# personal\n", encoding="utf-8")

    def _plan(self):
        with unittest.mock.patch.object(bootstrap, "detect_graphify_version",
                                        return_value=("1.2.3", True)):
            with unittest.mock.patch.dict(os.environ, self.env):
                plan, _ = bootstrap.build_plan(self.env)
        return {unit["id"]: unit for unit in plan}

    def test_deferred_is_not_managed_not_blocked(self) -> None:
        units = self._plan()
        for unit_id in ("rtk.guidance", "opencode.config"):
            self.assertEqual(bootstrap.OWNERSHIP_NOT_MANAGED, units[unit_id]["ownership"])
            self.assertIsNone(units[unit_id]["state"])
            self.assertNotEqual("BLOCKED", units[unit_id]["state"])

    def test_graph_unit_reports_freshness_separately(self) -> None:
        units = self._plan()
        graph = units["graphify.freshness"]
        self.assertEqual(bootstrap.OWNERSHIP_MANAGED, graph["ownership"])
        self.assertEqual(bootstrap.MUTABILITY_NONE, graph["mutability"])
        self.assertIn(graph["freshness"], {"CURRENT", "STALE", "MISSING", "BLOCKED"})

    def test_true_blocked_remains_blocked(self) -> None:
        (self.codex / "AGENTS.md").write_text(
            "<!-- >>> local-ai-infra managed: codex-guidance >>> -->\n"
            "<!-- >>> local-ai-infra managed: codex-guidance >>> -->\n"
            "<!-- <<< local-ai-infra managed: codex-guidance <<< -->\n",
            encoding="utf-8")
        units = self._plan()
        self.assertEqual("BLOCKED", units["codex.guidance"]["state"])


class GraphApplySafetyTests(GraphTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.codex = self.home / ".codex"
        self.codex.mkdir(parents=True, exist_ok=True)
        self.agents = self.codex / "AGENTS.md"
        self.agents.write_text("# personal\n", encoding="utf-8")
        self.graph = self.graph_dir()
        (self.state).mkdir(parents=True, exist_ok=True)
        (self.state / "machine.local.yaml").write_text(json.dumps({
            "graphify": {"graph_dir": str(self.graph)},
            "repository": self.repository(head="a" * 40),
        }), encoding="utf-8")

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        return subprocess.run([str(ROOT / "scripts/bootstrap"), *args], cwd=ROOT,
                              env=env, text=True, capture_output=True)

    def test_status_never_rebuilds_graph(self) -> None:
        self.make_graph(self.graph, "a" * 40, "1.2.3")
        before = (self.graph / "graph.json").read_bytes()
        result = self._run("status")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(before, (self.graph / "graph.json").read_bytes())

    def test_stale_graph_does_not_block_safe_apply(self) -> None:
        self.make_graph(self.graph, "old" + "0" * 37, "1.2.3")
        result = self._run("apply")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("local-ai-infra managed: codex-guidance",
                      self.agents.read_text(encoding="utf-8"))
        manifest = json.loads((self.graph / bootstrap.DEFAULT_MANIFEST_NAME).read_text())
        self.assertTrue(manifest["git_head"].startswith("old"))

    def test_dry_run_does_not_rebuild_graph(self) -> None:
        self.make_graph(self.graph, "a" * 40, "1.2.3")
        before = sorted(p.name for p in self.graph.iterdir())
        result = self._run("apply", "--dry-run")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(before, sorted(p.name for p in self.graph.iterdir()))

    def test_no_repo_local_graph_manifest(self) -> None:
        self.make_graph(self.graph, "a" * 40, "1.2.3")
        self.assertFalse((ROOT / bootstrap.DEFAULT_MANIFEST_NAME).exists())


if __name__ == "__main__":
    unittest.main()
