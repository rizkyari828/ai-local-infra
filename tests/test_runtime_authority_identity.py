"""Master A Fix Pass 1.2 — nested workspace repository identity.

Astra reproduced a HIGH identity/cache-isolation defect: Git reports a *relative*
``--git-common-dir`` relative to the directory the command ran in, and it was being
resolved against the repository **toplevel**. From ``A/project/pkg/src`` and
``B/project/pkg/src`` both resolved to the same wrong path (``<parent>/.git``), so two
unrelated remote-less repositories shared one identity and one cache namespace — and
the same repository received *different* identities depending on invocation depth.

These tests pin the corrected contract: identity comes from the canonical Git
common directory resolved in the Git invocation context, so it is independent of
invocation depth and collision-free across unrelated repositories.

Stub workers, temporary Git repositories and temporary cache/state only: no real
provider or model call is made.
"""

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
import authority  # noqa: E402
import execution  # noqa: E402
import failover  # noqa: E402
import orchestration_store  # noqa: E402
import result_cache  # noqa: E402


NESTED = ("pkg", "src")
FIXED_DATE = "2026-01-01T00:00:00Z"


def _git(root: pathlib.Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                            text=True)
    return result.stdout.strip()


def _repo(path: pathlib.Path, remote: str | None = None) -> pathlib.Path:
    """A remote-less repository with a nested ``pkg/src`` directory.

    Content and commit metadata are byte-identical for every repository this helper
    creates, so two of them share the same tree, branch and commit hash: identity must
    still differ.
    """
    nested = path.joinpath(*NESTED)
    nested.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    if remote:
        _git(path, "remote", "add", "origin", remote)
    (path / "f.txt").write_text("identical content\n", encoding="utf-8")
    (nested / "g.txt").write_text("identical nested content\n", encoding="utf-8")
    env = os.environ | {"GIT_AUTHOR_DATE": FIXED_DATE, "GIT_COMMITTER_DATE": FIXED_DATE,
                        "GIT_AUTHOR_NAME": "t", "GIT_COMMITTER_NAME": "t",
                        "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_EMAIL": "t@example.com"}
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "identical commit"], check=True, env=env)
    return path


class Stub(execution.Backend):
    """A claimed-real worker that counts launches and never touches a provider."""

    name = "stub"
    mode = "SUPPORTED"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def available(self) -> bool:
        return True

    def execute(self, request: dict) -> dict:
        self.calls.append(request)
        return {"task_id": request["task_id"], "status": "SUCCESS",
                "completion_signal": "agent_reported", "acceptance_results": {"ok": True},
                "files_changed": [], "decisions": [], "new_risk_signals": []}


def _plan() -> dict:
    """One AI node on a permitted tier, independent of any machine policy."""
    return {"schema_version": 1, "valid": True, "intent": "READ_ONLY", "order": ["A1"],
            "nodes": [{"task_id": "A1", "title": "review", "objective_summary": "review",
                       "task_family": "generic", "task_class": "CORRECTNESS_SENSITIVE",
                       "depends_on": [], "conditional": False, "intent": "READ_ONLY",
                       "executor": "AI", "routing": {"tier": "STRONG", "effort": "high",
                                                     "role": "teacher-cheap"},
                       "context": {"budget": "SMALL"}, "acceptance_criteria": ["ok"],
                       "risk_signals": [], "knowledge_refs": [], "context_refs": []}]}


class NestedRepositoryIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.a = _repo(self.base / "A" / "project")
        self.b = _repo(self.base / "B" / "project")
        self.nested_a = self.a.joinpath(*NESTED)
        self.nested_b = self.b.joinpath(*NESTED)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ---- the exact missing regression ------------------------------------- #

    def test_critical_nested_unrelated_same_basename_repos_are_distinct(self) -> None:
        # Identical basename, tree, branch and commit hash; different repositories.
        self.assertEqual("project", self.a.name)
        self.assertEqual(_git(self.a, "rev-parse", "HEAD"), _git(self.b, "rev-parse", "HEAD"))
        nested_a = authority.repository_identity(self.nested_a)
        nested_b = authority.repository_identity(self.nested_b)
        self.assertNotEqual(nested_a["id"], nested_b["id"],
                            "unrelated repositories resolved to one identity from a nested dir")
        self.assertEqual("git_common_dir", nested_a["kind"])
        self.assertFalse(nested_a["portable"])

    def test_identity_is_independent_of_invocation_depth(self) -> None:
        root = authority.repository_identity(self.a)
        for depth in ((self.a,), (self.a / "pkg",), (self.a / "pkg" / "src",)):
            self.assertEqual(root["id"], authority.repository_identity(depth[0])["id"], depth[0])
        # Same repository, incidental spelling.
        spelled = pathlib.Path(str(self.nested_a) + os.sep + "." + os.sep)
        self.assertEqual(root["id"], authority.repository_identity(spelled)["id"])
        link = self.base / "alias-into-a"
        link.symlink_to(self.nested_a, target_is_directory=True)
        self.assertEqual(root["id"], authority.repository_identity(link)["id"])
        self.assertEqual(root["workspace_instance"], authority.repository_identity(self.nested_a)["workspace_instance"])
        # A main-worktree nested invocation is not a linked worktree.
        self.assertFalse(authority.repository_identity(self.nested_a)["worktree"])

    def test_commit_stability_from_a_nested_invocation(self) -> None:
        before = authority.repository_identity(self.nested_a)
        (self.a / "f.txt").write_text("changed\n", encoding="utf-8")
        _git(self.a, "add", "-A")
        _git(self.a, "commit", "-qm", "second")
        after = authority.repository_identity(self.nested_a)
        self.assertEqual(before["id"], after["id"])
        self.assertNotEqual(before["head"], after["head"])

    def test_linked_worktree_from_a_nested_invocation(self) -> None:
        worktree = self.base / "A" / "linked"
        _git(self.a, "worktree", "add", "-q", "-b", "side", str(worktree))
        main = authority.repository_identity(self.a)
        linked = authority.repository_identity(worktree.joinpath(*NESTED))
        self.assertEqual(main["id"], linked["id"])              # one repository
        self.assertNotEqual(main["workspace_instance"], linked["workspace_instance"])
        self.assertTrue(linked["worktree"])
        self.assertEqual(linked["id"], authority.repository_identity(worktree)["id"])

    def test_remote_forms_are_stable_and_secret_free_from_a_nested_invocation(self) -> None:
        cases = {
            "https://example.com/o/r.git": "remote:https://example.com/o/r.git",
            "https://token:secret@example.com/o/r.git": "remote:https://example.com/o/r.git",
            "ssh://deploy:hunter2@example.com/o/r.git": "remote:ssh://example.com/o/r.git",
            "git@github.com:org/r.git": "remote:git@github.com:org/r.git",
        }
        for index, (remote, expected) in enumerate(cases.items()):
            repo = _repo(self.base / f"remote-{index}" / "project", remote=remote)
            identity = authority.repository_identity(repo.joinpath(*NESTED))
            self.assertEqual(expected, identity["id"])
            self.assertNotIn("secret", json.dumps(identity))
            self.assertNotIn("hunter2", json.dumps(identity))
            self.assertTrue(identity["portable"])

    def test_non_git_directories_with_the_same_basename_are_distinct(self) -> None:
        for parent in ("one", "two"):
            (self.base / parent / "plain" / "pkg" / "src").mkdir(parents=True)
        left = authority.repository_identity(self.base / "one" / "plain" / "pkg" / "src")
        right = authority.repository_identity(self.base / "two" / "plain" / "pkg" / "src")
        self.assertTrue(left["id"].startswith("path:"))
        self.assertFalse(left["portable"])
        self.assertNotEqual(left["id"], right["id"])

    def test_nested_explicit_workspace_from_an_unrelated_invocation_reports_conflict(self) -> None:
        resolved = authority.resolve_workspace(self.nested_a, invocation_dir=self.nested_b)
        self.assertEqual(str(self.nested_a.resolve()), resolved["workspace"])
        self.assertEqual(authority.EXPLICIT, resolved["source"])
        self.assertNotEqual(resolved["conflict"]["explicit_identity"],
                            resolved["conflict"]["discovered_identity"])
        self.assertEqual(resolved["repository"]["id"], resolved["conflict"]["explicit_identity"])
        self.assertFalse(resolved["is_infrastructure_repo"])

    def test_one_canonical_identity_is_shared_by_cache_and_resume_identity(self) -> None:
        canonical = authority.repository_identity(self.nested_a)
        delegated = orchestration_store.repository_identity(self.nested_a)
        self.assertEqual(canonical["id"], delegated["repository_id"])
        other = orchestration_store.repository_identity(self.nested_b)
        self.assertNotEqual(delegated["repository_id"], other["repository_id"])


class NestedCacheIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.a = _repo(self.base / "A" / "project")
        self.b = _repo(self.base / "B" / "project")
        self.state = self.base / "state"
        self.nested_a = self.a.joinpath(*NESTED)
        self.nested_b = self.b.joinpath(*NESTED)
        self.plan = _plan()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, workspace: pathlib.Path):
        backend = Stub()
        report = execution.execute_plan(self.plan, backend=backend, state_dir=self.state,
                                        repo_root=workspace, confirm=True, allow_dirty=True,
                                        record_evidence=False, cache=True)
        return report, backend

    def test_nested_workspaces_do_not_share_cached_results(self) -> None:
        cold, cold_backend = self._run(self.nested_a)
        self.assertEqual("SUCCESS", cold["nodes"]["A1"]["status"])
        self.assertEqual(1, len(cold_backend.calls))
        self.assertEqual(0, cold["cache"]["hits"])
        self.assertGreater(cold["cache"]["writes"], 0)

        warm, warm_backend = self._run(self.nested_a)
        self.assertEqual([], warm_backend.calls)          # same nested workspace reuses
        self.assertGreater(warm["cache"]["hits"], 0)

        other, other_backend = self._run(self.nested_b)
        self.assertEqual(1, len(other_backend.calls))     # unrelated repo: fresh work
        self.assertEqual(0, other["cache"]["hits"])       # A's result never adopted
        self.assertEqual(0, other.get("cache_reused", 0))

    def test_same_workspace_via_a_symlink_alias_still_reuses(self) -> None:
        self._run(self.nested_a)
        alias = self.base / "alias-into-a"
        alias.symlink_to(self.nested_a, target_is_directory=True)
        report, backend = self._run(alias)
        self.assertEqual([], backend.calls)               # canonicalization keeps reuse
        self.assertGreater(report["cache"]["hits"], 0)

    def test_production_workspace_entrypoint_yields_distinct_repository_identities(self) -> None:
        explicit_a = authority.resolve_workspace(self.nested_a)
        explicit_b = authority.resolve_workspace(self.nested_b)
        self.assertNotEqual(explicit_a["repository"]["id"], explicit_b["repository"]["id"])
        # Invocation from a nested directory discovers the toplevel, so identity is
        # depth-independent there too.
        invoked_a = authority.resolve_workspace(invocation_dir=self.nested_a)
        invoked_b = authority.resolve_workspace(invocation_dir=self.nested_b)
        self.assertEqual(str(self.a.resolve()), invoked_a["workspace"])
        self.assertNotEqual(invoked_a["repository"]["id"], invoked_b["repository"]["id"])
        self.assertEqual(invoked_a["repository"]["id"], explicit_a["repository"]["id"])


class NestedResumeIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.a = _repo(self.base / "A" / "project")
        self.b = _repo(self.base / "B" / "project")
        self.state = self.base / "state"
        self.nested_a = self.a.joinpath(*NESTED)
        self.nested_b = self.b.joinpath(*NESTED)
        self.plan = _plan()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_resume_from_an_unrelated_nested_workspace_does_not_adopt_results(self) -> None:
        first = Stub()
        report_a = execution.execute_plan(self.plan, backend=first, run_id="r1",
                                          state_dir=self.state, repo_root=self.nested_a,
                                          confirm=True, allow_dirty=True, record_evidence=False,
                                          cache=False)
        self.assertEqual("SUCCESS", report_a["nodes"]["A1"]["status"])
        self.assertEqual(1, len(first.calls))

        second = Stub()
        report_b = execution.execute_plan(self.plan, backend=second, run_id="r1", resume=True,
                                          state_dir=self.state, repo_root=self.nested_b,
                                          confirm=True, allow_dirty=True, record_evidence=False,
                                          cache=False)
        self.assertTrue(report_b.get("error", "").startswith("REFUSE_RESUME"), report_b.get("error"))
        self.assertEqual([], second.calls)                       # nothing launched
        self.assertEqual(1, len(first.calls))                    # A untouched
        self.assertIsNotNone(report_a["nodes"]["A1"].get("resume_input_identity"))

    def test_identity_alone_invalidates_reuse_under_the_same_head(self) -> None:
        # Independent of the workspace-path guard, the recorded resume identity is bound
        # to the repository identity: recomputing it for the other nested workspace (same
        # HEAD, same plan, same inputs) must report a change.
        first = Stub()
        report_a = execution.execute_plan(self.plan, backend=first, run_id="r1",
                                         state_dir=self.state, repo_root=self.nested_a,
                                         confirm=True, allow_dirty=True, record_evidence=False,
                                         cache=False)
        recorded = report_a["nodes"]["A1"]["resume_input_identity"]
        constraints = authority.bind_constraints(None)
        head = execution.repo_state(self.nested_b.resolve())["head"]
        project_b = orchestration_store.repository_identity(self.nested_b.resolve())["repository_id"]
        project_a = orchestration_store.repository_identity(self.nested_a.resolve())["repository_id"]
        self.assertNotEqual(project_a, project_b)
        changed = execution._resume_input_changed(
            self.plan, {"nodes": {"A1": {"status": "SUCCESS", "resume_input_identity": recorded}}},
            self.nested_b.resolve(), project_b, head, constraints)
        self.assertEqual("A1", changed)
        unchanged = execution._resume_node_identity(self.plan["nodes"][0], [],
                                                    self.nested_a.resolve(), project_a, head,
                                                    constraints)
        self.assertEqual(recorded, unchanged)
        self.assertNotEqual(recorded, execution._resume_node_identity(
            self.plan["nodes"][0], [], self.nested_b.resolve(), project_b, head, constraints))


class IdentityCacheKeyTests(unittest.TestCase):
    """The identity that enters cache/resume keys is the canonical one."""

    def test_cache_input_fingerprint_differs_across_nested_workspaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            a = _repo(base / "A" / "project").joinpath(*NESTED)
            b = _repo(base / "B" / "project").joinpath(*NESTED)
            node = _plan()["nodes"][0]
            semantic = result_cache.task_semantic_fingerprint(node)
            fingerprint = lambda identity: result_cache.input_fingerprint(  # noqa: E731
                project_id=identity, task_semantic_fingerprint=semantic, context_fingerprint="ctx",
                dependency_fingerprints=[], freshness_policy="input_bound", repository_head="head")
            id_a = authority.repository_identity(a)["id"]
            id_b = authority.repository_identity(b)["id"]
            self.assertNotEqual(fingerprint(id_a), fingerprint(id_b))
            self.assertEqual(fingerprint(id_a), fingerprint(
                authority.repository_identity(a.joinpath("."))["id"]))


class PolicyInteractionTests(unittest.TestCase):
    """The correction must not disturb accepted Master A authority behaviour."""

    def test_policy_binding_and_provenance_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            repo = _repo(base / "A" / "project")
            bound = authority.bind_constraints(None)
            self.assertIn("prohibited_providers", bound)
            self.assertIn("machine_policy", bound)
            resolved = authority.resolve_workspace(repo.joinpath(*NESTED))
            self.assertEqual(str(repo.joinpath(*NESTED).resolve()), resolved["workspace"])
            self.assertFalse(resolved["subdirectory"] is None)
            self.assertTrue(resolved["repository"]["is_repository"])
            self.assertEqual(failover.PROHIBITED_BY_MACHINE_POLICY, authority.PROHIBITED)


if __name__ == "__main__":
    unittest.main()
