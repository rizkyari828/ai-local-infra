"""Master A — effective runtime authority and workspace authority.

Hard machine policy is not a preference and not a health state: it cannot be
lifted by an explicit invocation, a machine-local file, or a healthy observation.
Everything here is synthetic or in a temporary repository — no provider, model,
network call, or real user config is touched.
"""

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
import authority  # noqa: E402
import bootstrap  # noqa: E402
import doctor  # noqa: E402
import failover  # noqa: E402
import providers  # noqa: E402
import execution  # noqa: E402

# scripts/verify-environment is not an importable module name.
import importlib.machinery  # noqa: E402
import importlib.util  # noqa: E402


def _load_verify_environment():
    loader = importlib.machinery.SourceFileLoader(
        "verify_environment_authority", str(ROOT / "scripts/verify-environment"))
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
    (path / "f.txt").write_text("x\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")
    return path


class BucketTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()


# --------------------------------------------------------------------------- #
# Effective authority: hard policy vs overridable defaults
# --------------------------------------------------------------------------- #


class EffectiveResolutionTests(BucketTestBase):
    def test_explicit_overrides_overridable_default(self) -> None:
        result = authority.effective("model", portable="portable-model",
                                     machine_local="local-model", explicit="cli-model")
        self.assertEqual("cli-model", result["value"])
        self.assertEqual(authority.EXPLICIT, result["source"])
        self.assertTrue(result["allowed"])

    def test_machine_local_overrides_portable_default(self) -> None:
        result = authority.effective("model", portable="portable-model", machine_local="local-model")
        self.assertEqual("local-model", result["value"])
        self.assertEqual(authority.MACHINE_LOCAL, result["source"])

    def test_portable_default_is_the_last_resort(self) -> None:
        result = authority.effective("model", portable="portable-model")
        self.assertEqual("portable-model", result["value"])
        self.assertEqual(authority.PORTABLE_DEFAULT, result["source"])

    def test_provenance_identifies_the_supplying_source(self) -> None:
        result = authority.effective("model", portable="p", explicit="e")
        self.assertEqual(authority.EXPLICIT, result["source"])
        self.assertEqual([authority.EXPLICIT, authority.PORTABLE_DEFAULT], result["layers"])

    def test_hard_prohibition_cannot_be_overridden_by_explicit_invocation(self) -> None:
        result = authority.effective("model", portable="p", machine_local="m", explicit="ollama",
                                     prohibited_by=authority.NOT_CONFIGURED_BY_MACHINE_POLICY)
        self.assertFalse(result["allowed"])
        self.assertIsNone(result["value"])
        self.assertEqual(authority.MACHINE_POLICY, result["source"])
        self.assertEqual(authority.NOT_CONFIGURED_BY_MACHINE_POLICY, result["blocked_by"])

    def test_no_layer_present_is_allowed_and_source_less(self) -> None:
        result = authority.effective("model")
        self.assertTrue(result["allowed"])
        self.assertIsNone(result["source"])

    def test_missing_optional_machine_config_falls_back_predictably(self) -> None:
        env = {"LAI_STATE_DIR": str(self.home / "absent-state")}
        self.assertEqual({}, authority.load_machine_local(env))
        result = authority.effective("model", portable="portable-model",
                                     machine_local=authority.load_machine_local(env).get("model", authority._UNSET))
        self.assertEqual("portable-model", result["value"])
        self.assertEqual(authority.PORTABLE_DEFAULT, result["source"])

    def test_malformed_machine_local_config_fails_safely(self) -> None:
        state = self.home / "state"
        state.mkdir(parents=True)
        (state / "machine.local.yaml").write_text("{ not json", encoding="utf-8")
        with self.assertRaises(authority.AuthorityError):
            authority.load_machine_local({"LAI_STATE_DIR": str(state)})


