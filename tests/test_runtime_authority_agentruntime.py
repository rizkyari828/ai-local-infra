"""Master A targeted audit — AgentRuntime authority.

Question under audit: can any production-reachable provider-backed execution occur
without the authoritative machine-policy binding that ``execute_plan`` guarantees?

The audit found no production bypass, and Fix Pass 1.1 closed the residual by
moving the check to the execution boundary itself. These tests record the result:

- ``AgentRuntime.run`` checks authoritative machine permission immediately before
  the first provider invocation, so construction method cannot affect safety —
  ``from_config``, direct construction, and an escalation factory that omits its
  early check are all stopped;
- the config-driven factories still check early (fail-fast diagnostics), and a
  source-level guard flags a new production ``AgentRuntime(...)`` site for review;
- every production adapter that can reach a provider is ``SUPPORTED``, so
  ``execute_plan``'s hard-policy gate applies to it;
- pretend adapters (SIMULATED/UNSUPPORTED) cannot reach a provider at all.

Stub providers and temporary machine descriptors only: no real provider or model
call is made.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import authority  # noqa: E402
import escalation  # noqa: E402
import execution  # noqa: E402
import providers  # noqa: E402
import runtime  # noqa: E402


def _config(**overrides) -> dict:
    config = json.loads(json.dumps(providers.DEFAULT_RUNTIME_CONFIG))
    config.update(overrides)
    return config


class ProhibitingMachine(unittest.TestCase):
    """A synthetic machine whose profile prohibits Ollama, injected into the authority."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name) / "infra"
        (self.root / "machines").mkdir(parents=True)
        (self.root / "machines" / "audit.yaml").write_text(json.dumps({
            "id": "audit", "platform": "linux",
            "provider_availability": {"ollama": False}}), encoding="utf-8")
        patcher = unittest.mock.patch.object(authority, "MACHINE_DIR", self.root / "machines")
        patcher.start()
        self.addCleanup(patcher.stop)
        env = unittest.mock.patch.dict(os.environ, {"LAI_MACHINE": "audit"})
        env.start()
        self.addCleanup(env.stop)
        self.launches: list[dict] = []
        # Never write runtime telemetry into the repository's own state files.
        self.spans = pathlib.Path(self.tmp.name) / "spans.jsonl"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def stub(self, provider):
        """A real provider class with its transport replaced: counts launches, no network."""
        def execute(request):
            self.launches.append({"provider": provider.name, "model": request.model})
            return providers.ProviderResponse(content="stub", provider=provider.name,
                                             model=request.model)
        provider.execute = execute
        return provider

    def policy(self) -> dict:
        return authority.machine_policy(dict(os.environ))


