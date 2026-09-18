from __future__ import annotations

import http.client
import json
import pathlib
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/common"))
import gateway  # noqa: E402
import learning  # noqa: E402
import providers  # noqa: E402
import telemetry  # noqa: E402

AUTH = "Bearer sk-test-secret-value-should-never-appear"


def sample_pricing() -> dict:
    return {
        "schema_version": 1,
        "pricing_version": "test",
        "currency": "USD",
        "providers": {"deepseek": {"models": {"deepseek-flash": {
            "normal_input_price_per_million": 1.0,
            "cached_input_price_per_million": 0.1,
            "output_price_per_million": 2.0,
        }}}},
    }


class _UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        self._run()

    def do_GET(self) -> None:
        self._run()

    def do_HEAD(self) -> None:
        self._run()

    def _run(self) -> None:
        upstream = self.server.fake
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        upstream.requests.append({
            "method": self.command,
            "path": self.path,
            "headers": dict(self.headers.items()),
            "body": body,
        })
        status, headers, raw = upstream.response
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        if upstream.stream_lines is not None:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for line in upstream.stream_lines:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
            self.wfile.write(b"0\r\n\r\n")
        else:
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(raw)


class FakeUpstream:
    def __init__(self) -> None:
        self.requests: list = []
        self.response = (200, [("Content-Type", "application/json")], b"{}")
        self.stream_lines = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
        self.server.daemon_threads = True
        self.server.fake = self

    def start(self) -> "FakeUpstream":
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class GatewayHarness:
    def __init__(self, upstream_base_url: str, span_output: pathlib.Path, **kwargs: object) -> None:
        self.engine = gateway.Gateway(upstream_base_url, span_output=span_output, **kwargs)
        self.server = gateway.create_server(self.engine, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def request(self, method: str, path: str, body: bytes = b"", headers: dict | None = None,
                timeout: float = 10.0) -> tuple:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            data = response.read()
            return response.status, dict(response.getheaders()), data
        finally:
            connection.close()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def chat_body(stream: bool = False, content: str = "ping") -> bytes:
    return json.dumps({
        "model": "deepseek-flash",
        "messages": [{"role": "user", "content": content}],
        "stream": stream,
    }).encode("utf-8")


def deepseek_response(**usage: object) -> bytes:
    return json.dumps({
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "deepseek-flash",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}],
        "usage": usage,
    }).encode("utf-8")