class MachinePolicyTests(unittest.TestCase):
    def test_work_mac_prohibits_local_inference(self) -> None:
        policy = authority.machine_policy({"LAI_MACHINE": "work-mac"})
        self.assertEqual("work-mac", policy["machine_id"])
        self.assertEqual(authority.MACHINE_ID_ENV, policy["machine_id_source"])
        self.assertEqual(authority.NOT_CONFIGURED_BY_MACHINE_POLICY, policy["local_inference"])
        self.assertIn("ollama", policy["prohibited"])
        self.assertEqual(authority.MACHINE_POLICY, policy["source"])

    def test_home_wsl_permits_local_inference(self) -> None:
        policy = authority.machine_policy({"LAI_MACHINE": "home-wsl"})
        self.assertEqual({}, policy["prohibited"])
        self.assertIsNone(policy["local_inference"])

    def test_provider_availability_false_is_a_prohibition(self) -> None:
        root = self.root_with_descriptor(
            {"id": "synthetic", "platform": "linux", "provider_availability": {"ollama": False}})
        policy = authority.machine_policy({"LAI_MACHINE": "synthetic"}, root=root)
        self.assertEqual(authority.PROHIBITED, policy["prohibited"]["ollama"])
        self.assertIsNone(policy["local_inference"])

    def test_unknown_machine_is_not_treated_as_a_prohibition(self) -> None:
        policy = authority.machine_policy({"LAI_MACHINE": "does-not-exist"})
        self.assertEqual({}, policy["prohibited"])
        self.assertIsNone(policy["descriptor"])
        self.assertIsNone(policy["local_inference"])

    def test_repo_path_matching_resolves_machine_identity(self) -> None:
        root = self.root_with_descriptor({"id": "bypath", "platform": "linux",
                                          "repo_path": str(self.root_path())}, name="bypath")
        machine_id, source = authority.resolve_machine_id({}, root=root)
        self.assertEqual("bypath", machine_id)
        self.assertEqual(authority.MACHINE_ID_REPO_PATH, source)

    def test_malformed_machine_descriptor_fails_explicitly(self) -> None:
        root = self.root_path()
        (root / "machines").mkdir(parents=True, exist_ok=True)
        (root / "machines" / "broken.yaml").write_text("{ nope", encoding="utf-8")
        policy = authority.machine_policy({"LAI_MACHINE": "broken"}, root=root)
        self.assertEqual(authority.MACHINE_DESCRIPTOR_MALFORMED, policy["state"])
        self.assertIsNotNone(policy["error"])
        self.assertEqual({}, policy["prohibited"])
        with self.assertRaises(authority.AuthorityError):
            authority.machine_policy_or_fail({"LAI_MACHINE": "broken"}, root=root)

    def test_mistyped_boolean_policy_field_fails_explicitly(self) -> None:
        root = self.root_with_descriptor(
            {"id": "synthetic", "platform": "linux",
             "provider_availability": {"ollama": "false"}})
        policy = authority.machine_policy({"LAI_MACHINE": "synthetic"}, root=root)
        self.assertEqual(authority.MACHINE_POLICY_FIELD_INVALID, policy["state"])
        self.assertIn("must be a boolean", policy["error"])
        # A mistyped prohibition is invalid, never a silent permission.
        self.assertNotEqual(authority.VALID_REGISTERED_MACHINE, policy["state"])
        with self.assertRaises(authority.AuthorityError):
            authority.machine_policy_or_fail({"LAI_MACHINE": "synthetic"}, root=root)

    def test_mistyped_local_inference_fails_explicitly(self) -> None:
        root = self.root_with_descriptor(
            {"id": "synthetic", "platform": "linux", "local_inference": False})
        policy = authority.machine_policy({"LAI_MACHINE": "synthetic"}, root=root)
        self.assertEqual(authority.MACHINE_POLICY_FIELD_INVALID, policy["state"])

    def test_explicit_unknown_selector_is_invalid_not_unregistered(self) -> None:
        policy = authority.machine_policy({"LAI_MACHINE": "work-mca"})  # typo of work-mac
        self.assertEqual(authority.EXPLICIT_MACHINE_SELECTOR_INVALID, policy["state"])
        self.assertIn("not a registered machine", policy["error"])
        with self.assertRaises(authority.AuthorityError):
            authority.machine_policy_or_fail({"LAI_MACHINE": "work-mca"})

    def test_auto_discovery_malformed_descriptor_is_not_unrestricted(self) -> None:
        root = self.root_path()
        (root / "machines").mkdir(parents=True, exist_ok=True)
        (root / "machines" / "unreadable.yaml").write_text("{ nope", encoding="utf-8")
        outcome = authority.machine_policy({}, root=root)
        self.assertEqual(authority.MACHINE_DESCRIPTOR_MALFORMED, outcome["state"])
        with self.assertRaises(authority.AuthorityError):
            authority.machine_policy_or_fail({}, root=root)

    def test_genuinely_unregistered_machine_reports_no_policy(self) -> None:
        root = self.root_path()
        (root / "machines").mkdir(parents=True, exist_ok=True)
        (root / "machines" / "other.yaml").write_text(
            json.dumps({"id": "other", "platform": "linux", "repo_path": "/somewhere/else",
                        "provider_availability": {"ollama": False}}), encoding="utf-8")
        outcome = authority.machine_policy({}, root=root)
        self.assertEqual(authority.GENUINELY_UNREGISTERED_MACHINE, outcome["state"])
        self.assertEqual({}, outcome["prohibited"])
        self.assertIsNone(outcome["error"])
        # The portable V0.1 behavior is explicitly distinguishable from an invalid policy.
        self.assertEqual(outcome, authority.machine_policy_or_fail({}, root=root))

    def test_machine_local_cannot_override_a_hard_prohibition(self) -> None:
        # provider_availability is owned by the committed descriptor: a local file may
        # carry a preference, never a permission.
        root = self.root_with_descriptor(
            {"id": "synthetic", "platform": "linux", "provider_availability": {"ollama": False}})
        state = pathlib.Path(self.tmp.name) / "state"
        state.mkdir(parents=True)
        (state / "machine.local.yaml").write_text(
            json.dumps({"provider_availability": {"ollama": True}}), encoding="utf-8")
        policy = authority.machine_policy({"LAI_MACHINE": "synthetic", "LAI_STATE_DIR": str(state)}, root=root)
        self.assertIn("ollama", policy["prohibited"])

    def test_machine_local_cannot_repair_an_invalid_policy(self) -> None:
        root = self.root_with_descriptor(
            {"id": "synthetic", "platform": "linux",
             "provider_availability": {"ollama": "false"}})
        state = pathlib.Path(self.tmp.name) / "state"
        state.mkdir(parents=True)
        (state / "machine.local.yaml").write_text(
            json.dumps({"provider_availability": {"ollama": False},
                        "local_inference": authority.NOT_CONFIGURED_BY_MACHINE_POLICY}), encoding="utf-8")
        env = {"LAI_MACHINE": "synthetic", "LAI_STATE_DIR": str(state)}
        policy = authority.machine_policy(env, root=root)
        self.assertEqual(authority.MACHINE_POLICY_FIELD_INVALID, policy["state"])
        self.assertEqual({}, policy["prohibited"])
        with self.assertRaises(authority.AuthorityError):
            authority.machine_policy_or_fail(env, root=root)

    def test_unknown_descriptor_keys_do_not_change_policy(self) -> None:
        root = self.root_with_descriptor(
            {"id": "synthetic", "platform": "linux", "provider_availability": {"ollama": True},
             "not_a_real_key": {"nested": True}})
        policy = authority.machine_policy({"LAI_MACHINE": "synthetic"}, root=root)
        self.assertEqual({}, policy["prohibited"])

    def root_with_descriptor(self, descriptor: dict, name: str = "synthetic") -> pathlib.Path:
        root = self.root_path()
        (root / "machines").mkdir(parents=True, exist_ok=True)
        (root / "machines" / f"{name}.yaml").write_text(json.dumps(descriptor), encoding="utf-8")
        return root

    def root_path(self) -> pathlib.Path:
        if not hasattr(self, "_root"):
            self._root = pathlib.Path(tempfile.mkdtemp(dir=self.tmp.name))
        return self._root

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tmp.cleanup()


