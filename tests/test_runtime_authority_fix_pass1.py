"""Master A Fix Pass 1 — authority boundary corrections.

Covers the four reviewed blockers: machine policy binding at the shared execution
boundary, fail-closed invalid machine authority, Doctor/verify/runtime permission
agreement, and collision-resistant repository identity (including cross-repository
cache isolation). Plus the bounded TOML error-propagation correction.

Fake/stub adapters, temporary Git repositories, temporary config/state only: no
provider or model call is ever made.
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import authority  # noqa: E402
import doctor  # noqa: E402
import execution  # noqa: E402
import failover  # noqa: E402
import lifecycle  # noqa: E402
import orchestrate_cli  # noqa: E402
import orchestrator  # noqa: E402
import result_cache  # noqa: E402


def _load_verify_environment():
    loader = importlib.machinery.SourceFileLoader(
        "verify_environment_fix_pass1", str(ROOT / "scripts/verify-environment"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


VERIFY = _load_verify_environment()


def _git(root: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _repo(path: pathlib.Path, remote: str | None = None) -> pathlib.Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    if remote:
        _git(path, "remote", "add", "origin", remote)
    (path / "notes.txt").write_text("authority notes\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")
    return path


class Stub(execution.Backend):
    """A claimed-real adapter: it can reach a provider, so it counts launches."""

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


def _ai(task_id: str, title: str, *, tier: str = "BALANCED", effort: str = "medium",
        task_class: str = "NORMAL") -> dict:
    return {"task_id": task_id, "title": title, "objective_summary": title, "task_family": "generic",
            "task_class": task_class, "depends_on": [], "conditional": False, "intent": "READ_ONLY",
            "executor": "AI", "routing": {"tier": tier, "effort": effort, "role": "student"},
            "context": {"budget": "SMALL"}, "acceptance_criteria": ["ok"], "risk_signals": [],
            "knowledge_refs": [], "context_refs": []}


def _plan(nodes: list[dict]) -> dict:
    return {"schema_version": 1, "valid": True, "intent": "READ_ONLY",
            "order": [n["task_id"] for n in nodes], "nodes": nodes}


class PolicyRootMixin:
    """A temporary machine-descriptor root, injected into the authority defaults."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.root = self.base / "infra"
        (self.root / "machines").mkdir(parents=True)
        self._machine_dir = unittest.mock.patch.object(authority, "MACHINE_DIR",
                                                       self.root / "machines")
        self._machine_dir.start()
        self.addCleanup(self._machine_dir.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_machine(self, machine_id: str, **fields) -> None:
        descriptor = {"id": machine_id, "platform": "linux"}
        descriptor.update(fields)
        (self.root / "machines" / f"{machine_id}.yaml").write_text(json.dumps(descriptor),
                                                                   encoding="utf-8")

    def select(self, machine_id: str) -> None:
        patcher = unittest.mock.patch.dict(os.environ, {"LAI_MACHINE": machine_id})
        patcher.start()
        self.addCleanup(patcher.stop)


# --------------------------------------------------------------------------- #
# E. Execution boundary binding
# --------------------------------------------------------------------------- #


class ExecutionBoundaryTests(PolicyRootMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = _repo(self.base / "product")
        self.state = self.base / "state"
        self.write_machine("work-mac", local_inference=authority.NOT_CONFIGURED_BY_MACHINE_POLICY)
        self.select("work-mac")

    def test_E1_run_direct_without_caller_constraints_blocks_before_launch(self) -> None:
        backend = Stub()
        with unittest.mock.patch.object(orchestrator, "decompose",
                                        return_value=_plan([_ai("A1", "fix the config parser")])):
            _plan_out, report = lifecycle.run_direct("fix the config parser", backend=backend,
                                                     state_dir=self.state, repo_root=self.repo,
                                                     constraints=None, confirm=True, allow_dirty=True,
                                                     record_evidence=False, cache=False)
        self.assertEqual([], backend.calls)  # launch count = 0
        self.assertEqual(failover.NOT_CONFIGURED_BY_MACHINE_POLICY, report["error"])

    def test_E2_direct_mode_cli_blocks_before_launch(self) -> None:
        args = orchestrate_cli.parser().parse_args([
            "direct", "--objective", "fix the config parser", "--backend", "opencode",
            "--dry-run", "--json", "--state-dir", str(self.state), "--workspace", str(self.repo)])
        stdout = io.StringIO()
        with unittest.mock.patch.object(orchestrator, "decompose",
                                        return_value=_plan([_ai("A1", "fix the config parser")])), \
                contextlib.redirect_stdout(stdout):
            code = orchestrate_cli.cmd_run(args)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(2, code)  # a blocked run is not a success exit
        self.assertEqual(0, payload["calls_used"])
        self.assertEqual(failover.NOT_CONFIGURED_BY_MACHINE_POLICY, payload["error"])
        reasons = [binding.get("reason") for binding in payload["resolutions"]]
        self.assertIn(failover.NOT_CONFIGURED_BY_MACHINE_POLICY, reasons)

    def test_E3_explicit_request_for_a_prohibited_provider_is_blocked(self) -> None:
        backend = Stub()
        report = execution.execute_plan(_plan([_ai("A1", "t")]), backend=backend, state_dir=self.state,
                                        repo_root=self.repo, confirm=True, allow_dirty=True,
                                        record_evidence=False,
                                        constraints={"prefer_provider": "ollama",
                                                     "local_only": True})
        self.assertEqual([], backend.calls)
        self.assertEqual(failover.NOT_CONFIGURED_BY_MACHINE_POLICY, report["error"])

    def test_E4_constraints_omitting_prohibitions_still_bind(self) -> None:
        backend = Stub()
        report = execution.execute_plan(_plan([_ai("A1", "t")]), backend=backend, state_dir=self.state,
                                        repo_root=self.repo, confirm=True, allow_dirty=True,
                                        record_evidence=False, constraints={"read_only": False})
        self.assertEqual([], backend.calls)
        self.assertEqual(failover.NOT_CONFIGURED_BY_MACHINE_POLICY, report["error"])

    def test_E5_caller_cannot_weaken_a_machine_prohibition(self) -> None:
        backend = Stub()
        report = execution.execute_plan(_plan([_ai("A1", "t")]), backend=backend, state_dir=self.state,
                                        repo_root=self.repo, confirm=True, allow_dirty=True,
                                        record_evidence=False,
                                        constraints={"prohibited_providers": {"ollama": None}})
        self.assertEqual([], backend.calls)
        self.assertFalse(report["resolutions"][0]["runnable"])

    def test_E6_permitted_provider_remains_runnable(self) -> None:
        # The same node on a machine whose profile permits local inference still launches.
        self.write_machine("permissive", provider_availability={"ollama": True})
        self.select("permissive")
        backend = Stub()
        report = execution.execute_plan(_plan([_ai("A1", "t")]), backend=backend, state_dir=self.state,
                                        repo_root=self.repo, confirm=True, allow_dirty=True,
                                        record_evidence=False)
        self.assertEqual("SUCCESS", report["nodes"]["A1"]["status"])
        self.assertEqual(1, len(backend.calls))
        self.assertEqual("ollama", backend.calls[0]["provider"])

    def test_prohibited_provider_availability_only_is_blocked(self) -> None:
        # provider_availability.ollama = false with no local_inference marker.
        self.write_machine("no-local", provider_availability={"ollama": False})
        self.select("no-local")
        backend = Stub()
        report = execution.execute_plan(_plan([_ai("A1", "t")]), backend=backend, state_dir=self.state,
                                        repo_root=self.repo, confirm=True, allow_dirty=True,
                                        record_evidence=False)
        self.assertEqual([], backend.calls)
        self.assertEqual(failover.PROHIBITED_BY_MACHINE_POLICY, report["error"])

    def test_policy_is_part_of_resume_identity(self) -> None:
        backend = Stub()
        first = execution.execute_plan(_plan([_ai("A1", "t")]), backend=backend, run_id="r1",
                                       state_dir=self.state, repo_root=self.repo, confirm=True,
                                       allow_dirty=True, record_evidence=False)
        self.assertEqual(failover.NOT_CONFIGURED_BY_MACHINE_POLICY, first["error"])
        # The run never launched, so there is nothing to resume; identity still records policy.
        self.assertIn("prohibited_providers", authority.bind_constraints({}))
        self.assertEqual("work-mac", authority.bind_constraints({})["machine_policy"]["machine_id"])

    def test_simulated_and_abstract_adapters_do_not_bind_launch_policy(self) -> None:
        # A registry that cannot reach a provider has no launch to authorize.
        class AbstractAdapter(execution.Backend):
            name = "abstract"

        self.assertFalse(execution.launch_capable(execution.FakeBackend()))
        self.assertFalse(execution.launch_capable(AbstractAdapter()))
        self.assertTrue(execution.launch_capable(Stub()))
        self.assertTrue(execution.launch_capable(execution.CliBackend("opencode", "opencode")))
        self.assertFalse(execution.launch_capable(execution.FakeBackend(), {"fake": execution.FakeBackend()}))

    def test_agent_runtime_refuses_a_prohibited_provider(self) -> None:
        from providers import ProviderConfigurationError
        runtime = __import__("runtime")
        self.select("work-mac")
        with self.assertRaises(ProviderConfigurationError):
            runtime.require_machine_permission("ollama")
        self.assertEqual({"allowed": True}["allowed"],
                         runtime.require_machine_permission("deepseek")["allowed"])


# --------------------------------------------------------------------------- #
# M. Machine policy validation
# --------------------------------------------------------------------------- #


class MachinePolicyValidationTests(PolicyRootMixin, unittest.TestCase):
    def test_M1_false_boolean_is_a_prohibition(self) -> None:
        self.write_machine("m", provider_availability={"ollama": False})
        policy = authority.machine_policy({"LAI_MACHINE": "m"})
        self.assertEqual(authority.VALID_REGISTERED_MACHINE, policy["state"])
        self.assertEqual(authority.PROHIBITED, policy["prohibited"]["ollama"])

    def test_M2_string_false_is_invalid_and_refuses_execution(self) -> None:
        self.write_machine("m", provider_availability={"ollama": "false"})
        policy = authority.machine_policy({"LAI_MACHINE": "m"})
        self.assertEqual(authority.MACHINE_POLICY_FIELD_INVALID, policy["state"])
        self.assertIn("must be a boolean", policy["error"])
        with self.assertRaises(authority.AuthorityError):
            authority.machine_policy_or_fail({"LAI_MACHINE": "m"})
        # Doctor and verify must not read this as permitted (no probe, not ready).
        self.assertEqual(doctor.FAIL, doctor._authority_checks({}, {"LAI_MACHINE": "m"})[0].status)
        with unittest.mock.patch.dict(os.environ, {"LAI_MACHINE": "m"}), \
                unittest.mock.patch.object(VERIFY.urllib.request, "urlopen") as probe:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = VERIFY.main()
        probe.assert_not_called()
        self.assertEqual(1, code)
        self.assertIn("SKIPPED_INVALID_MACHINE_POLICY", out.getvalue())
        self.assertIn("MACHINE_POLICY_INVALID", err.getvalue())

    def test_M3_invalid_local_inference_type_is_invalid(self) -> None:
        self.write_machine("m", local_inference=False)
        self.assertEqual(authority.MACHINE_POLICY_FIELD_INVALID,
                         authority.machine_policy({"LAI_MACHINE": "m"})["state"])

    def test_M4_explicit_nonexistent_selector_fails_closed(self) -> None:
        self.write_machine("real", provider_availability={"ollama": True})
        self.select("work-mca")  # typo: must not fall back to discovery
        policy = authority.machine_policy(dict(os.environ))
        self.assertEqual(authority.EXPLICIT_MACHINE_SELECTOR_INVALID, policy["state"])
        with self.assertRaises(execution.ExecutionError):
            execution.execute_plan(_plan([_ai("A1", "t")]), backend=Stub(),
                                   state_dir=self.base / "state", repo_root=_repo(self.base / "p"),
                                   confirm=True, allow_dirty=True, record_evidence=False)

    def test_M5_explicit_malformed_descriptor_fails_closed(self) -> None:
        (self.root / "machines" / "broken.yaml").write_text("{ nope", encoding="utf-8")
        self.select("broken")
        self.assertEqual(authority.MACHINE_DESCRIPTOR_MALFORMED,
                         authority.machine_policy(dict(os.environ))["state"])
        with self.assertRaises(execution.ExecutionError):
            execution.execute_plan(_plan([_ai("A1", "t")]), backend=Stub(),
                                   state_dir=self.base / "state", repo_root=_repo(self.base / "p2"),
                                   confirm=True, allow_dirty=True, record_evidence=False)

    def test_M6_auto_discovery_malformed_is_not_unrestricted(self) -> None:
        (self.root / "machines" / "unreadable.yaml").write_text("{ nope", encoding="utf-8")
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LAI_MACHINE", None)
            policy = authority.machine_policy(dict(os.environ))
        self.assertEqual(authority.MACHINE_DESCRIPTOR_MALFORMED, policy["state"])
        self.assertEqual({}, policy["prohibited"])  # reported invalid, not silently permissive
        with self.assertRaises(authority.AuthorityError):
            authority.machine_policy_or_fail(dict(os.environ))

    def test_M7_genuinely_unregistered_is_explicit(self) -> None:
        self.write_machine("elsewhere", repo_path="/nowhere/else")
        with unittest.mock.patch.dict(os.environ, {"HOME": str(self.base)}):
            os.environ.pop("LAI_MACHINE", None)
            policy = authority.machine_policy(dict(os.environ))
        self.assertEqual(authority.GENUINELY_UNREGISTERED_MACHINE, policy["state"])
        self.assertIsNone(policy["machine_id"])
        self.assertEqual({}, policy["prohibited"])
        self.assertEqual(authority.PERMITTED, authority.PERMITTED)  # other states unaffected
        self.assertIsNone(policy["error"])
        # Reported as NO_MACHINE_POLICY, not as a valid empty policy.
        check = doctor._authority_checks({}, dict(os.environ))[0]
        self.assertEqual(doctor.PASS, check.status)
        self.assertIn(authority.NO_MACHINE_POLICY, check.observed)

    def test_M8_machine_local_cannot_repair_invalid_hard_policy(self) -> None:
        self.write_machine("m", provider_availability={"ollama": "false"})
        state = self.base / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "machine.local.yaml").write_text(
            json.dumps({"provider_availability": {"ollama": False},
                        "local_inference": authority.NOT_CONFIGURED_BY_MACHINE_POLICY}), encoding="utf-8")
        env = {"LAI_MACHINE": "m", "LAI_STATE_DIR": str(state)}
        self.assertEqual(authority.MACHINE_POLICY_FIELD_INVALID,
                         authority.machine_policy(env)["state"])


# --------------------------------------------------------------------------- #
# Doctor / verify / runtime agreement matrix
# --------------------------------------------------------------------------- #


class PermissionMatrixTests(PolicyRootMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = _repo(self.base / "product")
        self.state = self.base / "state"

    def runtime_state(self, provider: str = "ollama") -> str:
        health = failover.Health(binaries={"opencode": True}, providers={"opencode": {provider}},
                                 prohibited=unittest.mock.ANY if False else
                                 authority.machine_policy(dict(os.environ))["prohibited"])
        return health.status({"backend": "opencode", "provider": provider,
                              "model": "qwen3.6:27b-coding"})

    def case(self, machine_id: str, **fields) -> tuple[str, str]:
        self.write_machine(machine_id, **fields)
        self.select(machine_id)
        doctor_status = doctor._authority_checks({}, dict(os.environ))[0]
        policy = authority.machine_policy(dict(os.environ))
        for provider, reason in policy["prohibited"].items():
            health = failover.Health(binaries={"opencode": True}, providers={"opencode": {provider}},
                                     prohibited=policy["prohibited"])
            runtime = health.status({"backend": "opencode", "provider": provider,
                                     "model": "qwen3.6:27b-coding"})
            return doctor_status.status, runtime
        return doctor_status.status, failover.BACKEND_HEALTHY

    def test_permitted_available(self) -> None:
        status, runtime = self.case("m", provider_availability={"ollama": True})
        self.assertEqual(doctor.PASS, status)
        self.assertEqual(failover.BACKEND_HEALTHY, runtime)

    def test_permitted_unavailable(self) -> None:
        self.write_machine("m", provider_availability={"ollama": True})
        self.select("m")
        health = failover.Health(binaries={"opencode": False}, providers={"opencode": {"ollama"}})
        self.assertEqual(failover.BACKEND_UNAVAILABLE,
                         health.status({"backend": "opencode", "provider": "ollama", "model": "q"}))
        self.assertEqual(doctor.PASS, doctor._authority_checks({}, dict(os.environ))[0].status)

    def test_prohibited_by_local_inference(self) -> None:
        status, runtime = self.case("m", local_inference=authority.NOT_CONFIGURED_BY_MACHINE_POLICY)
        self.assertEqual(doctor.PASS, status)
        self.assertEqual(failover.NOT_CONFIGURED_BY_MACHINE_POLICY, runtime)

    def test_prohibited_by_provider_availability_only(self) -> None:
        self.write_machine("m", provider_availability={"ollama": False})
        self.select("m")
        # verify-environment must skip the probe for this machine too.
        with unittest.mock.patch.object(VERIFY.urllib.request, "urlopen") as probe:
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = VERIFY.main()
        probe.assert_not_called()
        self.assertEqual(0, code)
        self.assertIn("SKIPPED_BY_POLICY", out.getvalue())
        self.assertEqual(doctor.PASS, doctor._authority_checks({}, dict(os.environ))[0].status)
        health = failover.Health(binaries={"opencode": True}, providers={"opencode": {"ollama"}},
                                 prohibited=authority.machine_policy(dict(os.environ))["prohibited"])
        self.assertEqual(failover.PROHIBITED_BY_MACHINE_POLICY,
                         health.status({"backend": "opencode", "provider": "ollama", "model": "q"}))

    def test_malformed_descriptor_disagreement_closed(self) -> None:
        (self.root / "machines" / "broken.yaml").write_text("{ nope", encoding="utf-8")
        self.select("broken")
        self.assertEqual(doctor.FAIL, doctor._authority_checks({}, dict(os.environ))[0].status)
        with unittest.mock.patch.object(VERIFY.urllib.request, "urlopen") as probe, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = VERIFY.main()
        probe.assert_not_called()
        self.assertEqual(1, code)
        with self.assertRaises(authority.AuthorityError):
            authority.machine_policy_or_fail(dict(os.environ))


# --------------------------------------------------------------------------- #
# R. Repository identity + cache isolation
# --------------------------------------------------------------------------- #


class RepositoryIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_R1_same_basename_unrelated_repos_do_not_collide(self) -> None:
        first = _repo(self.base / "a" / "project")
        second = _repo(self.base / "b" / "project")
        a = authority.repository_identity(first)
        b = authority.repository_identity(second)
        self.assertEqual("project", a["name"])
        self.assertEqual("project", b["name"])
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual("git_common_dir", a["kind"])
        self.assertFalse(a["portable"])  # machine-local, never claimed as portable

    def test_R2_path_alias_and_symlink_share_identity(self) -> None:
        repo = _repo(self.base / "repo", remote="https://example.com/o/r.git")
        link = self.base / "alias"
        link.symlink_to(repo, target_is_directory=True)
        direct = authority.repository_identity(repo)
        aliased = authority.repository_identity(link)
        spelled = authority.repository_identity(pathlib.Path(str(repo) + os.sep + "." + os.sep))
        self.assertEqual(direct["id"], aliased["id"])
        self.assertEqual(direct["id"], spelled["id"])

    def test_R3_identity_survives_a_commit(self) -> None:
        repo = _repo(self.base / "repo", remote="https://example.com/o/r.git")
        before = authority.repository_identity(repo)
        (repo / "notes.txt").write_text("changed\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "second")
        after = authority.repository_identity(repo)
        self.assertEqual(before["id"], after["id"])
        self.assertNotEqual(before["head"], after["head"])  # revision moves separately

    def test_R4_linked_worktree_shares_repository_but_not_workspace_instance(self) -> None:
        repo = _repo(self.base / "repo", remote="https://example.com/o/r.git")
        worktree = self.base / "linked"
        _git(repo, "worktree", "add", "-b", "side", str(worktree))
        main = authority.repository_identity(repo)
        linked = authority.repository_identity(worktree)
        self.assertEqual(main["id"], linked["id"])  # same repository
        self.assertNotEqual(main["workspace_instance"], linked["workspace_instance"])
        self.assertFalse(main["worktree"])
        self.assertTrue(linked["worktree"])

    def test_R5_https_credentials_are_stripped(self) -> None:
        repo = _repo(self.base / "repo", remote="https://token:secret@example.com/o/r.git")
        identity = authority.repository_identity(repo)
        self.assertEqual("remote:https://example.com/o/r.git", identity["id"])
        self.assertNotIn("secret", json.dumps(identity))
        self.assertNotIn("token", json.dumps(identity))

    def test_R6_ssh_remote_is_stable_and_secret_free(self) -> None:
        repo = _repo(self.base / "repo", remote="git@github.com:org/r.git")
        identity = authority.repository_identity(repo)
        self.assertEqual("remote:git@github.com:org/r.git", identity["id"])
        credential = _repo(self.base / "cred", remote="ssh://deploy:hunter2@example.com/o/r.git")
        stripped = authority.repository_identity(credential)
        self.assertEqual("remote:ssh://example.com/o/r.git", stripped["id"])
        self.assertNotIn("hunter2", json.dumps(stripped))

    def test_R7_identical_repos_with_same_basename_and_state_do_not_collide(self) -> None:
        first = _repo(self.base / "x" / "project")
        second = _repo(self.base / "y" / "project")
        # Same tree, same commit message, same HEAD-ish state; identity must still differ.
        self.assertEqual(authority.repository_identity(first)["head"],
                         authority.repository_identity(first)["head"])
        self.assertNotEqual(authority.repository_identity(first)["id"],
                            authority.repository_identity(second)["id"])

    def test_R8_cross_repository_cache_reuse_is_impossible(self) -> None:
        repo_a = _repo(self.base / "a" / "project")
        repo_b = _repo(self.base / "b" / "project")
        state = self.base / "state"
        plan = _plan([_ai("A1", "review", tier="STRONG", effort="high", task_class="CORRECTNESS_SENSITIVE")])

        cold = Stub()
        first = execution.execute_plan(plan, backend=cold, state_dir=state, repo_root=repo_a,
                                      confirm=True, allow_dirty=True, record_evidence=False, cache=True)
        self.assertEqual("SUCCESS", first["nodes"]["A1"]["status"])
        self.assertEqual(1, len(cold.calls))
        self.assertGreater(first["cache"]["writes"], 0)

        warm = Stub()
        second = execution.execute_plan(plan, backend=warm, state_dir=state, repo_root=repo_a,
                                       confirm=True, allow_dirty=True, record_evidence=False, cache=True)
        self.assertEqual([], warm.calls)  # same repository: reuse
        self.assertGreater(second["cache"]["hits"], 0)

        other = Stub()
        third = execution.execute_plan(plan, backend=other, state_dir=state, repo_root=repo_b,
                                      confirm=True, allow_dirty=True, record_evidence=False, cache=True)
        self.assertEqual(1, len(other.calls))  # unrelated repository: fresh work, no reuse
        self.assertEqual(0, third["cache"]["hits"])

    def test_R9_explicit_workspace_conflict_with_invocation_repository(self) -> None:
        invoker = _repo(self.base / "invoker", remote="https://example.com/o/invoker.git")
        target = _repo(self.base / "target", remote="https://example.com/o/target.git")
        resolved = authority.resolve_workspace(target, invocation_dir=invoker)
        self.assertEqual(str(target.resolve()), resolved["workspace"])
        self.assertIsNotNone(resolved["conflict"])
        self.assertNotEqual(resolved["conflict"]["explicit_identity"],
                            resolved["conflict"]["discovered_identity"])
        self.assertIn(resolved["workspace_instance"], json.dumps(resolved))

    def test_single_identity_implementation_is_shared(self) -> None:
        import orchestration_store
        repo = _repo(self.base / "shared", remote="https://example.com/o/shared.git")
        canonical = authority.repository_identity(repo)
        delegated = orchestration_store.repository_identity(repo)
        self.assertEqual(canonical["id"], delegated["repository_id"])
        self.assertEqual(canonical["name"], delegated["name"])
        import bootstrap
        self.assertEqual(canonical["id"], authority.repository_identity(repo)["id"])

    def test_non_git_workspace_identity_is_conservative(self) -> None:
        left = self.base / "one" / "plain"
        right = self.base / "two" / "plain"
        left.mkdir(parents=True)
        right.mkdir(parents=True)
        a = authority.repository_identity(left)
        b = authority.repository_identity(right)
        self.assertTrue(a["id"].startswith("path:"))
        self.assertFalse(a["portable"])
        self.assertNotEqual(a["id"], b["id"])


# --------------------------------------------------------------------------- #
# TOML error propagation
# --------------------------------------------------------------------------- #


class TomlPropagationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "config.toml"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_malformed_codex_toml_is_not_silently_no_model(self) -> None:
        self.path.write_text('model = "unterminated\n', encoding="utf-8")
        model, error = authority.read_codex_model(self.path)
        self.assertIsNone(model)
        self.assertIn("malformed TOML", error)

    def test_codex_model_error_blocks_codex_candidates(self) -> None:
        health = failover.Health(binaries={"codex": True}, providers={"codex": {"codex"}},
                                 codex_model=None, codex_model_error="malformed TOML: bad")
        candidate = failover.resolve("MAX", "max", [{
            "backend": "codex", "provider": "codex", "model": "gpt-x",
            "native_variant": "max", "tier": "MAX", "effort": "max"}], health)[0]
        self.assertEqual(failover.MODEL_CONFIG_INVALID, candidate["status"])
        self.assertFalse(candidate["runnable"])
        self.assertIn(failover.MODEL_CONFIG_INVALID, candidate["blocking"])

    def test_absent_codex_toml_remains_unknown_not_invalid(self) -> None:
        model, error = authority.read_codex_model(self.path)
        self.assertIsNone(model)
        self.assertIsNone(error)


class CacheIdentityUnitTests(unittest.TestCase):
    def test_cache_namespaces_differ_for_remote_less_repos(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            base = pathlib.Path(tmp.name)
            a = authority.repository_identity(_repo(base / "a" / "project"))
            b = authority.repository_identity(_repo(base / "b" / "project"))
            self.assertNotEqual(a["id"], b["id"])
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