def sse(*payloads: object) -> bytes:
    lines = [b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n" for payload in payloads]
    lines.append(b"data: [DONE]\n\n")
    return b"".join(lines)


def wait_for_spans(path: pathlib.Path, count: int = 1, timeout: float = 2.0) -> list:
    deadline = time.monotonic() + timeout
    while True:
        if path.exists():
            records = learning.load_jsonl(path)
            if len(records) >= count:
                return records
        if time.monotonic() >= deadline:
            return learning.load_jsonl(path) if path.exists() else []
        time.sleep(0.02)


class GatewayTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.spans = pathlib.Path(self.temp.name) / "spans.jsonl"
        self.upstream = FakeUpstream().start()
        self.addCleanup(self.upstream.close)
        self.gateway = GatewayHarness(self.upstream.base_url, self.spans)
        self.addCleanup(self.gateway.close)

    def _span(self, index: int = 0) -> dict:
        records = wait_for_spans(self.spans, index + 1)
        self.assertGreaterEqual(len(records), index + 1, "no gateway span was written")
        telemetry.validate_span_event(records[index])
        return records[index]

    def _request(self, path: str = "/v1/chat/completions", body: bytes | None = None,
                 auth: str = AUTH) -> tuple:
        return self.gateway.request(
            "POST", path, body if body is not None else chat_body(),
            {"Content-Type": "application/json", "Authorization": auth},
        )


class PassthroughTests(GatewayTestCase):
    def test_non_streaming_request_is_forwarded_and_recorded(self) -> None:
        self.upstream.response = (200, [("Content-Type", "application/json")], deepseek_response(
            prompt_tokens=1000, completion_tokens=50, total_tokens=1050,
            prompt_cache_hit_tokens=800, prompt_cache_miss_tokens=200,
            completion_tokens_details={"reasoning_tokens": 12}))
        status, _, data = self._request()
        self.assertEqual(200, status)
        self.assertEqual(self.upstream.response[2], data)
        sent = self.upstream.requests[0]
        self.assertEqual("/v1/chat/completions", sent["path"])
        self.assertEqual(AUTH, sent["headers"]["Authorization"])
        span = self._span()
        self.assertEqual("deepseek", span["provider"])
        self.assertEqual("deepseek-flash", span["model"])
        self.assertEqual(1000, span["input_tokens"])
        self.assertEqual(800, span["cached_input_tokens"])
        self.assertEqual(200, span["cache_miss_tokens"])
        self.assertEqual(50, span["output_tokens"])
        self.assertEqual(12, span["reasoning_tokens"])
        self.assertEqual(1050, span["total_tokens"])
        self.assertEqual(0.8, span["cache_hit_ratio"])

    def test_chat_completions_without_v1_prefix_is_accepted(self) -> None:
        self.upstream.response = (200, [("Content-Type", "application/json")], deepseek_response(
            prompt_tokens=10, completion_tokens=2))
        status, _, _ = self._request(path="/chat/completions")
        self.assertEqual(200, status)
        self.assertEqual("/chat/completions", self.upstream.requests[0]["path"])

    def test_openai_cached_tokens_shape_is_normalized(self) -> None:
        self.upstream.response = (200, [("Content-Type", "application/json")], deepseek_response(
            prompt_tokens=100, completion_tokens=4,
            prompt_tokens_details={"cached_tokens": 60}))
        self._request()
        span = self._span()
        self.assertEqual(60, span["cached_input_tokens"])
        self.assertEqual(40, span["cache_miss_tokens"])

    def test_cost_is_estimated_when_pricing_is_configured(self) -> None:
        self.gateway.close()
        self.gateway = GatewayHarness(self.upstream.base_url, self.spans, pricing_config=sample_pricing())
        self.addCleanup(self.gateway.close)
        self.upstream.response = (200, [("Content-Type", "application/json")], deepseek_response(
            prompt_tokens=1000, completion_tokens=100, total_tokens=1100,
            prompt_cache_hit_tokens=800, prompt_cache_miss_tokens=200))
        self._request()
        span = self._span()
        self.assertAlmostEqual(0.00048, span["estimated_total_cost"])
        self.assertEqual("deepseek/deepseek-flash", span["pricing_profile"])

    def test_correlation_header_is_used_as_trace_id(self) -> None:
        self.upstream.response = (200, [("Content-Type", "application/json")], deepseek_response(
            prompt_tokens=5, completion_tokens=1))
        trace_id = str(uuid.uuid4())
        headers = {"Content-Type": "application/json", "Authorization": AUTH, "x-request-id": trace_id}
        self.gateway.request("POST", "/v1/chat/completions", chat_body(), headers)
        self.assertEqual(trace_id, self._span()["trace_id"])

    def test_models_path_is_proxied_without_inference_span(self) -> None:
        self.upstream.response = (200, [("Content-Type", "application/json")], b'{"data":[]}')
        status, _, data = self.gateway.request("GET", "/v1/models", b"", {"Authorization": AUTH})
        self.assertEqual(200, status)
        self.assertEqual(b'{"data":[]}', data)
        self.assertFalse(self.spans.exists())


class StreamingTests(GatewayTestCase):
    def test_streaming_chunks_are_relayed_and_usage_is_captured(self) -> None:
        self.upstream.response = (200, [("Content-Type", "text/event-stream")],
                                  sse(
                                      {"model": "deepseek-flash", "choices": [{"delta": {"content": "po"}}]},
                                      {"model": "deepseek-flash", "choices": [{"delta": {"content": "ng"}}]},
                                      {"model": "deepseek-flash", "choices": [{"delta": {}, "finish_reason": "stop"}],
                                       "usage": {"prompt_tokens": 500, "completion_tokens": 2, "total_tokens": 502,
                                                 "prompt_cache_hit_tokens": 400, "prompt_cache_miss_tokens": 100,
                                                 "completion_tokens_details": {"reasoning_tokens": 3}}},
                                  ))
        status, headers, data = self._request(body=chat_body(stream=True))
        self.assertEqual(200, status)
        self.assertEqual("chunked", headers.get("Transfer-Encoding", "").lower())
        self.assertIn(b"data: [DONE]", data)
        self.assertIn(b'"content": "po"', data)
        self.assertIn(b'"finish_reason": "stop"', data)
        span = self._span()
        self.assertEqual(500, span["input_tokens"])
        self.assertEqual(400, span["cached_input_tokens"])
        self.assertEqual(100, span["cache_miss_tokens"])
        self.assertEqual(3, span["reasoning_tokens"])

    def test_streaming_without_usage_still_records_latency(self) -> None:
        self.upstream.response = (200, [("Content-Type", "text/event-stream")],
                                  sse({"model": "deepseek-flash", "choices": [{"delta": {"content": "x"}}]}))
        status, _, _ = self._request(body=chat_body(stream=True))
        self.assertEqual(200, status)
        span = self._span()
        self.assertIsNone(span["input_tokens"])
        self.assertGreaterEqual(span["duration_ms"], 0)


class ErrorPropagationTests(GatewayTestCase):
    def test_upstream_400_is_preserved(self) -> None:
        body = b'{"error":{"message":"bad request","type":"invalid_request_error"}}'
        self.upstream.response = (400, [("Content-Type", "application/json")], body)
        status, _, data = self._request()
        self.assertEqual(400, status)
        self.assertEqual(body, data)
        span = self._span()
        self.assertEqual("validation_failure", span["error_category"])
        self.assertEqual("bad_request", span["error_code"])

    def test_upstream_429_is_preserved(self) -> None:
        body = b'{"error":{"message":"slow down","type":"rate_limit_error"}}'
        self.upstream.response = (429, [("Content-Type", "application/json")], body)
        status, _, data = self._request()
        self.assertEqual(429, status)
        self.assertEqual(body, data)
        span = self._span()
        self.assertEqual("provider_failure", span["error_category"])
        self.assertEqual("rate_limited", span["error_code"])

    def test_upstream_503_is_preserved(self) -> None:
        self.upstream.response = (503, [("Content-Type", "application/json")], b'{"error":"unavailable"}')
        status, _, _ = self._request()
        self.assertEqual(503, status)
        span = self._span()
        self.assertEqual("provider_failure", span["error_category"])
        self.assertEqual("http_503", span["error_code"])

    def test_unreachable_upstream_returns_502(self) -> None:
        harness = GatewayHarness("http://127.0.0.1:1", self.spans)
        self.addCleanup(harness.close)
        status, _, data = harness.request("POST", "/v1/chat/completions", chat_body())
        self.assertEqual(502, status)
        self.assertIn(b"gateway_error", data)
        span = self._span()
        self.assertEqual("provider_failure", span["error_category"])
        self.assertEqual("connection_error", span["error_code"])

    def test_unknown_path_is_404(self) -> None:
        status, _, _ = self.gateway.request("GET", "/v1/embeddings", b"", {"Authorization": AUTH})
        self.assertEqual(404, status)

    def test_malformed_upstream_body_is_relayed(self) -> None:
        self.upstream.response = (200, [("Content-Type", "text/html")], b"<html>not-json</html>")
        status, _, data = self._request()
        self.assertEqual(200, status)
        self.assertEqual(b"<html>not-json</html>", data)
        span = self._span()
        self.assertIsNone(span["input_tokens"])


class TelemetrySafetyTests(GatewayTestCase):
    def test_telemetry_failure_does_not_break_response(self) -> None:
        # A directory path makes the append fail with OSError; the response must survive.
        self.gateway.close()
        self.gateway = GatewayHarness(self.upstream.base_url, pathlib.Path(self.temp.name))
        self.addCleanup(self.gateway.close)
        self.upstream.response = (200, [("Content-Type", "application/json")], deepseek_response(
            prompt_tokens=10, completion_tokens=2))
        status, _, data = self._request()
        self.assertEqual(200, status)
        self.assertEqual(self.upstream.response[2], data)

    def test_authorization_and_prompt_are_never_persisted(self) -> None:
        secret_prompt = "MY-PRIVATE-PROMPT-9d1f"
        self.upstream.response = (200, [("Content-Type", "application/json")], deepseek_response(
            prompt_tokens=10, completion_tokens=2))
        self._request(body=chat_body(content=secret_prompt))
        wait_for_spans(self.spans)
        text = self.spans.read_text(encoding="utf-8")
        self.assertNotIn("sk-test-secret", text)
        self.assertNotIn(secret_prompt, text)
        span = self._span()
        self.assertTrue(span["prompt_prefix_hash"].startswith("sha256:"))
        # The prompt itself was still forwarded unchanged to the upstream.
        self.assertIn(secret_prompt.encode("utf-8"), self.upstream.requests[0]["body"])

    def test_authorization_is_not_logged(self) -> None:
        self.gateway.close()
        self.gateway = GatewayHarness(self.upstream.base_url, self.spans, debug=True)
        self.addCleanup(self.gateway.close)
        self.upstream.response = (200, [("Content-Type", "application/json")], deepseek_response(
            prompt_tokens=1, completion_tokens=1))
        with self.assertLogs("local-ai-infra.gateway", level="INFO") as logs:
            self._request()
        text = "\n".join(logs.output)
        self.assertNotIn("sk-test-secret", text)
        self.assertNotIn("Authorization", text)


if __name__ == "__main__":
    unittest.main()