class ObservationIsEvidenceTests(unittest.TestCase):
    def test_healthy_observation_cannot_authorize_a_prohibited_provider(self) -> None:
        policy = {"prohibited": {"ollama": authority.NOT_CONFIGURED_BY_MACHINE_POLICY}}
        gate = authority.observation_gate("ollama", authority.HEALTHY, policy=policy)
        self.assertFalse(gate["allowed"])
        self.assertFalse(gate["eligible"])
        self.assertEqual(authority.MACHINE_POLICY, gate["source"])

    def test_discovered_binary_and_installed_model_do_not_authorize(self) -> None:
        # Availability is not authorization: the binary exists and the model is loaded.
        health = failover.Health(binaries={"opencode": True}, providers={"opencode": {"ollama"}},
                                 ollama_models={"qwen3.6:27b-coding"},
                                 prohibited={"ollama": failover.NOT_CONFIGURED_BY_MACHINE_POLICY})
        candidate = failover.resolve("BALANCED", "medium", [{
            "backend": "opencode", "provider": "ollama", "model": "qwen3.6:27b-coding",
            "native_variant": "medium", "tier": "BALANCED", "effort": "medium"}], health)[0]
        self.assertEqual(failover.NOT_CONFIGURED_BY_MACHINE_POLICY, candidate["status"])
        self.assertFalse(candidate["runnable"])
        self.assertIn(failover.NOT_CONFIGURED_BY_MACHINE_POLICY, candidate["blocking"])

    def test_unhealthy_observation_removes_an_otherwise_permitted_candidate(self) -> None:
        gate = authority.observation_gate("deepseek", authority.UNHEALTHY, policy={"prohibited": {}})
        self.assertTrue(gate["allowed"])
        self.assertFalse(gate["eligible"])
        self.assertEqual(authority.RUNTIME_OBSERVATION, gate["source"])
        health = failover.Health(binaries={"opencode": False}, providers={"opencode": {"deepseek"}})
        self.assertEqual(failover.BACKEND_UNAVAILABLE, health.status(
            {"backend": "opencode", "provider": "deepseek", "model": "deepseek-flash"}))

    def test_cooldown_narrows_without_granting(self) -> None:
        gate = authority.observation_gate("deepseek", authority.HEALTHY, policy={"prohibited": {}},
                                          tripped=True)
        self.assertTrue(gate["allowed"])
        self.assertFalse(gate["eligible"])
        self.assertEqual("cooldown", gate["reason"])

    def test_observation_does_not_grant_a_permission_policy_never_gave(self) -> None:
        # No policy prohibition is still not a permission grant: an unhealthy candidate
        # stays ineligible even though nothing is prohibited.
        gate = authority.observation_gate("codex", authority.UNKNOWN, policy={"prohibited": {}})
        self.assertFalse(gate["eligible"])


