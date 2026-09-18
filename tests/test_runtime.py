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
import urllib.error
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
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
    return urllib.error.HTTPError("http://localhost:11434", status, "error", {}, io.BytesIO(raw))


class FlakyProvider(providers.ModelProvider):
    name = "fake"

    def __init__(self, failures: int = 1, model: str = "fake-model") -> None:
        self.failures = failures
        self.calls = 0
        self._model = model

    @property
    def model(self):
        return self._model

    def execute(self, request):
        self.calls += 1
        if self.calls <= self.failures:
            raise providers.ProviderUnavailable("connection_error", "flaky failure")
        return providers.ProviderResponse(content="recovered", provider=self.name, model=self._model,
                                          input_tokens=3, output_tokens=2)

    def health_check(self):
        return providers.ProviderHealth(provider=self.name, status="healthy", model=self._model)


class BrokenProvider(providers.ModelProvider):
    name = "fake"

    def execute(self, request):
        raise ValueError("unexpected internal bug")

    def health_check(self):
        return providers.ProviderHealth(provider=self.name, status="healthy")


class StepClock:
    """Deterministic clock that advances a fixed step on every read."""

    def __init__(self, step: float = 1.0) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


def read_spans(path: pathlib.Path) -> list:
    return learning.load_jsonl(path)


class ProviderConfigTests(unittest.TestCase):
    def test_repo_runtime_config_loads(self) -> None:
        config = providers.load_runtime_config()
        self.assertEqual("ollama", config["default_provider"])
        self.assertIn("ollama", config["providers"])

    def test_missing_config_falls_back_to_defaults(self) -> None:
        config = providers.load_runtime_config(pathlib.Path("/nonexistent/runtime.yaml"))
        self.assertEqual(providers.DEFAULT_RUNTIME_CONFIG, config)

    def test_build_fake_and_ollama(self) -> None:
        config = providers.load_runtime_config()
        self.assertIsInstance(providers.build_provider(config, "fake"), providers.FakeModelProvider)
        self.assertIsInstance(providers.build_provider(config, "ollama"), providers.OllamaProvider)
        self.assertIsInstance(providers.build_provider(config, "deepseek"), providers.DeepSeekProvider)

    def test_unknown_provider_rejected(self) -> None:
        config = providers.load_runtime_config()
        with self.assertRaises(providers.ProviderError):
            providers.build_provider(config, "codex")

    def test_ollama_requires_base_url(self) -> None:
        bad = json.loads(json.dumps(providers.DEFAULT_RUNTIME_CONFIG))
        del bad["providers"]["ollama"]["base_url"]
        with self.assertRaises(providers.ProviderConfigurationError):
            providers.validate_runtime_config(bad)

    def test_default_provider_must_be_configured(self) -> None:
        bad = json.loads(json.dumps(providers.DEFAULT_RUNTIME_CONFIG))
        bad["default_provider"] = "nope"
        with self.assertRaises(providers.ProviderConfigurationError):
            providers.validate_runtime_config(bad)

    def test_config_rejects_secret_like_value(self) -> None:
        bad = json.loads(json.dumps(providers.DEFAULT_RUNTIME_CONFIG))
        bad["providers"]["ollama"]["model"] = "sk-abcdefghijklmnop"
        with self.assertRaises(providers.ProviderConfigurationError):
            providers.validate_runtime_config(bad)

    def test_provider_metadata_has_no_credentials(self) -> None:
        provider = providers.OllamaProvider(base_url="http://localhost:11434", model="qwen")
        described = provider.describe()
        self.assertEqual("ollama", described["provider"])
        self.assertNotIn("@", described["base_url"])


class BaseUrlTests(unittest.TestCase):
    def test_valid_and_normalized(self) -> None:
        self.assertEqual("http://localhost:11434", providers.validate_base_url("http://localhost:11434/"))

    def test_invalid_urls_rejected(self) -> None:
        for value in ("localhost:11434", "ftp://host", "http://", "http://user:pass@host:11434"):
            with self.assertRaises(providers.ProviderConfigurationError):
                providers.validate_base_url(value)