class ConfigFactoryTests(ProhibitingMachine):
    """A5/A2/A3 — the normal production config/factory path."""

    def test_A5_from_config_refuses_a_prohibited_provider_before_launch(self) -> None:
        agent = runtime.AgentRuntime.from_config(_config(), "ollama", span_output=self.spans)
        agent.provider = self.stub(agent.provider)
        with self.assertRaises(providers.ProviderConfigurationError) as caught:
            agent.run(runtime.AgentTask(prompt="hello"))
        self.assertIn("provider_prohibited_by_machine_policy", str(caught.exception))
        self.assertEqual([], self.launches)  # launch count = 0
        self.assertEqual(authority.PROHIBITED, self.policy()["prohibited"]["ollama"])

    def test_A2_a_config_without_prohibitions_cannot_permit_a_prohibited_provider(self) -> None:
        # The caller config has no notion of machine policy at all; the authority decides.
        config = _config()
        self.assertEqual("ollama", config["default_provider"])
        self.assertNotIn("prohibited_providers", config)
        agent = runtime.AgentRuntime.from_config(config, span_output=self.spans)
        agent.provider = self.stub(agent.provider)
        with self.assertRaises(providers.ProviderConfigurationError):
            agent.run(runtime.AgentTask(prompt="hello"))
        self.assertEqual([], self.launches)

    def test_A3_a_caller_preference_cannot_override_the_prohibition(self) -> None:
        agent = runtime.AgentRuntime.from_config(_config(default_provider="ollama"), "ollama",
                                                   span_output=self.spans)
        agent.provider = self.stub(agent.provider)
        with self.assertRaises(providers.ProviderConfigurationError):
            agent.run(runtime.AgentTask(prompt="hello"))
        self.assertEqual([], self.launches)
        self.assertFalse(authority.provider_permission("ollama")["allowed"])

    def test_permitted_provider_still_runs(self) -> None:
        # The gate must not break legitimate providers.
        agent = runtime.AgentRuntime.from_config(_config(), "fake", span_output=self.spans)
        agent.provider = self.stub(agent.provider)
        result = agent.run(runtime.AgentTask(prompt="hello"))
        self.assertEqual("succeeded", result.status)
        self.assertEqual(1, len(self.launches))
        self.assertTrue(authority.provider_permission("fake")["allowed"])

    def test_invalid_machine_policy_fails_closed_at_the_inference_boundary(self) -> None:
        (self.root / "machines" / "audit.yaml").write_text(
            json.dumps({"id": "audit", "platform": "linux",
                        "provider_availability": {"ollama": "false"}}), encoding="utf-8")
        agent = runtime.AgentRuntime.from_config(_config(), "fake", span_output=self.spans)
        agent.provider = self.stub(agent.provider)
        with self.assertRaises(providers.ProviderConfigurationError) as caught:
            agent.run(runtime.AgentTask(prompt="hello"))
        self.assertIn("machine_policy_invalid", str(caught.exception))
        self.assertEqual([], self.launches)


class DirectConstructionTests(ProhibitingMachine):
    """A2/A3/A10 — the execution boundary is safe whatever built the runtime."""

    def _stubbed(self, provider_name: str):
        provider = providers.build_provider(_config(), provider_name)
        return provider

    def test_A2_direct_construction_cannot_execute_a_prohibited_provider(self) -> None:
        agent = runtime.AgentRuntime(self.stub(self._stubbed("ollama")),
                                     span_output=self.spans, record_spans=False)
        with self.assertRaises(providers.ProviderConfigurationError) as caught:
            agent.run(runtime.AgentTask(prompt="hello"))
        self.assertIn("provider_prohibited_by_machine_policy", str(caught.exception))
        self.assertEqual([], self.launches)  # launch count = 0
        self.assertEqual(authority.PROHIBITED, self.policy()["prohibited"]["ollama"])

    def test_A3_direct_construction_with_a_permitted_provider_executes(self) -> None:
        agent = runtime.AgentRuntime(self.stub(self._stubbed("deepseek")),
                                     span_output=self.spans, record_spans=False)
        result = agent.run(runtime.AgentTask(prompt="hello"))
        self.assertEqual("succeeded", result.status)
        self.assertEqual(1, len(self.launches))
        self.assertEqual("deepseek", self.launches[0]["provider"])

    def test_no_boolean_escape_hatch_disables_the_gate(self) -> None:
        # A construction-path flag must not be able to switch hard policy off.
        agent = runtime.AgentRuntime(self.stub(self._stubbed("ollama")),
                                     span_output=self.spans, record_spans=False)
        self.assertFalse(hasattr(agent, "policy_bound"))
        self.assertEqual([], self.launches)

    def test_A8_invalid_policy_refuses_direct_construction_too(self) -> None:
        (self.root / "machines" / "audit.yaml").write_text(
            json.dumps({"id": "audit", "platform": "linux",
                        "provider_availability": {"ollama": "false"}}), encoding="utf-8")
        agent = runtime.AgentRuntime(self.stub(self._stubbed("deepseek")),
                                     span_output=self.spans, record_spans=False)
        with self.assertRaises(providers.ProviderConfigurationError) as caught:
            agent.run(runtime.AgentTask(prompt="hello"))
        self.assertIn("machine_policy_invalid", str(caught.exception))
        self.assertEqual([], self.launches)

    def test_A9_explicit_invalid_selector_refuses_direct_construction(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"LAI_MACHINE": "no-such-machine"}):
            agent = runtime.AgentRuntime(self.stub(self._stubbed("deepseek")),
                                         span_output=self.spans, record_spans=False)
            with self.assertRaises(providers.ProviderConfigurationError) as caught:
                agent.run(runtime.AgentTask(prompt="hello"))
        self.assertIn("machine_policy_invalid", str(caught.exception))
        self.assertEqual([], self.launches)

    def test_offline_provider_is_not_blocked_merely_for_being_a_provider(self) -> None:
        # A6: the repository's offline provider performs no real inference and is not a
        # prohibited provider, so deterministic execution keeps working; the machine
        # policy is still recorded truthfully.
        provider = providers.FakeModelProvider()
        agent = runtime.AgentRuntime(provider, span_output=self.spans, record_spans=False)
        result = agent.run(runtime.AgentTask(prompt="hello"))
        self.assertEqual("succeeded", result.status)
        self.assertEqual(1, len(provider.calls))  # offline call, no provider launch
        self.assertEqual(authority.PROHIBITED, self.policy()["prohibited"]["ollama"])