# --------------------------------------------------------------------------- #
# Hard policy at the resolution boundary (the one shared authority)
# --------------------------------------------------------------------------- #


class PolicyBlockTests(unittest.TestCase):
    def test_policy_block_refuses_a_prohibited_provider(self) -> None:
        block = failover.policy_block("ollama", "BALANCED",
                                      {"prohibited_providers": {"ollama": failover.PROHIBITED_BY_MACHINE_POLICY}})
        self.assertEqual(failover.PROHIBITED_BY_MACHINE_POLICY, block)

    def test_local_only_constraint_still_applies(self) -> None:
        self.assertEqual("local_only", failover.policy_block("deepseek", "STRONG", {"local_only": True}))

    def test_prohibited_provider_beats_a_satisfiable_constraint(self) -> None:
        # An explicit invocation cannot lift a machine prohibition.
        block = failover.policy_block("ollama", "FAST",
                                      {"local_only": True,
                                       "prohibited_providers": {"ollama": failover.NOT_CONFIGURED_BY_MACHINE_POLICY}})
        self.assertEqual(failover.NOT_CONFIGURED_BY_MACHINE_POLICY, block)

    def test_machine_policy_binds_constraints_at_the_boundary(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"LAI_MACHINE": "work-mac"}):
            bound = authority.bind_constraints({"local_only": False})
        self.assertEqual({"ollama": authority.NOT_CONFIGURED_BY_MACHINE_POLICY},
                         bound["prohibited_providers"])
        self.assertEqual("work-mac", bound["machine_policy"]["machine_id"])

    def test_caller_cannot_weaken_a_machine_prohibition(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"LAI_MACHINE": "work-mac"}):
            bound = authority.bind_constraints(
                {"prohibited_providers": {"ollama": None, "deepseek": "caller_stricter"}})
        self.assertEqual(authority.NOT_CONFIGURED_BY_MACHINE_POLICY,
                         bound["prohibited_providers"]["ollama"])
        # A caller may still narrow policy further.
        self.assertEqual("caller_stricter", bound["prohibited_providers"]["deepseek"])

    def test_malformed_policy_refuses_instead_of_permitting(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"LAI_MACHINE": "work-mac"}), \
                unittest.mock.patch.object(authority, "machine_policy_or_fail",
                                           side_effect=authority.AuthorityError("boom")):
            with self.assertRaises(authority.AuthorityError):
                authority.bind_constraints({})


# --------------------------------------------------------------------------- #
# Structured configuration parsing
# --------------------------------------------------------------------------- #


class TomlParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "config.toml"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_codex_model_read_with_a_real_parser(self) -> None:
        self.path.write_text(
            'model_provider = "openai"\n'
            'model = "gpt-5.6-sol"\n'
            'model_reasoning_effort = "high"\n\n'
            "[mcp_servers.headroom]\n"
            'command = "/usr/local/bin/headroom"\n',
            encoding="utf-8")
        model, error = authority.read_codex_model(self.path)
        self.assertIsNone(error)
        self.assertEqual("gpt-5.6-sol", model)

    def test_malformed_toml_fails_explicitly_not_silently(self) -> None:
        self.path.write_text('model = "unterminated\n', encoding="utf-8")
        data, error = authority.read_toml(self.path)
        self.assertIsNone(data)
        self.assertIn("malformed TOML", error)
        model, error = authority.read_codex_model(self.path)
        self.assertIsNone(model)
        self.assertIn("malformed TOML", error)

    def test_absent_toml_is_absent_not_an_error(self) -> None:
        data, error = authority.read_toml(self.path)
        self.assertIsNone(data)
        self.assertIsNone(error)

    def test_non_string_model_is_rejected(self) -> None:
        self.path.write_text("model = 42\n", encoding="utf-8")
        model, error = authority.read_codex_model(self.path)
        self.assertIsNone(model)
        self.assertIsNotNone(error)

    def test_unknown_runtime_config_key_keeps_existing_rejection_policy(self) -> None:
        config = json.loads(json.dumps(providers.DEFAULT_RUNTIME_CONFIG))
        config["escalation"]["not_a_field"] = True
        with self.assertRaises(providers.ProviderConfigurationError):
            providers.validate_runtime_config(config)


