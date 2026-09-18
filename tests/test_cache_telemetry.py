from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import escalation  # noqa: E402
import infra  # noqa: E402
import learning  # noqa: E402
import pricing  # noqa: E402
import providers  # noqa: E402
import runtime  # noqa: E402
import telemetry  # noqa: E402


def sample_pricing() -> dict:
    return {
        "schema_version": 1,
        "pricing_version": "test",
        "currency": "USD",
        "providers": {
            "deepseek": {"models": {"deepseek-flash": {
                "normal_input_price_per_million": 1.0,
                "cached_input_price_per_million": 0.1,
                "output_price_per_million": 2.0,
            }}}
        },
    }


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


def opener_returning(body: object):
    def opener(request, timeout=None):
        return FakeResponse(body)
    return opener


def deepseek_reply(content: str = "ok", **usage: object) -> dict:
    return {
        "id": "chatcmpl-1",
        "model": "deepseek-flash",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": usage,
    }


def usage_span(**overrides: object) -> dict:
    record = {
        "schema_version": telemetry.SPAN_SCHEMA_VERSION,
        "trace_id": str(uuid.uuid4()),
        "span_id": str(uuid.uuid4()),
        "timestamp_utc": "2026-09-18T10:00:00Z",
        "stage": "inference",
        "duration_ms": 100.0,
        "provider": "deepseek",
        "model": "deepseek-flash",
        "input_tokens": None,
        "output_tokens": None,
        "cached_input_tokens": None,
        "cache_miss_tokens": None,
        "total_tokens": None,
        "estimated_total_cost": None,
    }
    record.update(overrides)
    return record


class OpenAiUsageNormalizationTests(unittest.TestCase):
    def test_cache_hit_and_miss(self) -> None:
        usage = providers.normalize_openai_usage(
            {"prompt_tokens": 100, "prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 20,
             "completion_tokens": 10})
        self.assertEqual(80, usage["cached_input_tokens"])
        self.assertEqual(20, usage["cache_miss_tokens"])
        self.assertEqual(0.8, telemetry.cache_hit_ratio(usage["cached_input_tokens"], usage["cache_miss_tokens"]))
        self.assertEqual(110, usage["total_tokens"])

    def test_full_miss(self) -> None:
        usage = providers.normalize_openai_usage(
            {"prompt_tokens": 100, "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 100,
             "completion_tokens": 5})
        self.assertEqual(0, usage["cached_input_tokens"])
        self.assertEqual(0.0, telemetry.cache_hit_ratio(0, 100))

    def test_high_cache_hit(self) -> None:
        usage = providers.normalize_openai_usage(
            {"prompt_tokens": 1000, "prompt_cache_hit_tokens": 990, "prompt_cache_miss_tokens": 10,
             "completion_tokens": 5})
        self.assertEqual(0.99, telemetry.cache_hit_ratio(usage["cached_input_tokens"], usage["cache_miss_tokens"]))

    def test_zero_input_has_no_ratio(self) -> None:
        usage = providers.normalize_openai_usage(
            {"prompt_tokens": 0, "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0,
             "completion_tokens": 3})
        self.assertIsNone(telemetry.cache_hit_ratio(usage["cached_input_tokens"], usage["cache_miss_tokens"]))

    def test_missing_optional_fields_stay_null(self) -> None:
        usage = providers.normalize_openai_usage({"prompt_tokens": 10, "completion_tokens": 2})
        self.assertIsNone(usage["cached_input_tokens"])
        self.assertIsNone(usage["cache_miss_tokens"])
        self.assertIsNone(usage["reasoning_tokens"])
        self.assertEqual(12, usage["total_tokens"])

    def test_qwen_cached_tokens_in_details_derives_miss(self) -> None:
        usage = providers.normalize_openai_usage(
            {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 60},
             "completion_tokens": 4})
        self.assertEqual(60, usage["cached_input_tokens"])
        self.assertEqual(40, usage["cache_miss_tokens"])

    def test_cached_tokens_absent(self) -> None:
        usage = providers.normalize_openai_usage({"prompt_tokens": 100, "completion_tokens": 4})
        self.assertIsNone(usage["cached_input_tokens"])
        self.assertIsNone(usage["cache_miss_tokens"])

    def test_reasoning_tokens_from_completion_details(self) -> None:
        usage = providers.normalize_openai_usage(
            {"prompt_tokens": 10, "completion_tokens": 8, "completion_tokens_details": {"reasoning_tokens": 5}})
        self.assertEqual(5, usage["reasoning_tokens"])


