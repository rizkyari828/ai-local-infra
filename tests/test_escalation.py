from __future__ import annotations

import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import escalation  # noqa: E402
import infra  # noqa: E402
import learning  # noqa: E402
import providers  # noqa: E402
import runtime  # noqa: E402
import telemetry  # noqa: E402


class FakeResponse:
    def __init__(self, body: object, status: int = 200) -> None:
        self.status = status
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def opener_returning(body: object, status: int = 200):
    def opener(request, timeout=None):
        return FakeResponse(body, status)
    return opener


def opener_raising(exc: BaseException):
    def opener(request, timeout=None):
        raise exc
    return opener


def http_error(status: int, body: object) -> urllib.error.HTTPError:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    return urllib.error.HTTPError("https://api.deepseek.com", status, "error", {}, io.BytesIO(raw))


def deepseek_reply(content: str = "fixed", **usage: object) -> dict:
    return {
        "id": "chatcmpl-1",
        "model": "deepseek-flash",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": usage,
    }


class ReasoningFailureProvider(providers.ModelProvider):
    name = "fake"
    model = "qwen-local"

    def execute(self, request):
        raise providers.ProviderError("reasoning_gap", category="model_reasoning_failure", retryable=False)

    def health_check(self):
        return providers.ProviderHealth(provider=self.name, status="healthy", model=self.model)


class EnvironmentFailureProvider(providers.ModelProvider):
    name = "fake"
    model = "qwen-local"

    def execute(self, request):
        raise providers.ProviderError("no_toolchain", category="environment_failure", retryable=False)

    def health_check(self):
        return providers.ProviderHealth(provider=self.name, status="healthy", model=self.model)


def make_limits(**tool_overrides: object) -> dict:
    limits = json.loads(json.dumps(telemetry.DEFAULT_LIMITS))
    limits["tools"].update(tool_overrides)
    return limits


def failed_result(category: str, model: str = "qwen-local") -> runtime.RuntimeResult:
    return runtime.RuntimeResult(
        run_id=str(uuid.uuid4()), trace_id=str(uuid.uuid4()), span_id=str(uuid.uuid4()),
        status="failed", provider="fake", model=model, content=None, duration_ms=1.0,
        attempts=1, error_category=category, error_code="code", retryable=False,
    )