# --------------------------------------------------------------------------- #
# Workspace authority
# --------------------------------------------------------------------------- #


class WorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.repo = _repo(self.base / "product", remote="https://example.com/org/product.git")
        (self.repo / "sub").mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_explicit_valid_workspace_wins_and_reports_its_source(self) -> None:
        result = authority.resolve_workspace(self.repo)
        self.assertEqual(str(self.repo.resolve()), result["workspace"])
        self.assertEqual(authority.EXPLICIT, result["source"])
        self.assertEqual("remote:https://example.com/org/product.git", result["repository"]["id"])
        self.assertFalse(result["is_infrastructure_repo"])

    def test_subdirectory_invocation_resolves_to_the_repository_toplevel(self) -> None:
        result = authority.resolve_workspace(invocation_dir=self.repo / "sub")
        self.assertEqual(str(self.repo.resolve()), result["workspace"])
        self.assertEqual("discovered_git_toplevel", result["source"])
        self.assertTrue(result["subdirectory"])

    def test_product_repo_invocation_is_not_the_infrastructure_repo(self) -> None:
        result = authority.resolve_workspace(invocation_dir=self.repo)
        self.assertFalse(result["is_infrastructure_repo"])
        self.assertNotEqual(str(ROOT.resolve()), result["workspace"])

    def test_infrastructure_repo_invocation_is_recognized(self) -> None:
        result = authority.resolve_workspace(invocation_dir=ROOT)
        self.assertTrue(result["is_infrastructure_repo"])
        self.assertEqual("ai-local-infra", result["repository"]["name"])

    def test_invalid_workspace_is_rejected_explicitly(self) -> None:
        with self.assertRaises(authority.AuthorityError) as caught:
            authority.resolve_workspace(self.base / "does-not-exist")
        self.assertIn("WORKSPACE_INVALID", str(caught.exception))

    def test_non_repository_directory_is_a_valid_workspace(self) -> None:
        plain = self.base / "plain"
        plain.mkdir()
        result = authority.resolve_workspace(plain)
        self.assertEqual(str(plain.resolve()), result["workspace"])
        self.assertFalse(result["repository"]["is_repository"])
        # Conservative identity: a hashed canonical path, never a bare directory name,
        # so two unrelated "plain" directories cannot collide.
        self.assertTrue(result["repository"]["id"].startswith("path:"))
        self.assertFalse(result["repository"]["portable"])
        other = self.base / "elsewhere" / "plain"
        other.mkdir(parents=True)
        self.assertNotEqual(result["repository"]["id"],
                            authority.resolve_workspace(other)["repository"]["id"])

    def test_conflicting_explicit_and_discovered_workspace_is_reported(self) -> None:
        other = _repo(self.base / "other", remote="https://example.com/org/other.git")
        result = authority.resolve_workspace(other, invocation_dir=self.repo)
        self.assertEqual(str(other.resolve()), result["workspace"])  # explicit stays authoritative
        self.assertEqual(authority.EXPLICIT, result["source"])
        self.assertIsNotNone(result["conflict"])
        self.assertEqual("remote:https://example.com/org/other.git", result["conflict"]["explicit_identity"])
        self.assertEqual("remote:https://example.com/org/product.git",
                         result["conflict"]["discovered_identity"])

    def test_no_conflict_when_explicit_matches_discovery(self) -> None:
        result = authority.resolve_workspace(self.repo, invocation_dir=self.repo / "sub")
        self.assertIsNone(result["conflict"])

    def test_repository_identity_is_stable_across_path_spelling(self) -> None:
        link = self.base / "spelled-differently"
        link.symlink_to(self.repo, target_is_directory=True)
        direct = authority.resolve_workspace(self.repo)
        spelled = authority.resolve_workspace(link)
        trailing = authority.resolve_workspace(str(self.repo) + os.sep + "." + os.sep)
        self.assertEqual(direct["repository"]["id"], spelled["repository"]["id"])
        self.assertEqual(direct["repository"]["id"], trailing["repository"]["id"])
        self.assertEqual(direct["workspace"], spelled["workspace"])
        self.assertEqual(direct["workspace"], trailing["workspace"])

    def test_linked_worktree_is_reported(self) -> None:
        worktree = self.base / "linked"
        _git(self.repo, "worktree", "add", "-b", "side", str(worktree))
        result = authority.resolve_workspace(worktree)
        self.assertTrue(result["worktree"])
        self.assertEqual(result["repository"]["root"], str(worktree.resolve()))

    def test_repository_relative_resolution_uses_the_active_workspace(self) -> None:
        # A subdirectory invocation must resolve sources against the workspace toplevel,
        # which is also the root the executor and tools are given.
        result = authority.resolve_workspace(invocation_dir=self.repo / "sub")
        self.assertEqual(execution.workspace_root(result["workspace"]), self.repo.resolve())
        self.assertEqual((pathlib.Path(result["workspace"]) / "f.txt").read_text(encoding="utf-8"), "x\n")