class DeepSeekProviderCacheTests(unittest.TestCase):
    def test_provider_populates_cache_fields(self) -> None:
        provider = providers.DeepSeekProvider(
            model="deepseek-flash", api_key="k",
            opener=opener_returning(deepseek_reply(
                "answer", prompt_tokens=1000, prompt_cache_hit_tokens=800, prompt_cache_miss_tokens=200,
                completion_tokens=100, total_tokens=1100)))
        response = provider.execute(providers.ProviderRequest(prompt="hi"))
        self.assertEqual(800, response.cached_input_tokens)
        self.assertEqual(200, response.cache_miss_tokens)
        self.assertEqual(1100, response.total_tokens)
        self.assertEqual(0.8, telemetry.cache_hit_ratio(response.cached_input_tokens, response.cache_miss_tokens))

    def test_provider_derives_miss_from_details(self) -> None:
        provider = providers.DeepSeekProvider(
            model="deepseek-flash", api_key="k",
            opener=opener_returning(deepseek_reply("answer", prompt_tokens=100,
                                                   prompt_tokens_details={"cached_tokens": 60},
                                                   completion_tokens=4)))
        response = provider.execute(providers.ProviderRequest(prompt="hi"))
        self.assertEqual(60, response.cached_input_tokens)
        self.assertEqual(40, response.cache_miss_tokens)

    def test_provider_without_usage_is_null(self) -> None:
        provider = providers.DeepSeekProvider(model="deepseek-flash", api_key="k",
                                              opener=opener_returning(deepseek_reply("answer")))
        response = provider.execute(providers.ProviderRequest(prompt="hi"))
        self.assertIsNone(response.cached_input_tokens)
        self.assertIsNone(response.cache_miss_tokens)
        self.assertIsNone(response.total_tokens)


class PricingTests(unittest.TestCase):
    def test_cached_uncached_and_output_costs(self) -> None:
        cost = pricing.estimate_usage_cost("deepseek", "deepseek-flash", input_tokens=1000,
                                           cached_input_tokens=800, cache_miss_tokens=200,
                                           output_tokens=100, pricing=sample_pricing())
        self.assertAlmostEqual(0.0002, cost["estimated_input_cost"])
        self.assertAlmostEqual(0.00008, cost["estimated_cached_input_cost"])
        self.assertAlmostEqual(0.0002, cost["estimated_output_cost"])
        self.assertAlmostEqual(0.00048, cost["estimated_total_cost"])
        self.assertEqual("deepseek/deepseek-flash", cost["pricing_profile"])
        self.assertEqual("USD", cost["pricing_currency"])

    def test_input_cost_prices_miss_tokens(self) -> None:
        cost = pricing.estimate_usage_cost("deepseek", "deepseek-flash", input_tokens=1000,
                                           cached_input_tokens=900, output_tokens=0,
                                           pricing=sample_pricing())
        self.assertAlmostEqual(0.0001, cost["estimated_input_cost"])

    def test_unknown_pricing_stays_null(self) -> None:
        unknown = {"schema_version": 1, "pricing_version": "x", "currency": "USD", "providers": {}}
        cost = pricing.estimate_usage_cost("deepseek", "deepseek-flash", input_tokens=1000,
                                           cached_input_tokens=800, cache_miss_tokens=200,
                                           output_tokens=100, pricing=unknown)
        for field in pricing.COST_FIELDS:
            self.assertIsNone(cost[field], field)

    def test_no_model_entry_stays_null(self) -> None:
        cost = pricing.estimate_usage_cost("ollama", "qwen", input_tokens=10, output_tokens=5,
                                           pricing=sample_pricing())
        self.assertIsNone(cost["estimated_total_cost"])
        self.assertIsNone(cost["pricing_profile"])

    def test_invalid_price_rejected(self) -> None:
        bad = sample_pricing()
        bad["providers"]["deepseek"]["models"]["deepseek-flash"]["output_price_per_million"] = -1
        with self.assertRaises(infra.InfraError):
            pricing.load_pricing(_write(bad))

    def test_missing_price_field_rejected(self) -> None:
        bad = sample_pricing()
        del bad["providers"]["deepseek"]["models"]["deepseek-flash"]["cached_input_price_per_million"]
        with self.assertRaises(infra.InfraError):
            pricing.load_pricing(_write(bad))

    def test_missing_file_falls_back_to_empty(self) -> None:
        config = pricing.load_pricing(pathlib.Path("/nonexistent/pricing.yaml"))
        self.assertEqual(pricing.DEFAULT_PRICING, config)

    def test_repo_pricing_file_loads(self) -> None:
        config = pricing.load_pricing()
        self.assertIsInstance(config.get("providers"), dict)