class DeepSeekProviderTests(unittest.TestCase):
    def test_registration_and_build(self) -> None:
        self.assertIsInstance(providers.build_provider(providers.DEFAULT_RUNTIME_CONFIG, "deepseek"),
                              providers.DeepSeekProvider)

    def test_default_fallback_is_flash_high(self) -> None:
        entry = providers.DEFAULT_RUNTIME_CONFIG["providers"]["deepseek"]
        self.assertEqual("deepseek-flash", entry["model"])
        self.assertEqual("high", entry["reasoning_profile"])
        loaded = providers.load_runtime_config()
        self.assertEqual("deepseek-flash", loaded["providers"]["deepseek"]["model"])
        provider = providers.DeepSeekProvider(api_key="k")
        self.assertEqual("deepseek-flash", provider.model)
        self.assertEqual("high", provider.reasoning_profile)
        self.assertEqual("deepseek-flash", provider.describe()["model"])

    def test_config_requires_base_url(self) -> None:
        config = json.loads(json.dumps(providers.DEFAULT_RUNTIME_CONFIG))
        del config["providers"]["deepseek"]["base_url"]
        with self.assertRaises(providers.ProviderConfigurationError):
            providers.validate_runtime_config(config)

    def test_missing_api_key_is_configuration_failure(self) -> None:
        provider = providers.DeepSeekProvider(model="deepseek-chat", api_key=None, api_key_env="LAI_ABSENT_KEY")
        with self.assertRaises(providers.ProviderError) as caught:
            provider.execute(providers.ProviderRequest(prompt="hi"))
        self.assertEqual("configuration_failure", caught.exception.category)
        self.assertEqual("missing_api_key", caught.exception.code)

    def test_api_key_from_environment_and_never_described(self) -> None:
        secret = "sk-unit-test-abcdefghijklmnop"
        with unittest.mock.patch.dict(os.environ, {"LAI_TEST_DEEPSEEK_KEY": secret}):
            provider = providers.DeepSeekProvider(model="deepseek-chat", api_key_env="LAI_TEST_DEEPSEEK_KEY")
        self.assertTrue(provider.has_api_key())
        described = json.dumps(provider.describe())
        self.assertNotIn(secret, described)
        self.assertTrue(provider.describe()["has_api_key"])

    def test_request_and_response_normalization(self) -> None:
        captured: dict = {}

        def opener(request, timeout=None):
            captured["url"] = request.full_url
            captured["auth"] = request.headers.get("Authorization")
            captured["method"] = request.get_method()
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse(deepseek_reply("answer", prompt_tokens=11, completion_tokens=22,
                                               prompt_cache_hit_tokens=4,
                                               completion_tokens_details={"reasoning_tokens": 7}))

        provider = providers.DeepSeekProvider(model="deepseek-flash", api_key="unit-key", opener=opener)
        response = provider.execute(providers.ProviderRequest(prompt="do it", options={"temperature": 0.2}))
        self.assertEqual("answer", response.content)
        self.assertEqual("deepseek-flash", response.model)
        self.assertEqual(11, response.input_tokens)
        self.assertEqual(22, response.output_tokens)
        self.assertEqual(4, response.cached_input_tokens)
        self.assertEqual(7, response.reasoning_tokens)
        self.assertEqual("stop", response.finish_reason)
        self.assertEqual("POST", captured["method"])
        self.assertTrue(captured["url"].endswith("/chat/completions"))
        self.assertEqual("Bearer unit-key", captured["auth"])
        self.assertEqual("do it", captured["body"]["messages"][0]["content"])
        self.assertEqual("deepseek-flash", captured["body"]["model"])
        self.assertFalse(captured["body"]["stream"])
        self.assertEqual(0.2, captured["body"]["temperature"])

    def test_token_metadata_missing_is_null(self) -> None:
        provider = providers.DeepSeekProvider(model="deepseek-chat", api_key="k",
                                              opener=opener_returning(deepseek_reply("ok")))
        response = provider.execute(providers.ProviderRequest(prompt="hi"))
        self.assertIsNone(response.input_tokens)
        self.assertIsNone(response.output_tokens)
        self.assertIsNone(response.cached_input_tokens)
        self.assertIsNone(response.reasoning_tokens)

    def test_invalid_response_rejected(self) -> None:
        provider = providers.DeepSeekProvider(model="deepseek-chat", api_key="k",
                                              opener=opener_returning({"choices": []}))
        with self.assertRaises(providers.ProviderResponseError):
            provider.execute(providers.ProviderRequest(prompt="hi"))

    def test_timeout_mapping(self) -> None:
        provider = providers.DeepSeekProvider(model="deepseek-chat", api_key="k",
                                              opener=opener_raising(TimeoutError()))
        with self.assertRaises(providers.ProviderError) as caught:
            provider.execute(providers.ProviderRequest(prompt="hi"))
        self.assertEqual("provider_timeout", caught.exception.category)

    def test_auth_error_mapping(self) -> None:
        provider = providers.DeepSeekProvider(model="deepseek-chat", api_key="k",
                                              opener=opener_raising(http_error(401, {"error": "bad key"})))
        with self.assertRaises(providers.ProviderError) as caught:
            provider.execute(providers.ProviderRequest(prompt="hi"))
        self.assertEqual("configuration_failure", caught.exception.category)
        self.assertEqual("auth_error", caught.exception.code)

    def test_server_error_mapping_is_retryable(self) -> None:
        provider = providers.DeepSeekProvider(model="deepseek-chat", api_key="k",
                                              opener=opener_raising(http_error(503, {"error": "down"})))
        with self.assertRaises(providers.ProviderError) as caught:
            provider.execute(providers.ProviderRequest(prompt="hi"))
        self.assertEqual("provider_failure", caught.exception.category)
        self.assertTrue(caught.exception.retryable)

    def test_connection_error_mapping(self) -> None:
        provider = providers.DeepSeekProvider(model="deepseek-chat", api_key="k",
                                              opener=opener_raising(urllib.error.URLError("no dns")))
        with self.assertRaises(providers.ProviderError) as caught:
            provider.execute(providers.ProviderRequest(prompt="hi"))
        self.assertEqual("provider_failure", caught.exception.category)

    def test_cancellation(self) -> None:
        cancel = threading.Event()
        cancel.set()
        provider = providers.DeepSeekProvider(model="deepseek-chat", api_key="k")
        with self.assertRaises(providers.ProviderCancelled):
            provider.execute(providers.ProviderRequest(prompt="hi", cancel=cancel))

    def test_unsupported_option_rejected(self) -> None:
        with self.assertRaises(providers.ProviderConfigurationError):
            providers.DeepSeekProvider(model="deepseek-chat", api_key="k", options={"num_ctx": 1024})

    def test_health_missing_key(self) -> None:
        provider = providers.DeepSeekProvider(model="deepseek-chat", api_key=None, api_key_env="LAI_ABSENT_KEY")
        self.assertEqual("misconfigured", provider.health_check().status)

    def test_health_healthy_and_unreachable(self) -> None:
        healthy = providers.DeepSeekProvider(model="deepseek-chat", api_key="k",
                                             opener=opener_returning({"data": []}))
        self.assertEqual("healthy", healthy.health_check().status)
        unreachable = providers.DeepSeekProvider(model="deepseek-chat", api_key="k",
                                                 opener=opener_raising(urllib.error.URLError("no")))
        self.assertEqual("unreachable", unreachable.health_check().status)


class EscalationPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = runtime.TaskSession(run_id=str(uuid.uuid4()), trace_id=str(uuid.uuid4()),
                                           started=0.0, budget=100.0)

    def policy(self, **overrides: object) -> escalation.EscalationPolicy:
        base = dict(enabled=True, mode="automatic", hosted_allowed=True,
                    fallback_provider="deepseek", eligible_categories=escalation.DEFAULT_ELIGIBLE_CATEGORIES,
                    max_depth=1)
        base.update(overrides)
        return escalation.EscalationPolicy(**base)

    def test_defaults_are_conservative(self) -> None:
        policy = escalation.EscalationPolicy.from_config(providers.DEFAULT_RUNTIME_CONFIG)
        self.assertFalse(policy.enabled)
        self.assertFalse(policy.hosted_allowed)
        self.assertEqual("manual", policy.mode)

    def test_success_skips(self) -> None:
        result = runtime.RuntimeResult(str(uuid.uuid4()), str(uuid.uuid4()), None, "succeeded", "fake",
                                       "qwen", "ok", 1.0, 1, None, None, False)
        decision = self.policy().evaluate(result, self.session)
        self.assertEqual("student_succeeded", decision.reason)
        self.assertFalse(decision.eligible)

    def test_disabled_skips(self) -> None:
        decision = self.policy(enabled=False).evaluate(failed_result("model_reasoning_failure"), self.session)
        self.assertEqual("escalation_disabled", decision.reason)
        self.assertFalse(decision.action == "escalate")

    def test_non_eligible_category_skips(self) -> None:
        for category in ("environment_failure", "configuration_failure", "tool_failure", "tool_timeout",
                         "provider_timeout", "provider_failure", "user_cancelled", "resource_limit"):
            decision = self.policy().evaluate(failed_result(category), self.session)
            self.assertEqual("category_not_eligible", decision.reason, category)
            self.assertFalse(decision.eligible)

    def test_hosted_blocked(self) -> None:
        decision = self.policy(hosted_allowed=False).evaluate(failed_result("model_reasoning_failure"), self.session)
        self.assertEqual("blocked", decision.action)
        self.assertEqual("hosted_blocked", decision.reason)

    def test_manual_recommendation(self) -> None:
        decision = self.policy(mode="manual").evaluate(failed_result("validation_failure"), self.session)
        self.assertEqual("recommend", decision.action)
        self.assertEqual("manual_mode", decision.reason)

    def test_automatic_escalation(self) -> None:
        decision = self.policy().evaluate(failed_result("model_reasoning_failure"), self.session)
        self.assertEqual("escalate", decision.action)
        self.assertTrue(decision.eligible)

    def test_depth_exceeded(self) -> None:
        session = self.session.child()
        decision = self.policy().evaluate(failed_result("model_reasoning_failure"), session)
        self.assertEqual("depth_exceeded", decision.reason)

    def test_cancelled_session_skips(self) -> None:
        cancel = threading.Event()
        cancel.set()
        session = runtime.TaskSession(run_id=str(uuid.uuid4()), trace_id=str(uuid.uuid4()),
                                      started=0.0, budget=100.0, cancel=cancel)
        decision = self.policy().evaluate(failed_result("model_reasoning_failure"), session)
        self.assertEqual("cancelled", decision.reason)


class EscalationControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.temp.name)
        self.spans = base / "spans.jsonl"
        self.escalations = base / "escalations.jsonl"
        self.limits = make_limits()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def policy(self, **overrides: object) -> escalation.EscalationPolicy:
        base = dict(enabled=True, mode="automatic", hosted_allowed=True,
                    fallback_provider="deepseek", eligible_categories=escalation.DEFAULT_ELIGIBLE_CATEGORIES,
                    max_depth=1)
        base.update(overrides)
        return escalation.EscalationPolicy(**base)

    def controller(self, fallback: providers.ModelProvider, **overrides: object) -> escalation.EscalationController:
        policy = overrides.pop("policy", self.policy())
        return escalation.EscalationController(
            policy, fallback, limits=overrides.pop("limits", self.limits), span_output=self.spans,
            escalation_output=self.escalations, **overrides)

    def run_student(self, provider: providers.ModelProvider, prompt: str = "solve the task",
                    **task_fields: object) -> tuple:
        agent = runtime.AgentRuntime(provider, limits=self.limits, span_output=self.spans)
        task = runtime.AgentTask(prompt=prompt, **task_fields)
        session = agent.open_session(task)
        return session, agent.run(task, session=session)

    def test_automatic_escalation_success_and_trace_continuity(self) -> None:
        fallback = providers.FakeModelProvider(content="FIXED ANSWER")
        session, student = self.run_student(ReasoningFailureProvider())
        self.assertEqual("model_reasoning_failure", student.error_category)
        outcome = self.controller(fallback).maybe_escalate(session, runtime.AgentTask(prompt="solve the task"),
                                                           student, task_class="bounded")
        self.assertTrue(outcome.escalated)
        self.assertEqual("succeeded", outcome.teacher_result.status)
        self.assertEqual("FIXED ANSWER", outcome.teacher_result.content)
        self.assertEqual(student.trace_id, outcome.trace_id)
        self.assertEqual(student.trace_id, outcome.teacher_result.trace_id)
        self.assertNotEqual(student.run_id, outcome.teacher_run_id)
        self.assertEqual(1, len(fallback.calls))
        rows = learning.load_jsonl(self.escalations)
        learning.validate_escalation_record(rows[0])
        self.assertEqual(outcome.teacher_run_id, rows[0]["run_id"])
        self.assertEqual(student.run_id, rows[0]["parent_run_id"])
        self.assertEqual(student.trace_id, rows[0]["trace_id"])
        spans = learning.load_jsonl(self.spans)
        inference = [span for span in spans if span["stage"] == "inference"]
        self.assertEqual(2, len(inference))
        teacher_span = [span for span in inference if span["role"] == "teacher-cheap"][0]
        self.assertEqual(student.span_id, teacher_span["parent_span_id"])

    def test_escalation_to_failing_teacher(self) -> None:
        fallback = providers.FakeModelProvider(scenario="provider_error")
        session, student = self.run_student(ReasoningFailureProvider())
        outcome = self.controller(fallback).maybe_escalate(session, runtime.AgentTask(prompt="solve"), student)
        self.assertTrue(outcome.escalated)
        self.assertEqual("failed", outcome.teacher_result.status)
        self.assertEqual("provider_failure", outcome.teacher_result.error_category)
        self.assertEqual(1, len(learning.load_jsonl(self.escalations)))

    def test_non_eligible_failure_does_not_escalate(self) -> None:
        fallback = providers.FakeModelProvider()
        session, student = self.run_student(EnvironmentFailureProvider())
        outcome = self.controller(fallback).maybe_escalate(session, runtime.AgentTask(prompt="solve"), student)
        self.assertFalse(outcome.escalated)
        self.assertEqual("category_not_eligible", outcome.reason)
        self.assertEqual(0, len(fallback.calls))
        self.assertFalse(self.escalations.exists())

    def test_hosted_blocked_returns_structured_outcome(self) -> None:
        fallback = providers.FakeModelProvider()
        policy = self.policy(hosted_allowed=False)
        session, student = self.run_student(ReasoningFailureProvider())
        outcome = self.controller(fallback, policy=policy).maybe_escalate(
            session, runtime.AgentTask(prompt="solve"), student)
        self.assertEqual("blocked", outcome.action)
        self.assertEqual("hosted_blocked", outcome.reason)
        self.assertEqual(0, len(fallback.calls))

    def test_manual_mode_recommends_without_calling(self) -> None:
        fallback = providers.FakeModelProvider()
        policy = self.policy(mode="manual")
        session, student = self.run_student(ReasoningFailureProvider())
        outcome = self.controller(fallback, policy=policy).maybe_escalate(
            session, runtime.AgentTask(prompt="solve"), student)
        self.assertEqual("recommend", outcome.action)
        self.assertFalse(outcome.escalated)
        self.assertEqual(0, len(fallback.calls))

    def test_disabled_never_calls(self) -> None:
        fallback = providers.FakeModelProvider()
        policy = self.policy(enabled=False)
        session, student = self.run_student(ReasoningFailureProvider())
        outcome = self.controller(fallback, policy=policy).maybe_escalate(
            session, runtime.AgentTask(prompt="solve"), student)
        self.assertEqual("escalation_disabled", outcome.reason)
        self.assertEqual(0, len(fallback.calls))

    def test_depth_protection(self) -> None:
        fallback = providers.FakeModelProvider()
        session = runtime.TaskSession(run_id=str(uuid.uuid4()), trace_id=str(uuid.uuid4()),
                                      started=0.0, budget=100.0, escalation_depth=1)
        outcome = self.controller(fallback).maybe_escalate(
            session, runtime.AgentTask(prompt="solve"), failed_result("model_reasoning_failure"))
        self.assertEqual("depth_exceeded", outcome.reason)
        self.assertEqual(0, len(fallback.calls))

    def test_cancellation_prevents_escalation(self) -> None:
        fallback = providers.FakeModelProvider()
        cancel = threading.Event()
        cancel.set()
        session = runtime.TaskSession(run_id=str(uuid.uuid4()), trace_id=str(uuid.uuid4()),
                                      started=0.0, budget=100.0, cancel=cancel)
        outcome = self.controller(fallback).maybe_escalate(
            session, runtime.AgentTask(prompt="solve"), failed_result("model_reasoning_failure"))
        self.assertEqual("cancelled", outcome.reason)
        self.assertEqual(0, len(fallback.calls))

    def test_budget_exhausted(self) -> None:
        fallback = providers.FakeModelProvider()
        session = runtime.TaskSession(run_id=str(uuid.uuid4()), trace_id=str(uuid.uuid4()),
                                      started=time.perf_counter() - 50.0, budget=1.0)
        outcome = self.controller(fallback).maybe_escalate(
            session, runtime.AgentTask(prompt="solve"), failed_result("model_reasoning_failure"))
        self.assertEqual("budget_exhausted", outcome.reason)
        self.assertEqual(0, len(fallback.calls))

    def test_remaining_budget_bounds_teacher_timeout(self) -> None:
        fallback = providers.FakeModelProvider(delay_seconds=10.0)
        session = runtime.TaskSession(run_id=str(uuid.uuid4()), trace_id=str(uuid.uuid4()),
                                      started=time.perf_counter(), budget=0.3)
        started = time.perf_counter()
        outcome = self.controller(fallback).maybe_escalate(
            session, runtime.AgentTask(prompt="solve"), failed_result("model_reasoning_failure"))
        elapsed = time.perf_counter() - started
        self.assertTrue(outcome.escalated)
        self.assertEqual("provider_timeout", outcome.teacher_result.error_category)
        self.assertLess(elapsed, 3.0)

    def test_hosted_secret_protection_blocks_escalation(self) -> None:
        fallback = providers.FakeModelProvider()
        secret_prompt = "deploy with api_key=SuperSecret12345"
        session, student = self.run_student(ReasoningFailureProvider(), prompt=secret_prompt)
        outcome = self.controller(fallback).maybe_escalate(
            session, runtime.AgentTask(prompt=secret_prompt), student)
        self.assertEqual("blocked", outcome.action)
        self.assertEqual("hosted_secret_detected", outcome.reason)
        self.assertEqual(0, len(fallback.calls))
        self.assertFalse(self.escalations.exists())

    def test_escalation_telemetry_is_review_gated(self) -> None:
        fallback = providers.FakeModelProvider(content="FIXED")
        session, student = self.run_student(ReasoningFailureProvider())
        outcome = self.controller(fallback).maybe_escalate(session, runtime.AgentTask(prompt="solve"), student)
        row = learning.load_jsonl(self.escalations)[0]
        self.assertEqual("unreviewed", row["human_review_outcome"])
        self.assertFalse(row["lesson_candidate"])
        self.assertFalse(row["eval_candidate"])
        self.assertFalse(row["training_candidate"])
        self.assertEqual("unknown", row["failure_category"])
        self.assertEqual(fallback.name, row["teacher_provider"])
        self.assertEqual("unknown", row["teacher_outcome"])

    def test_environment_failure_never_creates_learning_attribution(self) -> None:
        fallback = providers.FakeModelProvider()
        session, student = self.run_student(EnvironmentFailureProvider())
        self.controller(fallback).maybe_escalate(session, runtime.AgentTask(prompt="solve"), student)
        self.assertFalse(self.escalations.exists())

    def test_telemetry_can_be_disabled(self) -> None:
        fallback = providers.FakeModelProvider()
        controller = escalation.EscalationController(self.policy(), fallback, limits=self.limits,
                                                     span_output=self.spans, escalation_output=self.escalations,
                                                     record_telemetry=False)
        session = runtime.TaskSession(run_id=str(uuid.uuid4()), trace_id=str(uuid.uuid4()),
                                      started=time.perf_counter(), budget=100.0)
        outcome = controller.maybe_escalate(session, runtime.AgentTask(prompt="solve"),
                                            failed_result("model_reasoning_failure"))
        self.assertTrue(outcome.escalated)
        self.assertFalse(outcome.telemetry_written)
        self.assertFalse(self.escalations.exists())

    def test_from_config_uses_fallback_provider(self) -> None:
        config = json.loads(json.dumps(providers.DEFAULT_RUNTIME_CONFIG))
        config["escalation"].update({"enabled": True, "mode": "automatic", "hosted_allowed": True})
        controller = escalation.EscalationController.from_config(
            config, fallback_provider=providers.FakeModelProvider(), span_output=self.spans,
            escalation_output=self.escalations)
        self.assertEqual("deepseek", controller.policy.fallback_provider)
        self.assertEqual("fake", controller.fallback_provider.name)