class WorkspaceCliTests(unittest.TestCase):
    def test_cli_renders_workspace_provenance(self) -> None:
        import orchestrate_cli
        result = authority.resolve_workspace(ROOT)
        rendered = orchestrate_cli._render_workspace(result)
        self.assertIn("WORKSPACE", rendered)
        self.assertIn("source=", rendered)

    def test_cli_rejects_an_invalid_workspace_explicitly(self) -> None:
        import orchestrate_cli
        with self.assertRaises(execution.ExecutionError):
            orchestrate_cli._resolve_workspace(type("A", (), {"workspace": ROOT / "nope"})())


# --------------------------------------------------------------------------- #
# Doctor / verify / runtime agreement
# --------------------------------------------------------------------------- #


class DoctorVerifyRuntimeAgreementTests(unittest.TestCase):
    """For one synthetic machine, all three surfaces report the same authority.

    They may differ in detail (verify probes health, doctor only reports policy),
    never in authoritative permitted/prohibited state.
    """

    def probe(self, machine: str) -> str:
        return authority.machine_policy({"LAI_MACHINE": machine})

    def test_prohibited_and_available(self) -> None:
        policy = self.probe("work-mac")
        self.assertIn("ollama", policy["prohibited"])

        with unittest.mock.patch.dict(os.environ, {"LAI_MACHINE": "work-mac"}), \
                unittest.mock.patch.object(VERIFY.urllib.request, "urlopen") as probe_call:
            self.assertEqual(authority.NOT_CONFIGURED_BY_MACHINE_POLICY,
                             VERIFY.machine_local_inference_policy())
        probe_call.assert_not_called()  # availability is not probed when prohibited

        with unittest.mock.patch.dict(os.environ, {"LAI_MACHINE": "work-mac"}):
            checks = doctor._authority_checks({}, dict(os.environ))
        self.assertEqual(doctor.PASS, checks[0].status)
        self.assertEqual("work-mac:NOT_CONFIGURED_BY_MACHINE_POLICY", checks[0].observed)

        health = failover.Health(binaries={"opencode": True}, providers={"opencode": {"ollama"}},
                                 ollama_models={"qwen3.6:27b-coding"},
                                 local_inference=policy["local_inference"], prohibited=policy["prohibited"])
        self.assertEqual(authority.NOT_CONFIGURED_BY_MACHINE_POLICY, health.status(
            {"backend": "opencode", "provider": "ollama", "model": "qwen3.6:27b-coding"}))

    def test_permitted_and_available(self) -> None:
        policy = self.probe("home-wsl")
        self.assertEqual({}, policy["prohibited"])
        with unittest.mock.patch.dict(os.environ, {"LAI_MACHINE": "home-wsl"}):
            checks = doctor._authority_checks({}, dict(os.environ))
        self.assertEqual(doctor.PASS, checks[0].status)
        self.assertEqual("home-wsl:PERMITTED", checks[0].observed)

        health = failover.Health(binaries={"opencode": True}, providers={"opencode": {"ollama"}},
                                 ollama_models={"qwen3.6:27b-coding"},
                                 prohibited=policy["prohibited"])
        self.assertEqual(failover.BACKEND_HEALTHY, health.status(
            {"backend": "opencode", "provider": "ollama", "model": "qwen3.6:27b-coding"}))

    def test_permitted_and_unavailable(self) -> None:
        policy = self.probe("home-wsl")
        health = failover.Health(binaries={"opencode": True}, providers={"opencode": {"ollama"}},
                                 ollama_models=None, prohibited=policy["prohibited"])
        status = health.status({"backend": "opencode", "provider": "ollama", "model": "qwen3.6:27b-coding"})
        self.assertEqual(failover.UNKNOWN, status)  # unavailable, never "not permitted"
        with unittest.mock.patch.dict(os.environ, {"LAI_MACHINE": "home-wsl"}):
            checks = doctor._authority_checks({}, dict(os.environ))
        self.assertEqual("home-wsl:PERMITTED", checks[0].observed)

    def test_not_configured_machine_has_no_prohibition(self) -> None:
        # A genuinely unregistered checkout (no explicit selector) keeps the portable
        # V0.1 behavior and reports it as NO_MACHINE_POLICY, not as a valid empty policy.
        root = pathlib.Path(tempfile.mkdtemp())
        (root / "machines").mkdir(parents=True)
        try:
            policy = authority.machine_policy({}, root=root)
            self.assertEqual(authority.GENUINELY_UNREGISTERED_MACHINE, policy["state"])
            self.assertEqual({}, policy["prohibited"])
        finally:
            __import__("shutil").rmtree(root, ignore_errors=True)

    def test_explicit_unknown_machine_fails_in_doctor(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"LAI_MACHINE": "work-mca"}):
            checks = doctor._authority_checks({}, dict(os.environ))
        self.assertEqual(doctor.FAIL, checks[0].status)
        self.assertIn(authority.EXPLICIT_MACHINE_SELECTOR_INVALID, checks[0].observed)

    def test_malformed_configuration_fails_in_doctor_not_silently(self) -> None:
        with unittest.mock.patch.object(authority, "machine_policy",
                                        side_effect=authority.AuthorityError("bad profile")):
            checks = doctor._authority_checks({}, {"LAI_MACHINE": "work-mac"})
        self.assertEqual(doctor.FAIL, checks[0].status)
        self.assertIn("bad profile", checks[0].reason)

    def test_verify_refuses_an_invalid_policy_instead_of_probing(self) -> None:
        # A policy that cannot be resolved is not a permission: no probe, not ready.
        with unittest.mock.patch.object(authority, "machine_policy") as policy, \
                unittest.mock.patch.object(VERIFY.urllib.request, "urlopen") as probe:
            policy.return_value = {"state": authority.MACHINE_POLICY_FIELD_INVALID,
                                   "prohibited": {}, "local_inference": None,
                                   "machine_id": "synthetic", "error": "provider_availability.ollama "
                                                                      "must be a boolean"}
            out = __import__("io").StringIO()
            with unittest.mock.patch.dict(os.environ, {"LAI_MACHINE": "synthetic"}), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(__import__("io").StringIO()):
                code = VERIFY.main()
        probe.assert_not_called()
        self.assertEqual(1, code)
        self.assertIn("SKIPPED_INVALID_MACHINE_POLICY", out.getvalue())

    def test_runtime_unhealthy(self) -> None:
        health = failover.Health(binaries={"opencode": False}, providers={"opencode": {"deepseek"}})
        self.assertEqual(failover.BACKEND_UNAVAILABLE, health.status(
            {"backend": "opencode", "provider": "deepseek", "model": "deepseek-flash"}))
        self.assertEqual({}, authority.machine_policy({"LAI_MACHINE": "home-wsl"})["prohibited"])