def _write(value: dict) -> pathlib.Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(value, handle)
    handle.close()
    return pathlib.Path(handle.name)


class AggregationTests(unittest.TestCase):
    def test_cache_hit_ratio_and_totals(self) -> None:
        spans = [
            usage_span(input_tokens=1000, cached_input_tokens=800, cache_miss_tokens=200,
                       output_tokens=100, total_tokens=1100, estimated_total_cost=0.00048),
            usage_span(input_tokens=500, cached_input_tokens=300, cache_miss_tokens=200,
                       output_tokens=50, total_tokens=550, estimated_total_cost=0.0001),
        ]
        metrics = telemetry.summarize_usage(spans)
        self.assertEqual(2, metrics["total_requests"])
        self.assertEqual(1500, metrics["input_tokens"])
        self.assertEqual(1100, metrics["cached_input_tokens"])
        self.assertEqual(400, metrics["cache_miss_tokens"])
        self.assertEqual(150, metrics["output_tokens"])
        self.assertAlmostEqual(0.7333, metrics["cache_hit_ratio"])
        self.assertAlmostEqual(0.00058, metrics["estimated_total_cost"])
        self.assertEqual(100.0, metrics["avg_latency_ms"])

    def test_breakdown_by_provider_and_model(self) -> None:
        spans = [
            usage_span(provider="deepseek", model="deepseek-flash", input_tokens=100,
                       cached_input_tokens=80, cache_miss_tokens=20, output_tokens=10),
            usage_span(provider="ollama", model="qwen", input_tokens=50, output_tokens=5),
        ]
        metrics = telemetry.summarize_usage(spans)
        keys = {(group["provider"], group["model"]) for group in metrics["by_provider_model"]}
        self.assertEqual({("deepseek", "deepseek-flash"), ("ollama", "qwen")}, keys)
        deepseek = [g for g in metrics["by_provider_model"] if g["provider"] == "deepseek"][0]
        self.assertEqual(0.8, deepseek["cache_hit_ratio"])
        ollama = [g for g in metrics["by_provider_model"] if g["provider"] == "ollama"][0]
        self.assertIsNone(ollama["cache_hit_ratio"])

    def test_only_inference_spans_count(self) -> None:
        spans = [usage_span(), {**usage_span(), "stage": "tool"}]
        metrics = telemetry.summarize_usage(spans)
        self.assertEqual(1, metrics["total_requests"])
        self.assertEqual(1, metrics["successful_requests"])

    def test_failed_request_counted_but_not_successful(self) -> None:
        spans = [usage_span(error_category="provider_failure")]
        metrics = telemetry.summarize_usage(spans)
        self.assertEqual(1, metrics["total_requests"])
        self.assertEqual(0, metrics["successful_requests"])
        self.assertIsNone(metrics["avg_input_tokens_per_request"])


class RuntimePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.spans = pathlib.Path(self.temp.name) / "spans.jsonl"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _runtime(self) -> tuple:
        provider = providers.DeepSeekProvider(
            model="deepseek-flash", api_key="k",
            opener=opener_returning(deepseek_reply(
                "fixed", prompt_tokens=1000, prompt_cache_hit_tokens=800, prompt_cache_miss_tokens=200,
                completion_tokens=100, total_tokens=1100)))
        agent = runtime.AgentRuntime(provider, span_output=self.spans, pricing_config=sample_pricing())
        return agent, provider

    def test_span_carries_normalized_usage_cost_and_prefix_hash(self) -> None:
        agent, _ = self._runtime()
        agent.execute("STABLE SYSTEM PREFIX\ncurrent task")
        span = learning.load_jsonl(self.spans)[0]
        telemetry.validate_span_event(span)
        self.assertEqual(200, span["cache_miss_tokens"])
        self.assertEqual(1100, span["total_tokens"])
        self.assertEqual(0.8, span["cache_hit_ratio"])
        self.assertAlmostEqual(0.00048, span["estimated_total_cost"])
        self.assertEqual("deepseek/deepseek-flash", span["pricing_profile"])
        self.assertEqual("USD", span["pricing_currency"])
        self.assertTrue(span["prompt_prefix_hash"].startswith("sha256:"))
        self.assertNotIn("STABLE SYSTEM PREFIX", json.dumps(span))

    def test_prefix_hash_is_stable_and_distinguishing(self) -> None:
        agent, _ = self._runtime()
        stable = "S" * 2100  # exceeds the 2048-char prefix window
        agent.execute(stable + " current task A")
        agent.execute(stable + " current task B")
        agent.execute(("T" * 2100) + " current task A")
        spans = learning.load_jsonl(self.spans)
        self.assertEqual(spans[0]["prompt_prefix_hash"], spans[1]["prompt_prefix_hash"])
        self.assertNotEqual(spans[0]["prompt_prefix_hash"], spans[2]["prompt_prefix_hash"])

    def test_unconfigured_pricing_keeps_costs_null(self) -> None:
        provider = providers.DeepSeekProvider(
            model="deepseek-flash", api_key="k",
            opener=opener_returning(deepseek_reply("ok", prompt_tokens=10, prompt_cache_hit_tokens=4,
                                                   prompt_cache_miss_tokens=6, completion_tokens=2)))
        agent = runtime.AgentRuntime(provider, span_output=self.spans)
        agent.execute("hi")
        span = learning.load_jsonl(self.spans)[0]
        self.assertIsNone(span["estimated_total_cost"])
        self.assertIsNone(span["pricing_profile"])
        self.assertEqual(4, span["cached_input_tokens"])