class EscalationConfigTests(unittest.TestCase):
    def _bad(self, **escalation_values: object) -> dict:
        config = json.loads(json.dumps(providers.DEFAULT_RUNTIME_CONFIG))
        config["escalation"].update(escalation_values)
        return config

    def test_fallback_provider_must_be_configured(self) -> None:
        with self.assertRaises(providers.ProviderConfigurationError):
            providers.validate_runtime_config(self._bad(fallback_provider="missing"))

    def test_max_depth_must_be_one(self) -> None:
        with self.assertRaises(providers.ProviderConfigurationError):
            providers.validate_runtime_config(self._bad(max_depth=2))

    def test_invalid_mode_rejected(self) -> None:
        with self.assertRaises(providers.ProviderConfigurationError):
            providers.validate_runtime_config(self._bad(mode="sometimes"))

    def test_invalid_category_rejected(self) -> None:
        with self.assertRaises(providers.ProviderConfigurationError):
            providers.validate_runtime_config(self._bad(eligible_categories=["vibes"]))

    def test_unknown_field_rejected(self) -> None:
        with self.assertRaises(providers.ProviderConfigurationError):
            providers.validate_runtime_config(self._bad(autostart=True))


class EscalationCliTests(unittest.TestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(ROOT / "scripts/agent-run"), *args],
                              cwd=ROOT, text=True, capture_output=True)

    def test_cli_escalation_wiring_on_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = pathlib.Path(temp)
            result = self._run("--provider", "fake", "--prompt", "hello", "--confirm-run",
                               "--escalate", "--escalation-mode", "automatic", "--hosted",
                               "--json", "--span-output", str(base / "spans.jsonl"),
                               "--escalation-output", str(base / "escalations.jsonl"))
            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual("succeeded", payload["result"]["status"])
            self.assertEqual("student_succeeded", payload["escalation"]["reason"])
            self.assertFalse(payload["escalation"]["escalated"])

    def test_cli_escalation_rejected_with_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = self._run("--tool", "file.read", "--workspace", temp,
                               "--confirm-run", "--escalate")
            self.assertEqual(2, result.returncode)


@unittest.skipUnless(os.environ.get("LAI_DEEPSEEK_INTEGRATION") == "1",
                     "set LAI_DEEPSEEK_INTEGRATION=1 to run the optional live DeepSeek test")
class OptionalDeepSeekIntegrationTests(unittest.TestCase):
    """Opt-in smoke test; tiny prompt, minimal tokens, skipped by default."""

    def test_tiny_completion(self) -> None:
        provider = providers.DeepSeekProvider(model=os.environ.get("LAI_DEEPSEEK_MODEL", "deepseek-flash"))
        if not provider.has_api_key():
            self.skipTest("DEEPSEEK_API_KEY is not set")
        response = provider.execute(providers.ProviderRequest(prompt="Reply with the single word READY.",
                                                              timeout_seconds=30,
                                                              options={"max_tokens": 4}))
        self.assertTrue(response.content.strip())


if __name__ == "__main__":
    unittest.main()