class EscalationFallbackTests(ProhibitingMachine):
    """A4 — fallback/escalation must not launch a prohibited provider."""

    def _config_with_ollama_fallback(self) -> dict:
        config = _config()
        config["escalation"].update({"enabled": True, "mode": "automatic", "hosted_allowed": True,
                                     "fallback_provider": "ollama"})
        return config

    def test_A4_from_config_refuses_a_prohibited_fallback(self) -> None:
        with self.assertRaises(providers.ProviderConfigurationError) as caught:
            escalation.EscalationController.from_config(self._config_with_ollama_fallback(),
                                                        span_output=self.spans)
        self.assertIn("provider_prohibited_by_machine_policy", str(caught.exception))
        self.assertEqual([], self.launches)  # teacher launch count = 0

    def test_A4_teacher_is_never_launched_for_a_prohibited_fallback(self) -> None:
        policy = escalation.EscalationPolicy.from_config(self._config_with_ollama_fallback())
        self.assertEqual("ollama", policy.fallback_provider)
        with self.assertRaises(providers.ProviderConfigurationError):
            escalation.EscalationController.from_config(self._config_with_ollama_fallback(),
                                                        limits={}, span_output=self.spans)
        self.assertEqual([], self.launches)

    def test_permitted_fallback_still_escalates(self) -> None:
        # The gate must not disable legitimate escalation.
        fallback = providers.FakeModelProvider()
        controller = escalation.EscalationController(
            escalation.EscalationPolicy.from_config(_config()), self.stub(fallback),
            span_output=self.spans, escalation_output=self.spans, record_spans=False,
            record_telemetry=False)
        self.assertEqual("fake", controller.fallback_provider.name)
        self.assertTrue(authority.provider_permission("fake")["allowed"])

    def test_A5_boundary_stops_the_teacher_even_without_a_factory_gate(self) -> None:
        # Synthetic fixture: a controller built directly with a prohibited provider, i.e.
        # as if a factory had omitted its early check. The execution boundary still stops
        # the teacher launch on its own.
        policy = escalation.EscalationPolicy.from_config(self._config_with_ollama_fallback())
        teacher = self.stub(providers.build_provider(_config(), "ollama"))
        controller = escalation.EscalationController(
            policy, teacher, span_output=self.spans, escalation_output=self.spans,
            record_spans=False, record_telemetry=False)
        session = runtime.TaskSession(run_id="r1", trace_id="t1", started=time.perf_counter(),
                                      budget=60.0)
        task = runtime.AgentTask(prompt="fix the parser")
        student = runtime.RuntimeResult(
            run_id="r1", trace_id="t1", span_id=None, status="failed", provider="fake",
            model="fake-model", content=None, duration_ms=1.0, attempts=1,
            error_category="model_reasoning_failure", error_code="reasoning_gap", retryable=False)
        with self.assertRaises(providers.ProviderConfigurationError) as caught:
            controller.maybe_escalate(session, task, student, task_class="bounded")
        self.assertIn("provider_prohibited_by_machine_policy", str(caught.exception))
        self.assertEqual([], self.launches)  # teacher launch count = 0


