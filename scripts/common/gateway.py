#!/usr/bin/env python3
"""Transparent OpenAI-compatible pass-through gateway with inference telemetry.

OpenCode talks to this gateway, the gateway talks to the configured upstream
(DeepSeek by default). The request is forwarded as-is: method, path, query,
body, and every non-hop-by-hop header, including the caller's own
``Authorization``. The gateway therefore never needs the provider credential and
never stores, logs, or echoes it.

The response is relayed transparently: status, headers, and body are preserved
(4xx/429/5xx are never converted into a generic 500), and SSE streams are
forwarded line-by-line instead of being buffered. The gateway parses only the
small final usage object from the stream to emit one normalized ``inference``
span through the existing runtime span builder. Prompts and response content are
never persisted; only metadata, token counts, cache fields, latency, and cost.

Ponytail: this is intentionally a pass-through, not a router. One upstream, one
endpoint family, no retries/routing/fallback. Upgrade path when evidence supports
it: add policy in front of ``Gateway.forward`` without changing this transport.
"""

from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterable, Iterator, Optional

import infra
import pricing
import providers
import runtime
import telemetry

LOGGER = logging.getLogger("local-ai-infra.gateway")

DEFAULT_UPSTREAM_BASE_URL = providers.DEEPSEEK_DEFAULT_BASE_URL
DEFAULT_PROVIDER_NAME = "deepseek"
DEFAULT_ROLE = "teacher-cheap"
DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8788

EVENT_STREAM = "text/event-stream"
JSON_CONTENT_TYPE = "application/json"
INFERENCE_PATHS = ("/v1/chat/completions", "/chat/completions")
ALLOWED_PATHS = INFERENCE_PATHS + ("/v1/models", "/models")
CORRELATION_HEADERS = ("x-request-id", "x-opencode-session-id", "x-session-id")

# Framing/hop-by-hop headers are recomputed, Host is set by urllib, and
# Accept-Encoding is forced to identity so relayed bodies and SSE usage chunks
# stay parseable without decompression.
STRIP_REQUEST_HEADERS = frozenset({
    "host", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "accept-encoding",
})
STRIP_RESPONSE_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
})