class AdversarialProbeTests(unittest.TestCase):
    """P1-P10, one probe per test, each asserting the effective authority, the
    provenance that supplied it, and the allowed/blocked result."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.repo = _repo(self.base / "product", remote="https://example.com/org/product.git")
        (self.repo / "sub").mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _candidate(self, model: str = "qwen3.6:27b-coding") -> dict:
        return {"backend": "opencode", "provider": "ollama", "model": model,
                "native_variant": "medium", "tier": "BALANCED", "effort": "medium"}

    def test_P1_machine_policy_forbids_ollama_but_binary_appears_available(self) -> None:
        policy = authority.machine_policy({"LAI_MACHINE": "work-mac"})
        health = failover.Health(binaries={"opencode": True}, providers={"opencode": {"ollama"}},
                                 ollama_models={"qwen3.6:27b-coding"},
                                 local_inference=policy["local_inference"], prohibited=policy["prohibited"])
        candidate = failover.resolve("BALANCED", "medium", [self._candidate()], health)[0]
        self.assertEqual(authority.MACHINE_POLICY, policy["source"])
        self.assertEqual(failover.NOT_CONFIGURED_BY_MACHINE_POLICY, candidate["status"])
        self.assertFalse(candidate["runnable"])

    def test_P2_explicit_invocation_asks_for_a_forbidden_backend(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"LAI_MACHINE": "work-mac"}):
            constraints = authority.bind_constraints({"prefer_provider": "ollama"})
        self.assertEqual("ollama", constraints["prefer_provider"])
        block = failover.policy_block("ollama", "BALANCED", constraints)
        self.assertEqual(authority.NOT_CONFIGURED_BY_MACHINE_POLICY, block)

    def test_P3_discovery_reports_a_model_configuration_never_authorized(self) -> None:
        # The endpoint reports a model no configuration authorized. It is still a
        # candidate only if it is authorized; an unapproved model is unavailable and
        # unknown discovery never becomes permission.
        health = failover.Health(binaries={"opencode": True}, providers={"opencode": {"ollama"}},
                                 ollama_models={"unapproved:latest"})
        candidate = failover.resolve("BALANCED", "medium", [self._candidate()], health)[0]
        self.assertEqual(failover.MODEL_UNAVAILABLE, candidate["status"])
        self.assertFalse(candidate["runnable"])
        import discovery
        capability = discovery.capability("ollama", "unapproved:latest")
        self.assertFalse(capability["known"])
        self.assertEqual(discovery.UNKNOWN, capability["status"])
        gate = authority.observation_gate("ollama", discovery.UNKNOWN, policy={"prohibited": {}})
        self.assertFalse(gate["eligible"])

    def test_P4_machine_local_default_conflicts_with_portable_default(self) -> None:
        result = authority.effective("prefer_model", portable="portable-model",
                                     machine_local="local-model")
        self.assertEqual("local-model", result["value"])
        self.assertEqual(authority.MACHINE_LOCAL, result["source"])
        self.assertTrue(result["allowed"])

    def test_P5_explicit_workspace_differs_from_current_directory(self) -> None:
        other = _repo(self.base / "other", remote="https://example.com/org/other.git")
        result = authority.resolve_workspace(other, invocation_dir=self.repo)
        self.assertNotEqual(str(self.repo.resolve()), result["workspace"])
        self.assertEqual(authority.EXPLICIT, result["source"])
        self.assertEqual("remote:https://example.com/org/other.git", result["repository"]["id"])
        self.assertIsNotNone(result["conflict"])

    def test_P6_invocation_from_project_subdirectory(self) -> None:
        result = authority.resolve_workspace(invocation_dir=self.repo / "sub")
        self.assertEqual(str(self.repo.resolve()), result["workspace"])
        self.assertEqual("discovered_git_toplevel", result["source"])
        self.assertTrue(result["subdirectory"])

    def test_P7_managed_guidance_plus_unrelated_user_instructions(self) -> None:
        # Covered end-to-end in tests/test_guidance_ownership.py; asserted here as a
        # probe so the adversarial set is complete and self-contained.
        text = (ROOT / "templates/codex/AGENTS.block.md").read_text(encoding="utf-8")
        begin, end = bootstrap.markers("html", "codex-guidance")
        self.assertIn(begin, bootstrap.render_block("html", "codex-guidance", text))
        self.assertNotEqual(begin, end)
        self.assertIn("codex-guidance", begin)
        owner = json.loads((ROOT / "config/bootstrap.yaml").read_text(encoding="utf-8"))
        unit = next(u for u in owner["units"] if u["id"] == "codex.guidance")
        self.assertEqual("managed-block", unit["mode"])
        self.assertEqual("{{codex.agents_path}}", unit["target"])

    def test_P8_malformed_toml_and_config(self) -> None:
        broken = self.base / "config.toml"
        broken.write_text("model = \n", encoding="utf-8")
        data, error = authority.read_toml(broken)
        self.assertIsNone(data)
        self.assertIsNotNone(error)
        root = self.base / "badroot"
        (root / "machines").mkdir(parents=True)
        (root / "machines" / "broken.yaml").write_text("nope", encoding="utf-8")
        policy = authority.machine_policy({"LAI_MACHINE": "broken"}, root=root)
        self.assertEqual(authority.MACHINE_DESCRIPTOR_MALFORMED, policy["state"])
        with self.assertRaises(authority.AuthorityError):
            authority.machine_policy_or_fail({"LAI_MACHINE": "broken"}, root=root)

    def test_P9_health_changes_between_two_observations(self) -> None:
        first = failover.Health(binaries={"opencode": True}, providers={"opencode": {"ollama"}},
                                ollama_models={"qwen3.6:27b-coding"})
        self.assertEqual(failover.BACKEND_HEALTHY, first.status(self._candidate()))
        second = failover.Health(binaries={"opencode": True}, providers={"opencode": {"ollama"}},
                                 ollama_models=None, tripped={"opencode"})
        self.assertEqual(failover.UNKNOWN, second.status(self._candidate()))
        self.assertTrue(second.is_tripped(self._candidate()))
        # Neither observation became durable authority: the only declared prohibition
        # is still the machine policy, with no health record persisted alongside it.
        policy = authority.machine_policy({"LAI_MACHINE": "work-mac"})
        self.assertEqual({"ollama"}, set(policy["prohibited"]))

    def test_P10_same_repository_via_different_incidental_path_spelling(self) -> None:
        link = self.base / "spelling"
        link.symlink_to(self.repo, target_is_directory=True)
        first = authority.resolve_workspace(self.repo)
        second = authority.resolve_workspace(link)
        self.assertEqual(first["repository"]["id"], second["repository"]["id"])
        self.assertEqual(first["workspace"], second["workspace"])


if __name__ == "__main__":
    unittest.main()