class ProductionBoundaryGuardTests(unittest.TestCase):
    """A6/A8 — the construction boundary and the real-adapter contract."""

    @staticmethod
    def _production_sources() -> list[pathlib.Path]:
        return [path for path in sorted((ROOT / "scripts").rglob("*"))
                if path.is_file() and path.suffix in ("", ".py")]

    def test_no_ungated_direct_agentruntime_construction_in_production(self) -> None:
        findings = []
        for path in self._production_sources():
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if "AgentRuntime(" in line:
                    findings.append((str(path.relative_to(ROOT)), number))
        files = sorted({name for name, _ in findings})
        # Safety no longer depends on call sites: AgentRuntime.run checks machine
        # permission at the execution boundary. This guard only flags a NEW production
        # construction site so its callers are re-reviewed deliberately.
        self.assertEqual(["scripts/common/escalation.py"], files,
                         f"new production AgentRuntime construction sites: {findings}")

    def test_every_factory_that_builds_the_teacher_provider_policy_checks_it(self) -> None:
        escalation_source = (ROOT / "scripts/common/escalation.py").read_text(encoding="utf-8")
        agent_run_source = (ROOT / "scripts/agent-run").read_text(encoding="utf-8")
        self.assertIn("require_machine_permission(policy.fallback_provider)", escalation_source)
        self.assertIn("require_machine_permission(policy.fallback_provider)", agent_run_source)

    def test_A8_every_production_backend_that_can_reach_a_provider_is_supported(self) -> None:
        for name in ("auto", "opencode", "codex"):
            adapter = execution.backend_for(name)
            self.assertEqual("SUPPORTED", adapter.mode, name)
            self.assertTrue(execution.launch_capable(adapter), name)  # hard policy applies
        fake = execution.backend_for("fake")
        self.assertEqual("SIMULATED", fake.mode)
        self.assertFalse(execution.launch_capable(fake))

    def test_A7_simulated_and_unsupported_adapters_cannot_reach_a_provider(self) -> None:
        import inspect
        source = inspect.getsource(execution.FakeBackend)
        for forbidden in ("subprocess", "urllib", "providers.", "requests", "socket"):
            self.assertNotIn(forbidden, source, f"FakeBackend must stay offline: {forbidden}")
        self.assertEqual("SIMULATED", execution.FakeBackend.mode)
        self.assertFalse(execution.launch_capable(execution.FakeBackend()))

        class Abstract(execution.Backend):
            name = "abstract"

        self.assertEqual("UNSUPPORTED", Abstract.mode)
        self.assertFalse(execution.launch_capable(Abstract()))

    def test_fake_inference_on_a_prohibiting_machine_is_not_a_policy_bypass(self) -> None:
        # A SIMULATED run performs no provider launch, so it is not blocked by policy;
        # its identity still records the authoritative policy.
        tmp = tempfile.TemporaryDirectory()
        try:
            root = pathlib.Path(tmp.name)
            machines = root / "machines"
            machines.mkdir()
            (machines / "audit.yaml").write_text(json.dumps({
                "id": "audit", "platform": "linux", "provider_availability": {"ollama": False}}),
                encoding="utf-8")
            repo = pathlib.Path(tmp.name) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
            (repo / "f.txt").write_text("x\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "i"], cwd=repo, check=True)
            plan = {"schema_version": 1, "valid": True, "intent": "READ_ONLY", "order": ["A1"],
                    "nodes": [{"task_id": "A1", "title": "t", "objective_summary": "t",
                               "task_family": "generic", "task_class": "NORMAL", "depends_on": [],
                               "conditional": False, "intent": "READ_ONLY", "executor": "AI",
                               "routing": {"tier": "BALANCED", "effort": "medium", "role": "student"},
                               "context": {"budget": "SMALL"}, "acceptance_criteria": ["ok"],
                               "risk_signals": [], "knowledge_refs": [], "context_refs": []}]}
            backend = execution.FakeBackend()
            with unittest.mock.patch.object(authority, "MACHINE_DIR", machines), \
                    unittest.mock.patch.dict(os.environ, {"LAI_MACHINE": "audit"}):
                report = execution.execute_plan(plan, backend=backend, state_dir=root / "state",
                                                repo_root=repo, confirm=True, allow_dirty=True,
                                                record_evidence=False)
                bound = authority.bind_constraints({})
            self.assertEqual("SUCCESS", report["nodes"]["A1"]["status"])
            self.assertEqual(1, len(backend.calls))  # simulated call, no provider launch
            self.assertEqual({"ollama": authority.PROHIBITED}, bound["prohibited_providers"])
        finally:
            tmp.cleanup()


class AgentRunEntrypointTests(unittest.TestCase):
    """A6 — scripts/agent-run, the real executable entrypoint."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.spans = self.base / "spans.jsonl"
        self.env = os.environ | {"LAI_MACHINE": "work-mac"}
        self.assertIn("ollama", authority.machine_policy(self.env)["prohibited"])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(ROOT / "scripts/agent-run"), *args],
                              cwd=ROOT, text=True, capture_output=True, env=self.env)

    def test_A6_explicit_prohibited_provider_is_refused(self) -> None:
        result = self._run("--prompt", "hello", "--provider", "ollama", "--confirm-run",
                           "--span-output", str(self.spans))
        self.assertEqual(2, result.returncode, result.stdout)
        self.assertIn("provider_prohibited_by_machine_policy", result.stderr)
        self.assertFalse(self.spans.exists())  # nothing was launched or recorded

    def test_A6_default_provider_is_refused_when_prohibited(self) -> None:
        result = self._run("--prompt", "hello", "--confirm-run")
        self.assertEqual(2, result.returncode, result.stdout)
        self.assertIn("provider_prohibited_by_machine_policy", result.stderr)

    def test_A6_escalation_fallback_is_refused_before_any_teacher_launch(self) -> None:
        config = self.base / "bad-fallback.yaml"
        payload = _config()
        payload["escalation"].update({"enabled": True, "mode": "automatic", "hosted_allowed": True,
                                      "fallback_provider": "ollama"})
        config.write_text(json.dumps(payload), encoding="utf-8")
        result = self._run("--prompt", "hello", "--provider", "fake", "--escalate", "--confirm-run",
                           "--config", str(config), "--span-output", str(self.spans))
        self.assertEqual(2, result.returncode, result.stdout)
        self.assertIn("provider_prohibited_by_machine_policy", result.stderr)

    def test_A6_tool_only_path_is_unaffected(self) -> None:
        # Zero-AI tool use must keep working: there is no provider launch to authorize.
        workspace = self.base / "ws"
        workspace.mkdir()
        (workspace / "a.txt").write_text("hello\n", encoding="utf-8")
        result = self._run("--tool", "file.read", "--workspace", str(workspace),
                           "--params", json.dumps({"path": "a.txt"}), "--confirm-run", "--json",
                           "--span-output", str(self.spans))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(json.loads(result.stdout)["success"])


if __name__ == "__main__":
    unittest.main()