class OllamaProviderTests(unittest.TestCase):
    def test_execute_parses_token_metadata(self) -> None:
        provider = providers.OllamaProvider(
            model="qwen", opener=opener_returning(
                {"response": "hello", "prompt_eval_count": 10, "eval_count": 5, "done_reason": "stop"}
            )
        )
        response = provider.execute(providers.ProviderRequest(prompt="hi"))
        self.assertEqual("hello", response.content)
        self.assertEqual(10, response.input_tokens)
        self.assertEqual(5, response.output_tokens)
        self.assertEqual("stop", response.finish_reason)
        self.assertEqual("ollama", response.provider)

    def test_execute_without_token_metadata_is_null(self) -> None:
        provider = providers.OllamaProvider(model="qwen", opener=opener_returning({"response": "hi"}))
        response = provider.execute(providers.ProviderRequest(prompt="hi"))
        self.assertIsNone(response.input_tokens)
        self.assertIsNone(response.output_tokens)
        self.assertIsNone(response.reasoning_tokens)

    def test_execute_derives_context_utilization_from_configured_window(self) -> None:
        provider = providers.OllamaProvider(
            model="qwen", opener=opener_returning({"response": "hi", "prompt_eval_count": 50})
        )
        response = provider.execute(providers.ProviderRequest(prompt="hi", options={"num_ctx": 100}))
        self.assertEqual(100, response.context_size)
        self.assertEqual(0.5, response.context_utilization)

    def test_execute_rejects_empty_response(self) -> None:
        provider = providers.OllamaProvider(model="qwen", opener=opener_returning({"done": True}))
        with self.assertRaises(providers.ProviderResponseError):
            provider.execute(providers.ProviderRequest(prompt="hi"))

    def test_execute_rejects_empty_prompt(self) -> None:
        provider = providers.OllamaProvider(model="qwen", opener=opener_returning({"response": "hi"}))
        with self.assertRaises(providers.ProviderValidationError):
            provider.execute(providers.ProviderRequest(prompt="   "))

    def test_execute_missing_model_is_configuration_failure(self) -> None:
        provider = providers.OllamaProvider(opener=opener_returning({"response": "hi"}))
        with self.assertRaises(providers.ProviderError) as caught:
            provider.execute(providers.ProviderRequest(prompt="hi"))
        self.assertEqual("configuration_failure", caught.exception.category)

    def test_execute_maps_model_not_found(self) -> None:
        provider = providers.OllamaProvider(
            model="qwen", opener=opener_raising(http_error(404, {"error": "model 'qwen' not found, try pulling it"}))
        )
        with self.assertRaises(providers.ProviderError) as caught:
            provider.execute(providers.ProviderRequest(prompt="hi"))
        self.assertEqual("configuration_failure", caught.exception.category)
        self.assertEqual("model_missing", caught.exception.code)

    def test_execute_maps_server_error_as_retryable(self) -> None:
        provider = providers.OllamaProvider(
            model="qwen", opener=opener_raising(http_error(500, {"error": "boom"}))
        )
        with self.assertRaises(providers.ProviderError) as caught:
            provider.execute(providers.ProviderRequest(prompt="hi"))
        self.assertEqual("provider_failure", caught.exception.category)
        self.assertTrue(caught.exception.retryable)

    def test_execute_maps_connection_error(self) -> None:
        provider = providers.OllamaProvider(model="qwen", opener=opener_raising(urllib.error.URLError("refused")))
        with self.assertRaises(providers.ProviderError) as caught:
            provider.execute(providers.ProviderRequest(prompt="hi"))
        self.assertEqual("provider_failure", caught.exception.category)
        self.assertNotEqual("model_reasoning_failure", caught.exception.category)

    def test_execute_maps_timeout(self) -> None:
        provider = providers.OllamaProvider(model="qwen", opener=opener_raising(TimeoutError()))
        with self.assertRaises(providers.ProviderError) as caught:
            provider.execute(providers.ProviderRequest(prompt="hi"))
        self.assertEqual("provider_timeout", caught.exception.category)
        self.assertTrue(caught.exception.retryable)

    def test_generation_options_reject_secret_like_value(self) -> None:
        with self.assertRaises(providers.ProviderConfigurationError):
            providers.OllamaProvider(model="qwen", options={"note": "api_key=abcdefgh12345"})

    def test_health_healthy(self) -> None:
        tags = {"models": [{"name": "qwen3.6:27b-coding"}, {"name": "other:latest"}]}
        provider = providers.OllamaProvider(
            model="qwen3.6:27b-coding", base_url="http://localhost:11434", opener=opener_returning(tags)
        )
        health = provider.health_check()
        self.assertEqual("healthy", health.status)
        self.assertEqual("ollama", health.provider)

    def test_health_model_missing(self) -> None:
        provider = providers.OllamaProvider(
            model="qwen3.6:27b-coding", opener=opener_returning({"models": [{"name": "different:latest"}]})
        )
        self.assertEqual("model_missing", provider.health_check().status)

    def test_health_unreachable(self) -> None:
        provider = providers.OllamaProvider(model="qwen", opener=opener_raising(urllib.error.URLError("refused")))
        self.assertEqual("unreachable", provider.health_check().status)