class EscalationCostTests(unittest.TestCase):
    class ReasoningFailureProvider(providers.ModelProvider):
        name = "fake"
        model = "qwen-local"

        def execute(self, request):
            raise providers.ProviderError("reasoning_gap", category="model_reasoning_failure", retryable=False)

        def health_check(self):
            return providers.ProviderHealth(provider=self.name, status="healthy", model=self.model)

    def test_escalation_records_estimated_cost_when_priced(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = pathlib.Path(temp)
            fallback = providers.DeepSeekProvider(
                model="deepseek-flash", api_key="k",
                opener=opener_returning(deepseek_reply(
                    "fixed", prompt_tokens=1000, prompt_cache_hit_tokens=800, prompt_cache_miss_tokens=200,
                    completion_tokens=100)))
            policy = escalation.EscalationPolicy(enabled=True, mode="automatic", hosted_allowed=True,
                                                 fallback_provider="deepseek")
            with unittest.mock.patch.object(pricing, "load_pricing", return_value=sample_pricing()):
                controller = escalation.EscalationController(
                    policy, fallback, span_output=base / "spans.jsonl", escalation_output=base / "esc.jsonl")
                agent = runtime.AgentRuntime(self.ReasoningFailureProvider(), span_output=base / "spans.jsonl")
                task = runtime.AgentTask(prompt="solve the task")
                session = agent.open_session(task)
                student = agent.run(task, session=session)
                outcome = controller.maybe_escalate(session, task, student, task_class="bounded")
                self.assertTrue(outcome.escalated)
            row = learning.load_jsonl(base / "esc.jsonl")[0]
            self.assertAlmostEqual(0.00048, row["estimated_cost_usd"])
            self.assertEqual("deepseek/deepseek-flash", row["pricing_source"])


class CacheMetricsCliTests(unittest.TestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(ROOT / "scripts/cache-metrics"), *args],
                              cwd=ROOT, text=True, capture_output=True)

    def test_json_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "spans.jsonl"
            path.write_text(
                "\n".join(json.dumps(usage_span(input_tokens=100, cached_input_tokens=80,
                                                cache_miss_tokens=20, output_tokens=10))
                          for _ in range(3)) + "\n",
                encoding="utf-8")
            result = self._run("--spans", str(path), "--json")
            self.assertEqual(0, result.returncode, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(3, report["total_requests"])
            self.assertEqual(240, report["cached_input_tokens"])
            self.assertEqual(0.8, report["cache_hit_ratio"])

    def test_text_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "spans.jsonl"
            path.write_text(json.dumps(usage_span(input_tokens=100, cached_input_tokens=80,
                                                  cache_miss_tokens=20, output_tokens=10)) + "\n",
                            encoding="utf-8")
            result = self._run("--spans", str(path))
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("Provider Cache Metrics", result.stdout)
            self.assertIn("cache hit ratio   80.00%", result.stdout)

    def test_missing_explicit_file_errors(self) -> None:
        result = self._run("--spans", "/nonexistent/spans.jsonl", "--json")
        self.assertEqual(1, result.returncode)


if __name__ == "__main__":
    unittest.main()
