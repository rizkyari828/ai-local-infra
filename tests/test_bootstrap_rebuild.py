from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import bootstrap  # noqa: E402


FAKE_GRAPHIFY = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, pathlib, subprocess, sys
    args = sys.argv[1:]
    if args and args[0] == "--version":
        print("graphify 7.7.7")
        sys.exit(0)
    if not args or args[0] != "extract":
        print("unsupported", file=sys.stderr)
        sys.exit(2)
    path = args[1]
    out = None
    for i, a in enumerate(args):
        if a == "--out":
            out = args[i + 1]
    sentinel = os.environ.get("FAKE_SENTINEL")
    if sentinel:
        open(sentinel, "w").write("rebuild-invoked\\n")
    if os.environ.get("FAKE_FAIL"):
        print("boom", file=sys.stderr)
        sys.exit(3)
    if os.environ.get("FAKE_COMMIT"):
        subprocess.run(["git", "-C", path, "commit", "--allow-empty", "-m", "move"], check=True)
    d = pathlib.Path(out) / "graphify-out"
    d.mkdir(parents=True, exist_ok=True)
    nodes = [] if os.environ.get("FAKE_EMPTY") else [{"id": "a"}, {"id": "b"}]
    json.dump({"nodes": nodes, "edges": [{"source": "a", "target": "b"}],
               "extracted_sources": [str(path) + "/file.py"]}, open(d / "graph.json", "w"))
    print("graphify wrote", d / "graph.json")
    """
)


def run_git(path: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class RebuildTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.state = self.home / "state"
        self.repo = self.home / "repo"
        self.repo.mkdir(parents=True)
        run_git(self.repo, "init")
        run_git(self.repo, "config", "user.email", "t@example.com")
        run_git(self.repo, "config", "user.name", "t")
        (self.repo / "file.py").write_text("x = 1\n", encoding="utf-8")
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-m", "init")
        self.head = run_git(self.repo, "rev-parse", "HEAD")
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.fake = self.bin / "graphify"
        self.fake.write_text(FAKE_GRAPHIFY, encoding="utf-8")
        self.fake.chmod(0o755)
        self.sentinel = self.home / "sentinel"
        self.graph_dir = self.home / "graphs" / "graphify-out"
        self.identity = "remote:https://example.com/o/r.git"
        self.state.mkdir(parents=True, exist_ok=True)
        self.write_local(self.graph_dir)
        self.env = os.environ | {
            "HOME": str(self.home),
            "LAI_STATE_DIR": str(self.state),
            "LAI_MACHINE": "work-mac",
        }
        self.manifest = self.graph_dir / bootstrap.DEFAULT_MANIFEST_NAME

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_local(self, graph_dir: pathlib.Path, identity: str | None = None) -> None:
        (self.state / "machine.local.yaml").write_text(json.dumps({
            "graphify": {"graph_dir": str(graph_dir), "binary": str(self.fake)},
            "repository": {"name": "repo", "identity": identity or self.identity,
                           "root": str(self.repo), "head": self.head},
        }), encoding="utf-8")

    def refresh_head(self) -> None:
        self.head = run_git(self.repo, "rev-parse", "HEAD")
        self.write_local(self.graph_dir)

    def cli(self, *args: str, extra_env: dict | None = None) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        env["FAKE_SENTINEL"] = str(self.sentinel)
        if extra_env:
            env.update(extra_env)
        return subprocess.run([str(ROOT / "scripts/bootstrap"), *args], cwd=ROOT,
                              env=env, text=True, capture_output=True)

    def make_previous_graph(self) -> None:
        self.graph_dir.mkdir(parents=True, exist_ok=True)
        (self.graph_dir / "graph.json").write_text(json.dumps({"nodes": [{"id": "old"}], "edges": []}),
                                                   encoding="utf-8")
        bootstrap.write_graph_manifest(self.graph_dir, {
            "name": "repo", "identity": "remote:https://example.com/o/r.git",
            "root": str(self.repo), "head": self.head,
        }, "7.7.7", self.graph_dir / "graph.json")


class CommandTests(RebuildTestBase):
    def test_rebuild_requires_machine(self) -> None:
        env = {k: v for k, v in self.env.items() if k != "LAI_MACHINE"}
        result = subprocess.run([str(ROOT / "scripts/bootstrap"), "graph", "rebuild"], cwd=ROOT,
                                env=env, text=True, capture_output=True)
        self.assertEqual(2, result.returncode)
        self.assertIn("LAI_MACHINE", result.stderr)

    def test_unknown_machine_rejected(self) -> None:
        result = self.cli("graph", "rebuild", "--machine", "nope")
        self.assertEqual(2, result.returncode)

    def test_status_never_rebuilds(self) -> None:
        self.cli("status")
        self.assertFalse(self.sentinel.exists())

    def test_apply_never_rebuilds(self) -> None:
        self.cli("apply", "--dry-run")
        self.assertFalse(self.sentinel.exists())

    def test_graph_status_never_rebuilds(self) -> None:
        self.cli("graph", "status")
        self.assertFalse(self.sentinel.exists())


class RebuildSuccessTests(RebuildTestBase):
    def test_rebuild_creates_graph_and_manifest_then_current(self) -> None:
        result = self.cli("graph", "rebuild")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue((self.graph_dir / "graph.json").is_file())
        self.assertTrue(self.manifest.is_file())
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(self.head, manifest["git_head"])
        self.assertEqual("7.7.7", manifest["graphify_version"])
        self.assertEqual(2, manifest["node_count"])
        self.assertEqual(1, manifest["edge_count"])
        status = self.cli("graph", "status")
        self.assertIn("CURRENT", status.stdout)
        self.assertEqual(0, status.returncode)

    def test_rebuild_preserves_sibling_and_cleans_staging(self) -> None:
        parent = self.graph_dir.parent
        parent.mkdir(parents=True, exist_ok=True)
        (parent / "keep.txt").write_text("keep", encoding="utf-8")
        self.cli("graph", "rebuild")
        self.assertTrue((parent / "keep.txt").is_file())
        leftovers = [p.name for p in parent.iterdir() if p.name.startswith(".lai-")]
        self.assertEqual([], leftovers)

    def test_stale_after_new_commit(self) -> None:
        self.cli("graph", "rebuild")
        evaluate = bootstrap.evaluate_graph_freshness(
            self.graph_dir,
            {"name": "repo", "identity": "remote:https://example.com/o/r.git",
             "root": str(self.repo), "head": "a" * 40},
            "7.7.7", True)
        self.assertEqual("STALE", evaluate["state"])

    def test_version_mismatch_is_stale(self) -> None:
        self.cli("graph", "rebuild")
        evaluate = bootstrap.evaluate_graph_freshness(
            self.graph_dir,
            {"name": "repo", "identity": "remote:https://example.com/o/r.git",
             "root": str(self.repo), "head": self.head},
            "8.8.8", True)
        self.assertEqual("STALE", evaluate["state"])


class RebuildFailureTests(RebuildTestBase):
    def test_failed_process_preserves_previous_graph(self) -> None:
        self.make_previous_graph()
        before_graph = (self.graph_dir / "graph.json").read_bytes()
        before_manifest = self.manifest.read_bytes()
        result = self.cli("graph", "rebuild", extra_env={"FAKE_FAIL": "1"})
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(before_graph, (self.graph_dir / "graph.json").read_bytes())
        self.assertEqual(before_manifest, self.manifest.read_bytes())

    def test_invalid_empty_graph_preserves_previous(self) -> None:
        self.make_previous_graph()
        before = (self.graph_dir / "graph.json").read_bytes()
        result = self.cli("graph", "rebuild", extra_env={"FAKE_EMPTY": "1"})
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(before, (self.graph_dir / "graph.json").read_bytes())

    def test_head_change_during_build_fails_safely(self) -> None:
        self.make_previous_graph()
        before = (self.graph_dir / "graph.json").read_bytes()
        result = self.cli("graph", "rebuild", extra_env={"FAKE_COMMIT": "1"})
        self.assertNotEqual(0, result.returncode)
        self.assertIn("HEAD changed", result.stderr + result.stdout)
        self.assertEqual(before, (self.graph_dir / "graph.json").read_bytes())

    def test_no_manifest_when_graph_never_validated(self) -> None:
        result = self.cli("graph", "rebuild", extra_env={"FAKE_EMPTY": "1"})
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(self.manifest.exists())


class PathSafetyTests(RebuildTestBase):
    def test_in_repo_graph_dir_blocked(self) -> None:
        self.write_local(self.repo / "graphify-out")
        before = sorted(p.name for p in self.repo.iterdir())
        result = self.cli("graph", "rebuild")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(before, sorted(p.name for p in self.repo.iterdir()))

    def test_symlink_into_repo_blocked(self) -> None:
        (self.repo / "graphify-out").mkdir(parents=True, exist_ok=True)
        link = self.home / "graph-link"
        link.symlink_to(self.repo / "graphify-out")
        self.write_local(link)
        result = self.cli("graph", "rebuild")
        self.assertNotEqual(0, result.returncode)


class SecurityTests(RebuildTestBase):
    def test_credentials_sanitized_in_manifest(self) -> None:
        self.write_local(self.graph_dir, identity="remote:https://token@example.com/o/r.git")
        self.cli("graph", "rebuild")
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual("remote:https://example.com/o/r.git", manifest["repository_identity"])
        self.assertNotIn("token@", self.manifest.read_text(encoding="utf-8"))

    def test_no_graph_in_repository(self) -> None:
        self.cli("graph", "rebuild")
        self.assertFalse((self.repo / "graphify-out").exists())
        self.assertFalse((self.repo / bootstrap.DEFAULT_MANIFEST_NAME).exists())


class CleanWorktreeTests(RebuildTestBase):
    def _assert_dirty_blocked(self) -> None:
        result = self.cli("graph", "rebuild")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("dirty", (result.stdout + result.stderr).lower())
        self.assertFalse(self.sentinel.exists())
        self.assertFalse(self.manifest.exists())

    def test_clean_worktree_permits_rebuild_and_is_current(self) -> None:
        result = self.cli("graph", "rebuild")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(self.manifest.exists())
        status = self.cli("graph", "status")
        self.assertIn("CURRENT", status.stdout)

    def test_tracked_modification_blocks(self) -> None:
        (self.repo / "file.py").write_text("x = 2\n", encoding="utf-8")
        self._assert_dirty_blocked()

    def test_staged_modification_blocks(self) -> None:
        (self.repo / "file.py").write_text("x = 2\n", encoding="utf-8")
        run_git(self.repo, "add", "file.py")
        self._assert_dirty_blocked()

    def test_untracked_source_blocks(self) -> None:
        (self.repo / "new_source.py").write_text("y = 1\n", encoding="utf-8")
        self._assert_dirty_blocked()

    def test_deletion_blocks(self) -> None:
        (self.repo / "file.py").unlink()
        self._assert_dirty_blocked()

    def test_ignored_file_does_not_block(self) -> None:
        (self.repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
        run_git(self.repo, "add", ".gitignore")
        run_git(self.repo, "commit", "-m", "add gitignore")
        self.refresh_head()
        (self.repo / "debug.log").write_text("noise\n", encoding="utf-8")
        result = self.cli("graph", "rebuild")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(self.manifest.exists())

    def test_dirty_rejection_preserves_previous_graph_and_manifest(self) -> None:
        self.make_previous_graph()
        before_graph = (self.graph_dir / "graph.json").read_bytes()
        before_manifest = self.manifest.read_bytes()
        (self.repo / "file.py").write_text("x = 2\n", encoding="utf-8")
        result = self.cli("graph", "rebuild")
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(self.sentinel.exists())
        self.assertEqual(before_graph, (self.graph_dir / "graph.json").read_bytes())
        self.assertEqual(before_manifest, self.manifest.read_bytes())


class FreshnessWorktreeTests(RebuildTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.make_previous_graph()

    def status(self) -> str:
        return self.cli("graph", "status").stdout

    def test_clean_matching_manifest_is_current(self) -> None:
        self.assertIn("CURRENT", self.status())

    def test_tracked_modification_is_stale(self) -> None:
        (self.repo / "file.py").write_text("x = 2\n", encoding="utf-8")
        output = self.status()
        self.assertIn("STALE", output)
        self.assertIn("uncommitted", output)

    def test_staged_modification_is_stale(self) -> None:
        (self.repo / "file.py").write_text("x = 2\n", encoding="utf-8")
        run_git(self.repo, "add", "file.py")
        self.assertIn("STALE", self.status())

    def test_untracked_non_ignored_file_is_stale(self) -> None:
        (self.repo / "new_source.py").write_text("y = 1\n", encoding="utf-8")
        self.assertIn("STALE", self.status())

    def test_ignored_file_remains_current(self) -> None:
        (self.repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
        run_git(self.repo, "add", ".gitignore")
        run_git(self.repo, "commit", "-m", "add gitignore")
        self.refresh_head()
        self.make_previous_graph()
        (self.repo / "debug.log").write_text("noise\n", encoding="utf-8")
        self.assertIn("CURRENT", self.status())

    def test_status_is_read_only(self) -> None:
        before_manifest = self.manifest.read_bytes()
        before_worktree = run_git(self.repo, "status", "--porcelain")
        self.status()
        self.assertEqual(before_worktree, run_git(self.repo, "status", "--porcelain"))
        self.assertEqual(before_manifest, self.manifest.read_bytes())

    def test_apply_does_not_rebuild(self) -> None:
        self.cli("apply", "--dry-run")
        self.assertFalse(self.sentinel.exists())


if __name__ == "__main__":
    unittest.main()