class FakeProviderTests(unittest.TestCase):
    def test_success_metadata(self) -> None:
        provider = providers.FakeModelProvider(scenario="success")
        response = provider.execute(providers.ProviderRequest(prompt="hi"))
        self.assertEqual("FAKE RESPONSE", response.content)
        self.assertEqual(12, response.input_tokens)
        self.assertEqual(7, response.output_tokens)

    def test_no_token_metadata(self) -> None:
        provider = providers.FakeModelProvider(scenario="no_token_metadata")
        response = provider.execute(providers.ProviderRequest(prompt="hi"))
        self.assertIsNone(response.input_tokens)
        self.assertIsNone(response.output_tokens)

    def test_unavailable_scenario(self) -> None:
        provider = providers.FakeModelProvider(scenario="unavailable")
        with self.assertRaises(providers.ProviderUnavailable):
            provider.execute(providers.ProviderRequest(prompt="hi"))

    def test_invalid_response_scenario(self) -> None:
        provider = providers.FakeModelProvider(scenario="invalid_response")
        with self.assertRaises(providers.ProviderResponseError):
            provider.execute(providers.ProviderRequest(prompt="hi"))

    def test_model_missing_scenario(self) -> None:
        provider = providers.FakeModelProvider(scenario="model_missing")
        with self.assertRaises(providers.ProviderModelMissing):
            provider.execute(providers.ProviderRequest(prompt="hi"))

    def test_cancelled_scenario(self) -> None:
        provider = providers.FakeModelProvider(scenario="cancelled")
        with self.assertRaises(providers.ProviderCancelled):
            provider.execute(providers.ProviderRequest(prompt="hi"))

    def test_unknown_scenario_rejected(self) -> None:
        with self.assertRaises(providers.ProviderConfigurationError):
            providers.FakeModelProvider(scenario="nope")


class RuntimeSuccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.spans = pathlib.Path(self.temp.name) / "spans.jsonl"
        self.runtime = runtime.AgentRuntime(providers.FakeModelProvider(), span_output=self.spans)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_result_normalization(self) -> None:
        result = self.runtime.execute("do the thing")
        self.assertEqual("succeeded", result.status)
        self.assertEqual("fake", result.provider)
        self.assertEqual("FAKE RESPONSE", result.content)
        self.assertEqual(1, result.attempts)
        self.assertIsNone(result.error_category)
        self.assertEqual(result.run_id, result.trace_id)
        self.assertTrue(result.span_written)
        uuid.UUID(result.trace_id)
        uuid.UUID(result.span_id)

    def test_explicit_trace_id_is_preserved(self) -> None:
        trace_id = str(uuid.uuid4())
        result = self.runtime.execute("do the thing", trace_id=trace_id)
        self.assertEqual(trace_id, result.trace_id)
        self.assertEqual(trace_id, read_spans(self.spans)[0]["trace_id"])

    def test_token_metadata_present(self) -> None:
        result = self.runtime.execute("do the thing")
        self.assertEqual(12, result.input_tokens)
        self.assertEqual(7, result.output_tokens)
        span = read_spans(self.spans)[0]
        self.assertEqual(12, span["input_tokens"])

    def test_token_metadata_absent_is_null(self) -> None:
        self.runtime.provider = providers.FakeModelProvider(scenario="no_token_metadata")
        result = self.runtime.execute("do the thing")
        self.assertIsNone(result.input_tokens)
        self.assertIsNone(result.output_tokens)
        span = read_spans(self.spans)[0]
        self.assertIsNone(span["input_tokens"])


class AutomaticTelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.spans = pathlib.Path(self.temp.name) / "spans.jsonl"
        self.runtime = runtime.AgentRuntime(providers.FakeModelProvider(), span_output=self.spans)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_span_is_emitted_and_valid(self) -> None:
        self.runtime.execute("do the thing")
        spans = read_spans(self.spans)
        self.assertEqual(1, len(spans))
        telemetry.validate_span_event(spans[0])
        self.assertEqual("inference", spans[0]["stage"])
        self.assertEqual("0.1.0", spans[0]["limits_version"])
        self.assertEqual("student", spans[0]["role"])

    def test_no_prompt_or_content_persisted(self) -> None:
        secret = "SuperSecret12345"
        self.runtime.provider = providers.FakeModelProvider(content="api_key=" + secret)
        result = self.runtime.execute("do the thing with password=" + secret)
        self.assertEqual("succeeded", result.status)
        for span in read_spans(self.spans):
            self.assertNotIn(secret, json.dumps(span))

    def test_span_written_can_be_disabled(self) -> None:
        agent = runtime.AgentRuntime(providers.FakeModelProvider(), span_output=self.spans, record_spans=False)
        result = agent.execute("do the thing")
        self.assertFalse(result.span_written)
        self.assertFalse(self.spans.exists())

    def test_trace_continuity_across_attempts(self) -> None:
        runtime_agent = runtime.AgentRuntime(FlakyProvider(failures=1), span_output=self.spans)
        result = runtime_agent.execute("do the thing", max_attempts=2)
        self.assertEqual("succeeded", result.status)
        self.assertEqual(2, result.attempts)
        spans = read_spans(self.spans)
        self.assertEqual(2, len(spans))
        self.assertEqual({result.trace_id}, {span["trace_id"] for span in spans})
        self.assertEqual([1, 2], [span["attempt"] for span in spans])
        self.assertEqual([0, 1], [span["retry_count"] for span in spans])


class RuntimeFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.spans = pathlib.Path(self.temp.name) / "spans.jsonl"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def build(self, provider, **kwargs) -> runtime.AgentRuntime:
        return runtime.AgentRuntime(provider, span_output=self.spans, **kwargs)

    def test_empty_prompt_is_validation_failure(self) -> None:
        result = self.build(providers.FakeModelProvider()).execute("   ")
        self.assertEqual("failed", result.status)
        self.assertEqual("validation_failure", result.error_category)
        span = read_spans(self.spans)[0]
        self.assertEqual("validation_failure", span["error_category"])
        self.assertEqual(1, span["attempt"])

    def test_provider_unavailable_classified(self) -> None:
        result = self.build(providers.FakeModelProvider(scenario="unavailable")).execute("hi")
        self.assertEqual("provider_failure", result.error_category)
        self.assertTrue(result.retryable)
        self.assertNotEqual("model_reasoning_failure", result.error_category)

    def test_timeout_classified(self) -> None:
        provider = providers.FakeModelProvider(scenario="timeout", delay_seconds=5.0)
        agent = self.build(provider)
        started = time.perf_counter()
        result = agent.execute("hi", timeout_seconds=0.05)
        elapsed = time.perf_counter() - started
        self.assertEqual("provider_timeout", result.error_category)
        self.assertEqual("failed", result.status)
        self.assertTrue(result.retryable)
        self.assertLess(elapsed, 2.0)

    def test_pre_cancelled_is_user_cancelled(self) -> None:
        cancel = threading.Event()
        cancel.set()
        provider = providers.FakeModelProvider()
        result = self.build(provider).execute("hi", cancel=cancel)
        self.assertEqual("cancelled", result.status)
        self.assertEqual("user_cancelled", result.error_category)
        self.assertEqual(0, len(provider.calls))

    def test_cancellation_during_run(self) -> None:
        cancel = threading.Event()
        provider = providers.FakeModelProvider(delay_seconds=5.0)
        agent = self.build(provider)

        def cancel_soon() -> None:
            time.sleep(0.05)
            cancel.set()

        threading.Thread(target=cancel_soon, daemon=True).start()
        started = time.perf_counter()
        result = agent.execute("hi", cancel=cancel, timeout_seconds=10)
        elapsed = time.perf_counter() - started
        self.assertEqual("cancelled", result.status)
        self.assertEqual("user_cancelled", result.error_category)
        self.assertLess(elapsed, 2.0)

    def test_model_not_configured_is_configuration_failure(self) -> None:
        provider = providers.OllamaProvider(opener=opener_returning({"response": "hi"}))
        result = self.build(provider).execute("hi")
        self.assertEqual("configuration_failure", result.error_category)

    def test_unknown_error_classified(self) -> None:
        result = self.build(BrokenProvider()).execute("hi")
        self.assertEqual("unknown", result.error_category)
        self.assertEqual("failed", result.status)

    def test_attempts_are_bounded_by_limits(self) -> None:
        limits = json.loads(json.dumps(telemetry.DEFAULT_LIMITS))
        limits["anomaly_thresholds"]["max_attempts"] = 2
        agent = runtime.AgentRuntime(FlakyProvider(failures=10), span_output=self.spans, limits=limits)
        result = agent.execute("hi", max_attempts=5)
        self.assertEqual(2, result.attempts)
        self.assertEqual(2, len(read_spans(self.spans)))

    def test_retryable_failure_retries_then_fails(self) -> None:
        agent = self.build(FlakyProvider(failures=5))
        result = agent.execute("hi", max_attempts=2)
        self.assertEqual("failed", result.status)
        self.assertEqual(2, result.attempts)
        self.assertEqual(2, len(read_spans(self.spans)))

    def test_runtime_budget_is_enforced(self) -> None:
        provider = providers.FakeModelProvider()
        agent = runtime.AgentRuntime(provider, span_output=self.spans, clock=StepClock(1.0))
        result = agent.execute("hi", runtime_budget_seconds=0.5)
        self.assertEqual("resource_limit", result.error_category)
        self.assertEqual("max_runtime_exceeded", result.error_code)
        self.assertEqual(0, len(provider.calls))