class _StreamUsageTracker:
    """Incrementally extract model and final usage from an SSE byte stream.

    Only complete ``data:`` lines are parsed; parsed objects are discarded after
    reading their metadata, so neither content deltas nor prompts are retained.
    """

    def __init__(self) -> None:
        self._buffer = b""
        self.model: Optional[str] = None
        self.usage: Optional[dict] = None
        self.error: Optional[providers.ProviderError] = None

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            self._consume(line.strip())

    def _consume(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            return
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        model = data.get("model")
        if isinstance(model, str) and model:
            self.model = model
        usage = data.get("usage")
        if isinstance(usage, dict) and usage:
            self.usage = usage


@dataclass
class ForwardedResponse:
    """A relayed upstream response. ``chunks`` records telemetry when exhausted."""

    status: int
    headers: list
    streamed: bool
    content_length: Optional[int]
    chunks: Iterator[bytes]


@dataclass
class _RequestContext:
    trace_id: str
    run_id: str
    request_model: Optional[str]
    prompt_prefix_hash: Optional[str]
    started: float
    record: bool


def _json_object(body: Any) -> Optional[dict]:
    if not body:
        return None
    try:
        value = json.loads(body.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _header_value(headers: Any, name: str) -> Optional[str]:
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name)
        return value if isinstance(value, str) else None
    return None


def _sanitize_response_headers(headers: Any) -> list:
    items = headers.items() if headers is not None and hasattr(headers, "items") else []
    return [(name, value) for name, value in items if name.lower() not in STRIP_RESPONSE_HEADERS]


def _prompt_prefix_hash(payload: Optional[dict]) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return None
    parts: list = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
    text = "\n".join(parts)
    if not text.strip():
        return None
    try:
        return telemetry.hash_prompt_prefix(text)
    except Exception:
        return None


def _correlation_ids(headers: Iterable) -> tuple:
    values = {name.lower(): value for name, value in headers}
    for key in CORRELATION_HEADERS:
        candidate = values.get(key)
        if isinstance(candidate, str):
            try:
                uuid.UUID(candidate.strip())
                return candidate.strip(), str(uuid.uuid4())
            except (ValueError, AttributeError, TypeError):
                continue
    return str(uuid.uuid4()), str(uuid.uuid4())


def _usage_from_body(body: bytes) -> tuple:
    payload = _json_object(body)
    if not isinstance(payload, dict):
        return None, None
    usage = payload.get("usage")
    model = payload.get("model") if isinstance(payload.get("model"), str) else None
    return (usage if isinstance(usage, dict) else None), model


class Gateway:
    """Forward OpenAI-compatible requests to one configured upstream."""

    def __init__(
        self,
        upstream_base_url: str = DEFAULT_UPSTREAM_BASE_URL,
        *,
        provider_name: str = DEFAULT_PROVIDER_NAME,
        role: str = DEFAULT_ROLE,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        span_output: Optional[Any] = None,
        pricing_config: Optional[dict] = None,
        record_telemetry: bool = True,
        opener: Optional[Any] = None,
        debug: bool = False,
    ) -> None:
        self.upstream_base_url = providers.validate_base_url(upstream_base_url)
        self.provider_name = provider_name
        self.role = role
        self.timeout_seconds = timeout_seconds
        self.span_output = infra.ROOT / "telemetry/spans/runs.jsonl" if span_output is None else span_output
        self.pricing = pricing_config if pricing_config is not None else pricing.load_pricing()
        self.record_telemetry = record_telemetry
        self.debug = debug
        self._opener = opener or urllib.request.urlopen

    def describe(self) -> dict:
        return {
            "upstream_base_url": self.upstream_base_url,
            "provider": self.provider_name,
            "role": self.role,
            "timeout_seconds": self.timeout_seconds,
            "span_output": str(self.span_output),
            "record_telemetry": self.record_telemetry,
            "allowed_paths": list(ALLOWED_PATHS),
        }

    def forward(self, method: str, target: str, headers: Any, body: bytes) -> ForwardedResponse:
        header_list = list(headers)
        payload = _json_object(body)
        request_model = None
        stream_requested = False
        if isinstance(payload, dict):
            if isinstance(payload.get("model"), str):
                request_model = payload["model"]
            stream_requested = bool(payload.get("stream"))
        path = urllib.parse.urlsplit(target).path
        trace_id, run_id = _correlation_ids(header_list)
        context = _RequestContext(
            trace_id=trace_id,
            run_id=run_id,
            request_model=request_model,
            prompt_prefix_hash=_prompt_prefix_hash(payload),
            started=time.perf_counter(),
            record=self.record_telemetry and path in INFERENCE_PATHS,
        )
        upstream = self._build_request(method, target, header_list, body)
        try:
            response = self._open(upstream)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            headers_out = _sanitize_response_headers(exc.headers)
            self._finish(context, exc.code, None, None,
                         providers.translate_openai_http_error(exc.code, raw, label="upstream"))
            return ForwardedResponse(exc.code, headers_out, False, len(raw), iter((raw,)))
        except (TimeoutError, socket.timeout):
            return self._synthetic_error(
                context, 504, providers.ProviderTimeout("timeout", "upstream did not respond in time"))
        except (urllib.error.URLError, OSError):
            return self._synthetic_error(
                context, 502, providers.ProviderUnavailable("connection_error", "upstream is unreachable"))

        status = getattr(response, "status", 200)
        headers_out = _sanitize_response_headers(getattr(response, "headers", None))
        content_type = (_header_value(getattr(response, "headers", None), "content-type") or "").lower()
        if status >= 400:
            raw = response.read()
            response.close()
            self._finish(context, status, None, None,
                         providers.translate_openai_http_error(status, raw, label="upstream"))
            return ForwardedResponse(status, headers_out, False, len(raw), iter((raw,)))
        response_length = _header_value(getattr(response, "headers", None), "content-length")
        if EVENT_STREAM in content_type or (stream_requested and response_length is None):
            return ForwardedResponse(status, headers_out, True, None,
                                     self._stream_chunks(response, context, status))
        raw = response.read()
        response.close()
        usage, response_model = _usage_from_body(raw)
        return ForwardedResponse(status, headers_out, False, len(raw),
                                 self._buffered_chunks(raw, context, status, usage, response_model))

    def _build_request(self, method: str, target: str, header_list: list, body: bytes) -> Any:
        url = self.upstream_base_url + target
        upstream_headers = {"Accept-Encoding": "identity"}
        for name, value in header_list:
            if name.lower() in STRIP_REQUEST_HEADERS:
                continue
            upstream_headers[name] = value
        data = body if method.upper() not in ("GET", "HEAD") else None
        return urllib.request.Request(url, data=data, headers=upstream_headers, method=method.upper())

    def _open(self, request: Any) -> Any:
        return self._opener(request, timeout=self.timeout_seconds)

    def _synthetic_error(self, context: _RequestContext, status: int, error: providers.ProviderError) -> ForwardedResponse:
        body = json.dumps({"error": {"message": error.code, "type": "gateway_error"}}).encode("utf-8")
        self._finish(context, status, None, None, error)
        headers = [("Content-Type", JSON_CONTENT_TYPE)]
        return ForwardedResponse(status, headers, False, len(body), iter((body,)))

    def _buffered_chunks(self, raw: bytes, context: _RequestContext, status: int,
                         usage: Optional[dict], model: Optional[str]) -> Iterator[bytes]:
        def gen() -> Iterator[bytes]:
            try:
                yield raw
            finally:
                self._finish(context, status, usage, model, None)
        return gen()

    def _stream_chunks(self, response: Any, context: _RequestContext, status: int) -> Iterator[bytes]:
        tracker = _StreamUsageTracker()

        def gen() -> Iterator[bytes]:
            try:
                while True:
                    line = response.readline()
                    if not line:
                        break
                    tracker.feed(line)
                    yield line
            except (TimeoutError, socket.timeout):
                tracker.error = providers.ProviderTimeout("timeout", "upstream stream timed out")
                raise
            except OSError:
                tracker.error = providers.ProviderUnavailable("stream_interrupted", "upstream stream was interrupted")
                raise
            finally:
                try:
                    response.close()
                except Exception:
                    pass
                self._finish(context, status, tracker.usage, tracker.model, tracker.error)
        return gen()

    def _finish(self, context: _RequestContext, status: int, usage: Optional[dict],
                model: Optional[str], error: Optional[providers.ProviderError]) -> None:
        """Write one inference span. Never raises: telemetry cannot break a response."""
        if not context.record:
            return
        try:
            duration_ms = round((time.perf_counter() - context.started) * 1000, 3)
            normalized = providers.normalize_openai_usage(usage or {})
            response = None if error is not None else providers.ProviderResponse(
                content="",
                provider=self.provider_name,
                model=model or context.request_model,
                duration_ms=duration_ms,
                input_tokens=normalized["input_tokens"],
                output_tokens=normalized["output_tokens"],
                cached_input_tokens=normalized["cached_input_tokens"],
                cache_miss_tokens=normalized["cache_miss_tokens"],
                reasoning_tokens=normalized["reasoning_tokens"],
                total_tokens=normalized["total_tokens"],
            )
            record = runtime.build_inference_span(
                provider=self.provider_name,
                role=self.role,
                model=model or context.request_model,
                duration_ms=duration_ms,
                trace_id=context.trace_id,
                run_id=context.run_id,
                span_id=str(uuid.uuid4()),
                response=response,
                error=error,
                attempt=1,
                prompt_prefix_hash=context.prompt_prefix_hash,
                limits_version=None,
                pricing_config=self.pricing,
            )
            if not runtime.write_span(record, self.span_output, enabled=self.record_telemetry):
                LOGGER.warning("gateway telemetry span was not written")
        except Exception as exc:  # pragma: no cover - defensive telemetry boundary
            LOGGER.warning("gateway telemetry failed: %s", type(exc).__name__)


class GatewayRequestHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler that relays through ``server.gateway``."""

    protocol_version = "HTTP/1.1"
    server_version = "local-ai-infra-gateway/0.1"
    sys_version = ""

    def do_POST(self) -> None:
        self._handle("POST")

    def do_GET(self) -> None:
        self._handle("GET")

    def do_HEAD(self) -> None:
        self._handle("HEAD")

    def log_message(self, fmt: str, *args: Any) -> None:
        if getattr(self.server, "gateway", None) is not None and self.server.gateway.debug:
            LOGGER.info("%s - %s", self.address_string(), fmt % args)

    def _handle(self, method: str) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path not in ALLOWED_PATHS:
            self._write_buffer(404, b'{"error":{"message":"resource not found","type":"gateway_error"}}')
            return
        try:
            body = self._read_body()
        except (ValueError, OSError):
            self._write_buffer(400, b'{"error":{"message":"invalid request body","type":"gateway_error"}}')
            return
        try:
            forwarded = self.server.gateway.forward(method, self.path, list(self.headers.items()), body)
        except Exception as exc:
            LOGGER.warning("gateway forwarding failed: %s", type(exc).__name__)
            self._write_buffer(502, b'{"error":{"message":"gateway_error","type":"gateway_error"}}')
            return
        self.send_response(forwarded.status)
        for name, value in forwarded.headers:
            self.send_header(name, value)
        if forwarded.streamed:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(forwarded.content_length or 0))
        self.end_headers()
        self._relay(method, forwarded)

    def _relay(self, method: str, forwarded: ForwardedResponse) -> None:
        try:
            if method == "HEAD":
                return
            if forwarded.streamed:
                for chunk in forwarded.chunks:
                    if chunk:
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.write(b"0\r\n\r\n")
            else:
                for chunk in forwarded.chunks:
                    if chunk:
                        self.wfile.write(chunk)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            close = getattr(forwarded.chunks, "close", None)
            if callable(close):
                close()

    def _read_body(self) -> bytes:
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            return self._read_chunked()
        length = self.headers.get("Content-Length")
        if length is None:
            return b""
        size = int(length)
        return self.rfile.read(size) if size > 0 else b""

    def _read_chunked(self) -> bytes:
        chunks: list = []
        while True:
            line = self.rfile.readline().strip()
            if b";" in line:
                line = line.split(b";", 1)[0]
            if not line:
                break
            size = int(line, 16)
            if size == 0:
                self.rfile.readline()
                break
            chunks.append(self.rfile.read(size))
            self.rfile.readline()
        return b"".join(chunks)

    def _write_buffer(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", JSON_CONTENT_TYPE)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
                self.wfile.flush()
            except OSError:
                pass


def create_server(gateway: Gateway, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """Build (but do not start) a threaded HTTP server bound to one gateway."""
    server = ThreadingHTTPServer((host, port), GatewayRequestHandler)
    server.daemon_threads = True
    server.gateway = gateway
    return server