class HealthCheckTests(unittest.TestCase):
    def test_runtime_health_from_fake(self) -> None:
        agent = runtime.AgentRuntime(providers.FakeModelProvider(health="healthy"), record_spans=False)
        self.assertEqual("healthy", agent.health_check()["status"])
        agent = runtime.AgentRuntime(providers.FakeModelProvider(health="model_missing"), record_spans=False)
        self.assertEqual("model_missing", agent.health_check()["status"])

    def test_runtime_health_never_executes(self) -> None:
        provider = providers.FakeModelProvider(health="unreachable")
        agent = runtime.AgentRuntime(provider, record_spans=False)
        self.assertEqual("unreachable", agent.health_check()["status"])
        self.assertEqual(0, len(provider.calls))


class SecretGuardTests(unittest.TestCase):
    def test_span_validation_rejects_secret_note(self) -> None:
        record = {
            "schema_version": telemetry.SPAN_SCHEMA_VERSION,
            "trace_id": str(uuid.uuid4()),
            "span_id": str(uuid.uuid4()),
            "timestamp_utc": "2026-09-17T10:00:00Z",
            "stage": "inference",
            "duration_ms": 1.0,
            "sanitized_note": "password=SuperSecret12345",
        }
        with self.assertRaises(infra.InfraError):
            telemetry.validate_span_event(record)


class CliTests(unittest.TestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(ROOT / "scripts/agent-run"), *args],
                              cwd=ROOT, text=True, capture_output=True)

    def test_health_fake(self) -> None:
        result = self._run("--provider", "fake", "--health", "--json")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("healthy", json.loads(result.stdout)["status"])

    def test_execute_fake_writes_span(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = pathlib.Path(temp)
            spans = base / "spans.jsonl"
            result = self._run("--provider", "fake", "--prompt", "hello", "--confirm-run",
                               "--json", "--span-output", str(spans),
                               "--outcome-output", str(base / "outcomes.jsonl"))
            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual("succeeded", payload["status"])
            self.assertEqual(1, len(read_spans(spans)))

    def test_execute_requires_confirm_run(self) -> None:
        result = self._run("--provider", "fake", "--prompt", "hello")
        self.assertEqual(2, result.returncode)

    def test_execute_reuses_trace_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            trace_id = str(uuid.uuid4())
            spans = pathlib.Path(temp) / "spans.jsonl"
            result = self._run("--provider", "fake", "--prompt", "hello", "--confirm-run",
                               "--trace-id", trace_id, "--json", "--span-output", str(spans),
                               "--outcome-output", str(pathlib.Path(temp) / "outcomes.jsonl"))
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(trace_id, read_spans(spans)[0]["trace_id"])


@unittest.skipUnless(os.environ.get("LAI_OLLAMA_INTEGRATION") == "1",
                     "set LAI_OLLAMA_INTEGRATION=1 to run the optional live Ollama test")
class OptionalOllamaIntegrationTests(unittest.TestCase):
    """Optional smoke test; skipped unless a healthy Ollama and model already exist."""

    def setUp(self) -> None:
        self.provider = providers.OllamaProvider(
            base_url=os.environ.get("LAI_OLLAMA_BASE_URL", "http://localhost:11434"),
            model=os.environ.get("LAI_OLLAMA_MODEL", "qwen3.6:27b-coding"),
        )
        health = self.provider.health_check()
        if health.status != "healthy":
            self.skipTest("Ollama is not ready: %s" % health.status)

    def test_tiny_generation(self) -> None:
        response = self.provider.execute(providers.ProviderRequest(prompt="Return exactly the word READY.",
                                                                  timeout_seconds=60))
        self.assertTrue(response.content.strip())


if __name__ == "__main__":
    unittest.main()
