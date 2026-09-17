#!/usr/bin/env python3
"""Model provider abstraction for the Agent Runtime (stdlib only).

A provider is the only place that talks to an inference service. v0.1 ships:

- ``OllamaProvider`` for an independent Ollama service reached over localhost;
- ``FakeModelProvider`` for deterministic tests, so the normal suite never
  requires a live model.

Providers return only metadata the service actually reports. Absent token or
context values stay ``None`` and are never fabricated. The runtime never embeds
weights, manages GPUs, or reimplements inference; those are inference-runtime
responsibilities.

Classification is delegated to the runtime, but provider failures already carry
a runtime error category (see ``telemetry.RUNTIME_ERROR_CATEGORIES``) so provider
and environment problems can never be misread as model reasoning failures.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import infra
import learning
import telemetry

FAKE_SCENARIOS = (
    "success",
    "no_token_metadata",
    "timeout",
    "provider_error",
    "unavailable",
    "invalid_response",
    "model_missing",
    "cancelled",
)
HEALTH_STATUSES = ("healthy", "unreachable", "model_missing", "misconfigured")

DEFAULT_HEALTH_TIMEOUT_SECONDS = 5.0

DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-flash"
DEEPSEEK_REASONING_PROFILES = ("minimal", "low", "medium", "high")
DEEPSEEK_DEFAULT_REASONING_PROFILE = "high"
DEEPSEEK_GENERATION_KEYS = (
    "temperature",
    "top_p",
    "max_tokens",
    "stop",
    "frequency_penalty",
    "presence_penalty",
    "reasoning_effort",
)

ERROR_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _safe_code(value: Any) -> str:
    text = str(value) if value else "error"
    return text if ERROR_CODE_PATTERN.match(text) else "error"


def _safe_detail(value: Any, limit: int = 200) -> Optional[str]:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text or learning.contains_secret_like(text):
        return None
    return text[:limit]


def _optional_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


class ProviderError(Exception):
    """A classified provider/runtime failure.

    ``category`` is always one of ``telemetry.RUNTIME_ERROR_CATEGORIES``.
    ``code`` is a short provider-neutral token. Neither the exception message
    nor ``detail`` may contain prompts, credentials, or raw response bodies.
    """

    category = "unknown"
    retryable = False
    default_code = "error"

    def __init__(
        self,
        code: Optional[str] = None,
        detail: Optional[str] = None,
        *,
        category: Optional[str] = None,
        retryable: Optional[bool] = None,
    ) -> None:
        if category is not None:
            self.category = category if category in telemetry.RUNTIME_ERROR_CATEGORIES else "unknown"
        if retryable is not None:
            self.retryable = bool(retryable)
        self.code = _safe_code(code or self.default_code)
        self.detail = _safe_detail(detail)
        super().__init__("%s:%s" % (self.category, self.code))


class ProviderConfigurationError(ProviderError):
    category = "configuration_failure"
    default_code = "configuration_error"


class ProviderValidationError(ProviderError):
    category = "validation_failure"
    default_code = "invalid_request"


class ProviderTimeout(ProviderError):
    category = "provider_timeout"
    retryable = True
    default_code = "timeout"


class ProviderUnavailable(ProviderError):
    category = "provider_failure"
    retryable = True
    default_code = "connection_error"


class ProviderResponseError(ProviderError):
    category = "provider_failure"
    retryable = True
    default_code = "invalid_response"


class ProviderModelMissing(ProviderError):
    category = "configuration_failure"
    retryable = False
    default_code = "model_missing"


class ProviderCancelled(ProviderError):
    category = "user_cancelled"
    retryable = False
    default_code = "cancelled"


@dataclass
class ProviderRequest:
    """One provider call. Cancellation is cooperative via a shared Event."""

    prompt: str
    model: Optional[str] = None
    timeout_seconds: Optional[float] = None
    options: dict = field(default_factory=dict)
    keep_alive: Optional[str] = None
    cancel: Optional[threading.Event] = None


@dataclass
class ProviderResponse:
    """Provider-reported result. Unavailable fields stay ``None``."""

    content: str
    provider: str
    model: Optional[str] = None
    duration_ms: Optional[float] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cached_input_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    context_size: Optional[int] = None
    context_utilization: Optional[float] = None
    finish_reason: Optional[str] = None


@dataclass
class ProviderHealth:
    """Structured readiness status for a configured provider endpoint."""

    provider: str
    status: str
    model: Optional[str] = None
    base_url: Optional[str] = None
    detail: Optional[str] = None
    checked_at: str = field(default_factory=infra.utc_now)

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "status": self.status,
            "model": self.model,
            "base_url": self.base_url,
            "detail": self.detail,
            "checked_at": self.checked_at,
        }


class ModelProvider:
    """Minimal provider contract: execute one request, report readiness."""

    name = "base"

    @property
    def model(self) -> Optional[str]:
        return None

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        raise NotImplementedError

    def health_check(self) -> ProviderHealth:
        raise NotImplementedError

    def describe(self) -> dict:
        return {"provider": self.name, "model": self.model}


class FakeModelProvider(ModelProvider):
    """Deterministic provider for tests; performs no real inference.

    Scenarios cover success, missing token metadata, timeout, provider error,
    unavailable service, malformed response, missing model, and cancellation.
    """

    name = "fake"

    def __init__(
        self,
        model: str = "fake-model",
        scenario: str = "success",
        content: str = "FAKE RESPONSE",
        delay_seconds: float = 0.0,
        health: str = "healthy",
        token_metadata: Optional[bool] = None,
    ) -> None:
        if scenario not in FAKE_SCENARIOS:
            raise ProviderConfigurationError("unknown_scenario", "fake scenario must be one of the supported set")
        if health not in HEALTH_STATUSES:
            raise ProviderConfigurationError("unknown_health", "fake health must be a supported status")
        self._model = model
        self.scenario = scenario
        self.content = content
        self.delay_seconds = max(0.0, float(delay_seconds))
        self.health = health
        if token_metadata is None:
            token_metadata = scenario != "no_token_metadata"
        self.token_metadata = token_metadata
        self.calls: list = []
        self.health_checks = 0

    @property
    def model(self) -> Optional[str]:
        return self._model

    def describe(self) -> dict:
        return {
            "provider": self.name,
            "model": self._model,
            "scenario": self.scenario,
            "deterministic": True,
        }

    def _wait(self, request: ProviderRequest) -> None:
        deadline = time.monotonic() + self.delay_seconds
        while True:
            if request.cancel is not None and request.cancel.is_set():
                raise ProviderCancelled("cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.02, remaining))

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        self.calls.append(request)
        if request.cancel is not None and request.cancel.is_set():
            raise ProviderCancelled("cancelled")
        if self.scenario == "cancelled":
            raise ProviderCancelled("cancelled")
        if self.scenario == "unavailable":
            raise ProviderUnavailable("connection_error", "fake provider endpoint is unreachable")
        if self.scenario == "provider_error":
            raise ProviderError("fake_error", "deterministic fake provider error",
                                category="provider_failure", retryable=False)
        if self.scenario == "invalid_response":
            raise ProviderResponseError("invalid_response", "fake provider returned a malformed response")
        if self.scenario == "model_missing":
            raise ProviderModelMissing("model_missing", "configured model is not available")
        if self.scenario == "timeout":
            self._wait(request)
            raise ProviderTimeout("timeout", "fake provider exceeded the configured timeout")
        self._wait(request)
        content = self.content
        if content is None or not str(content).strip():
            raise ProviderResponseError("invalid_response", "fake provider returned an empty response")
        return ProviderResponse(
            content=str(content),
            provider=self.name,
            model=request.model or self._model,
            duration_ms=0.0,
            input_tokens=12 if self.token_metadata else None,
            output_tokens=7 if self.token_metadata else None,
            reasoning_tokens=3 if self.token_metadata else None,
            context_size=None,
            context_utilization=None,
            finish_reason="stop" if self.token_metadata else None,
        )

    def health_check(self) -> ProviderHealth:
        self.health_checks += 1
        return ProviderHealth(provider=self.name, status=self.health, model=self._model)


def _validate_generation(options: Any) -> dict:
    if options is None:
        return {}
    if not isinstance(options, dict):
        raise ProviderConfigurationError("invalid_options", "generation options must be an object")
    clean = {}
    for key, value in options.items():
        if not isinstance(key, str) or not key:
            raise ProviderConfigurationError("invalid_options", "generation option names must be strings")
        if isinstance(value, bool) or isinstance(value, (int, float)):
            clean[key] = value
        elif isinstance(value, str):
            if learning.contains_secret_like(value):
                raise ProviderConfigurationError("invalid_options", "generation option looks like a credential")
            clean[key] = value
        else:
            raise ProviderConfigurationError("invalid_options", "generation option values must be scalar")
    return clean


def validate_base_url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderConfigurationError("invalid_base_url", "base_url must be a non-empty string")
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ProviderConfigurationError("invalid_base_url", "base_url must be an http(s) URL with a host")
    if parsed.username or parsed.password:
        raise ProviderConfigurationError("invalid_base_url", "base_url must not contain credentials")
    return value.strip().rstrip("/")


class OllamaProvider(ModelProvider):
    """Talk to an independent Ollama service over HTTP.

    The provider only assumes a reachable HTTP endpoint; it has no OS, GPU, or
    platform assumptions. ``opener`` is injectable so tests never need a live
    service.
    """

    name = "ollama"

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        options: Optional[dict] = None,
        keep_alive: Optional[str] = None,
        health_timeout_seconds: float = DEFAULT_HEALTH_TIMEOUT_SECONDS,
        opener: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.base_url = validate_base_url(base_url)
        self._model = model
        self.timeout_seconds = timeout_seconds
        self.options = _validate_generation(options or {})
        self.keep_alive = keep_alive
        self.health_timeout_seconds = health_timeout_seconds
        self._opener = opener or urllib.request.urlopen

    @property
    def model(self) -> Optional[str]:
        return self._model

    def describe(self) -> dict:
        return {
            "provider": self.name,
            "model": self._model,
            "base_url": self.base_url,
            "default_timeout_seconds": self.timeout_seconds,
            "keep_alive": self.keep_alive,
        }

    def _open(self, request: Any, timeout: Optional[float]) -> Any:
        if timeout is None:
            return self._opener(request)
        return self._opener(request, timeout=timeout)

    def _translate_http_error(self, status: int, body: bytes) -> ProviderError:
        message = ""
        try:
            payload = json.loads(body.decode("utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("error"), str):
                message = payload["error"]
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            message = ""
        if status == 404 or "not found" in message.lower() or "try pulling" in message.lower():
            return ProviderModelMissing("model_missing", "configured model is not available on the endpoint")
        if status in (401, 403):
            return ProviderConfigurationError("auth_error", "endpoint rejected the request credentials")
        if status >= 500:
            return ProviderUnavailable("http_%d" % status, "endpoint returned a server error")
        return ProviderResponseError("http_%d" % status, "endpoint rejected the request", retryable=False)

    def _request(self, path: str, payload: Optional[dict], timeout: Optional[float]) -> dict:
        url = self.base_url + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST" if data else "GET"
        )
        try:
            with self._open(request, timeout) as response:
                status = getattr(response, "status", 200)
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise self._translate_http_error(exc.code, exc.read()) from None
        except urllib.error.URLError as exc:
            raise ProviderUnavailable("connection_error", "endpoint is unreachable") from None
        except (TimeoutError, socket.timeout):
            raise ProviderTimeout("timeout", "endpoint did not respond before the timeout") from None
        except OSError:
            raise ProviderUnavailable("connection_error", "endpoint could not be reached") from None
        if status != 200:
            raise self._translate_http_error(status, body)
        try:
            value = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ProviderResponseError("invalid_response", "endpoint returned a non-JSON body") from None
        if not isinstance(value, dict):
            raise ProviderResponseError("invalid_response", "endpoint returned an unexpected payload")
        return value

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        if not isinstance(request.prompt, str) or not request.prompt.strip():
            raise ProviderValidationError("empty_prompt", "prompt must be a non-empty string")
        if request.cancel is not None and request.cancel.is_set():
            raise ProviderCancelled("cancelled")
        model = request.model or self._model
        if not model:
            raise ProviderConfigurationError("model_not_configured", "no model is configured for the provider")
        options = dict(self.options)
        options.update(_validate_generation(request.options))
        payload: dict = {"model": model, "prompt": request.prompt, "stream": False}
        if options:
            payload["options"] = options
        keep_alive = request.keep_alive if request.keep_alive is not None else self.keep_alive
        if keep_alive:
            payload["keep_alive"] = keep_alive
        timeout = request.timeout_seconds or self.timeout_seconds
        started = time.perf_counter()
        raw = self._request("/api/generate", payload, timeout)
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        return self._parse(raw, model, duration_ms, options)

    def _parse(self, raw: dict, model: str, duration_ms: float, options: dict) -> ProviderResponse:
        content = raw.get("response")
        if not isinstance(content, str) or not content.strip():
            raise ProviderResponseError("invalid_response", "endpoint returned an empty response")
        input_tokens = _optional_int(raw.get("prompt_eval_count"))
        output_tokens = _optional_int(raw.get("eval_count"))
        reasoning_tokens = _optional_int(raw.get("thinking_eval_count"))
        context_size = _optional_int(options.get("num_ctx"))
        utilization = None
        if input_tokens is not None and context_size:
            utilization = round(min(input_tokens / context_size, 1.0), 4)
        finish_reason = raw.get("done_reason")
        if finish_reason is None and raw.get("done") is True:
            finish_reason = "stop"
        if not isinstance(finish_reason, str):
            finish_reason = None
        return ProviderResponse(
            content=content,
            provider=self.name,
            model=model,
            duration_ms=duration_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            context_size=context_size,
            context_utilization=utilization,
            finish_reason=finish_reason,
        )

    def health_check(self) -> ProviderHealth:
        if not self.base_url:
            return ProviderHealth(provider=self.name, status="misconfigured", model=self._model,
                                  detail="base_url is not configured")
        try:
            raw = self._request("/api/tags", None, self.health_timeout_seconds)
        except ProviderTimeout:
            return ProviderHealth(provider=self.name, status="unreachable", model=self._model,
                                  base_url=self.base_url, detail="timeout")
        except ProviderConfigurationError as exc:
            return ProviderHealth(provider=self.name, status="misconfigured", model=self._model,
                                  base_url=self.base_url, detail=exc.code)
        except ProviderError as exc:
            return ProviderHealth(provider=self.name, status="unreachable", model=self._model,
                                  base_url=self.base_url, detail=exc.code)
        names = [
            item.get("name")
            for item in raw.get("models", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]
        if self._model and not self._model_available(names):
            return ProviderHealth(provider=self.name, status="model_missing", model=self._model,
                                  base_url=self.base_url, detail="configured model is not installed")
        return ProviderHealth(provider=self.name, status="healthy", model=self._model, base_url=self.base_url)

    def _model_available(self, names: list) -> bool:
        configured = self._model or ""
        if not configured:
            return True
        if ":" in configured:
            return configured in names
        return any(name.split(":")[0] == configured for name in names)


class DeepSeekProvider(ModelProvider):
    """Talk to the hosted DeepSeek chat-completions API (OpenAI-compatible).

    Credentials come from an environment variable (``DEEPSEEK_API_KEY`` by
    default); they are never stored in configuration, returned by ``describe``,
    or written to telemetry. Only the request/response fields this project
    requires are read or sent, and unavailable metrics stay ``None``.
    """

    name = "deepseek"

    def __init__(
        self,
        base_url: str = DEEPSEEK_DEFAULT_BASE_URL,
        model: Optional[str] = DEEPSEEK_DEFAULT_MODEL,
        api_key: Optional[str] = None,
        api_key_env: str = DEEPSEEK_API_KEY_ENV,
        reasoning_profile: str = "high",
        timeout_seconds: Optional[float] = None,
        options: Optional[dict] = None,
        health_timeout_seconds: float = DEFAULT_HEALTH_TIMEOUT_SECONDS,
        opener: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.base_url = validate_base_url(base_url)
        self._model = model
        self.api_key_env = api_key_env if isinstance(api_key_env, str) and api_key_env else DEEPSEEK_API_KEY_ENV
        self._api_key = api_key if api_key is not None else os.environ.get(self.api_key_env)
        self.reasoning_profile = _deepseek_reasoning_profile(reasoning_profile)
        self.timeout_seconds = timeout_seconds
        self.options = _deepseek_options(options or {})
        self.health_timeout_seconds = health_timeout_seconds
        self._opener = opener or urllib.request.urlopen

    @property
    def model(self) -> Optional[str]:
        return self._model

    def has_api_key(self) -> bool:
        return bool(self._api_key)

    def describe(self) -> dict:
        return {
            "provider": self.name,
            "model": self._model,
            "base_url": self.base_url,
            "reasoning_profile": self.reasoning_profile,
            "has_api_key": self.has_api_key(),
            "default_timeout_seconds": self.timeout_seconds,
        }

    def _open(self, request: Any, timeout: Optional[float]) -> Any:
        if timeout is None:
            return self._opener(request)
        return self._opener(request, timeout=timeout)

    def _auth_headers(self) -> dict:
        return {"Content-Type": "application/json", "Authorization": "Bearer %s" % self._api_key}

    def _translate_http_error(self, status: int, body: bytes) -> ProviderError:
        detail = _deepseek_error_detail(body)
        if status in (401, 403):
            return ProviderConfigurationError("auth_error", detail or "hosted endpoint rejected the credentials")
        if status == 404:
            return ProviderModelMissing("model_missing", detail or "configured model is not available")
        if status == 429:
            return ProviderUnavailable("rate_limited", detail or "hosted endpoint rate limited the request")
        if status == 400:
            return ProviderValidationError("bad_request", detail or "hosted endpoint rejected the request")
        if status >= 500:
            return ProviderUnavailable("http_%d" % status, detail or "hosted endpoint returned a server error")
        return ProviderResponseError("http_%d" % status, detail or "hosted endpoint rejected the request",
                                     retryable=False)

    def _request(self, path: str, payload: Optional[dict], timeout: Optional[float]) -> dict:
        url = self.base_url + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url, data=data, headers=self._auth_headers(), method="POST" if data else "GET"
        )
        try:
            with self._open(request, timeout) as response:
                status = getattr(response, "status", 200)
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise self._translate_http_error(exc.code, exc.read()) from None
        except urllib.error.URLError:
            raise ProviderUnavailable("connection_error", "hosted endpoint is unreachable") from None
        except (TimeoutError, socket.timeout):
            raise ProviderTimeout("timeout", "hosted endpoint did not respond before the timeout") from None
        except OSError:
            raise ProviderUnavailable("connection_error", "hosted endpoint could not be reached") from None
        if status != 200:
            raise self._translate_http_error(status, body)
        try:
            value = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ProviderResponseError("invalid_response", "hosted endpoint returned a non-JSON body") from None
        if not isinstance(value, dict):
            raise ProviderResponseError("invalid_response", "hosted endpoint returned an unexpected payload")
        return value

    def execute(self, request: ProviderRequest) -> ProviderResponse:
        if not isinstance(request.prompt, str) or not request.prompt.strip():
            raise ProviderValidationError("empty_prompt", "prompt must be a non-empty string")
        if request.cancel is not None and request.cancel.is_set():
            raise ProviderCancelled("cancelled")
        model = request.model or self._model
        if not model:
            raise ProviderConfigurationError("model_not_configured", "no model is configured for the provider")
        if not self._api_key:
            raise ProviderConfigurationError("missing_api_key", "hosted provider credentials are not set")
        options = dict(self.options)
        options.update(_deepseek_options(request.options))
        options.setdefault("reasoning_effort", self.reasoning_profile)
        payload: dict = {
            "model": model,
            "messages": [{"role": "user", "content": request.prompt}],
            "stream": False,
        }
        for key in DEEPSEEK_GENERATION_KEYS:
            if key in options:
                payload[key] = options[key]
        timeout = request.timeout_seconds or self.timeout_seconds
        started = time.perf_counter()
        raw = self._request("/chat/completions", payload, timeout)
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        return self._parse(raw, model, duration_ms)

    def _parse(self, raw: dict, model: str, duration_ms: float) -> ProviderResponse:
        choices = raw.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderResponseError("invalid_response", "hosted endpoint returned no choices")
        first = choices[0]
        message = first.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ProviderResponseError("invalid_response", "hosted endpoint returned an empty response")
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
        finish_reason = first.get("finish_reason")
        reported_model = raw.get("model")
        return ProviderResponse(
            content=content,
            provider=self.name,
            model=reported_model if isinstance(reported_model, str) and reported_model else model,
            duration_ms=duration_ms,
            input_tokens=_optional_int(usage.get("prompt_tokens")),
            output_tokens=_optional_int(usage.get("completion_tokens")),
            cached_input_tokens=_optional_int(usage.get("prompt_cache_hit_tokens")),
            reasoning_tokens=_optional_int(details.get("reasoning_tokens")),
            context_size=None,
            context_utilization=None,
            finish_reason=finish_reason if isinstance(finish_reason, str) else None,
        )

    def health_check(self) -> ProviderHealth:
        if not self.has_api_key():
            return ProviderHealth(provider=self.name, status="misconfigured", model=self._model,
                                  base_url=self.base_url, detail="missing_api_key")
        try:
            self._request("/models", None, self.health_timeout_seconds)
        except ProviderConfigurationError as exc:
            return ProviderHealth(provider=self.name, status="misconfigured", model=self._model,
                                  base_url=self.base_url, detail=exc.code)
        except ProviderError as exc:
            return ProviderHealth(provider=self.name, status="unreachable", model=self._model,
                                  base_url=self.base_url, detail=exc.code)
        return ProviderHealth(provider=self.name, status="healthy", model=self._model, base_url=self.base_url)


def _deepseek_reasoning_profile(value: Any) -> str:
    """Normalize the configured reasoning profile to a supported effort level."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return DEEPSEEK_DEFAULT_REASONING_PROFILE
    if not isinstance(value, str) or value.strip().lower() not in DEEPSEEK_REASONING_PROFILES:
        raise ProviderConfigurationError(
            "invalid_reasoning_profile",
            "DeepSeek reasoning_profile must be one of: %s" % ", ".join(DEEPSEEK_REASONING_PROFILES))
    return value.strip().lower()


def _deepseek_error_detail(body: bytes) -> Optional[str]:
    """Extract a short, sanitized error message for diagnostics (never the key)."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None
    message = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            message = error["message"]
        elif isinstance(error, str):
            message = error
        elif isinstance(payload.get("message"), str):
            message = payload["message"]
    return _safe_detail(message) if message else None


def _deepseek_options(options: Any) -> dict:
    clean = _validate_generation(options)
    unknown = sorted(set(clean) - set(DEEPSEEK_GENERATION_KEYS))
    if unknown:
        raise ProviderConfigurationError("unsupported_option",
                                         "unsupported DeepSeek generation option: %s" % ", ".join(unknown))
    return clean


PROVIDER_BUILDERS: dict = {}


def register_provider(name: str, builder: Callable[[dict], ModelProvider]) -> None:
    """Register a provider builder so future hosted providers need no runtime edits."""
    PROVIDER_BUILDERS[name] = builder


def _build_ollama(entry: dict) -> ModelProvider:
    if "base_url" not in entry:
        raise ProviderConfigurationError("missing_base_url", "ollama provider requires base_url")
    return OllamaProvider(
        base_url=entry["base_url"],
        model=entry.get("model"),
        timeout_seconds=entry.get("timeout_seconds"),
        options=entry.get("options"),
        keep_alive=entry.get("keep_alive"),
    )


def _build_fake(entry: dict) -> ModelProvider:
    return FakeModelProvider(
        model=entry.get("model", "fake-model"),
        scenario=entry.get("scenario", "success"),
        content=entry.get("content", "FAKE RESPONSE"),
        delay_seconds=entry.get("delay_seconds", 0.0),
        health=entry.get("health", "healthy"),
    )


def _build_deepseek(entry: dict) -> ModelProvider:
    if "base_url" not in entry:
        raise ProviderConfigurationError("missing_base_url", "deepseek provider requires base_url")
    return DeepSeekProvider(
        base_url=entry["base_url"],
        model=entry.get("model"),
        api_key_env=entry.get("api_key_env", DEEPSEEK_API_KEY_ENV),
        reasoning_profile=entry.get("reasoning_profile", "high"),
        timeout_seconds=entry.get("timeout_seconds"),
        options=entry.get("options"),
    )


register_provider("ollama", _build_ollama)
register_provider("fake", _build_fake)
register_provider("deepseek", _build_deepseek)


DEFAULT_RUNTIME_CONFIG: dict = {
    "schema_version": 1,
    "runtime_version": "0.1.0",
    "providers_version": "0.1.0",
    "default_provider": "ollama",
    "providers": {
        "ollama": {
            "provider": "ollama",
            "base_url": "http://localhost:11434",
            "model": "qwen3.6:27b-coding",
            "keep_alive": "5m",
        },
        "deepseek": {
            "provider": "deepseek",
            "base_url": DEEPSEEK_DEFAULT_BASE_URL,
            "model": DEEPSEEK_DEFAULT_MODEL,
            "reasoning_profile": "high",
            "api_key_env": DEEPSEEK_API_KEY_ENV,
        },
        "fake": {"provider": "fake", "model": "fake-model", "scenario": "success"},
    },
    "escalation": {
        "enabled": False,
        "mode": "manual",
        "hosted_allowed": False,
        "fallback_provider": "deepseek",
        "eligible_categories": ["model_reasoning_failure", "validation_failure"],
        "max_depth": 1,
    },
}

ESCALATION_MODES = ("manual", "automatic")
MAX_ESCALATION_DEPTH = 1


def _validate_escalation(config: dict, providers_map: dict) -> None:
    section = config.get("escalation")
    if section is None:
        return
    if not isinstance(section, dict):
        raise ProviderConfigurationError("invalid_escalation", "escalation must be an object")
    unknown = sorted(set(section) - {
        "enabled", "mode", "hosted_allowed", "fallback_provider", "eligible_categories", "max_depth",
    })
    if unknown:
        raise ProviderConfigurationError("invalid_escalation", "escalation has unknown fields: %s" % ", ".join(unknown))
    for key in ("enabled", "hosted_allowed"):
        value = section.get(key)
        if value is not None and not isinstance(value, bool):
            raise ProviderConfigurationError("invalid_escalation", "escalation.%s must be true or false" % key)
    mode = section.get("mode", "manual")
    if mode not in ESCALATION_MODES:
        raise ProviderConfigurationError("invalid_escalation", "escalation.mode must be one of: %s" % ", ".join(ESCALATION_MODES))
    fallback = section.get("fallback_provider", "deepseek")
    if fallback not in providers_map:
        raise ProviderConfigurationError("invalid_escalation", "escalation.fallback_provider must be a configured provider")
    categories = section.get("eligible_categories")
    if categories is not None:
        if not isinstance(categories, list) or not categories:
            raise ProviderConfigurationError("invalid_escalation", "escalation.eligible_categories must be a non-empty list")
        for category in categories:
            if category not in telemetry.RUNTIME_ERROR_CATEGORIES:
                raise ProviderConfigurationError("invalid_escalation", "unknown eligible category: %s" % category)
    depth = section.get("max_depth", MAX_ESCALATION_DEPTH)
    if isinstance(depth, bool) or depth != MAX_ESCALATION_DEPTH:
        raise ProviderConfigurationError("invalid_escalation",
                                         "escalation.max_depth must be %d in this milestone" % MAX_ESCALATION_DEPTH)


def validate_runtime_config(config: Any) -> dict:
    if not isinstance(config, dict):
        raise ProviderConfigurationError("invalid_runtime_config", "runtime config must be an object")
    if config.get("schema_version") != 1:
        raise ProviderConfigurationError("unsupported_schema", "unsupported runtime config schema_version")
    providers = config.get("providers")
    if not isinstance(providers, dict) or not providers:
        raise ProviderConfigurationError("missing_providers", "runtime config must define providers")
    default = config.get("default_provider")
    if default not in providers:
        raise ProviderConfigurationError("invalid_default_provider", "default_provider must name a configured provider")
    for name, entry in providers.items():
        if not isinstance(entry, dict):
            raise ProviderConfigurationError("invalid_provider_entry", "provider '%s' must be an object" % name)
        if entry.get("provider") != name:
            raise ProviderConfigurationError("provider_name_mismatch", "provider '%s' has a mismatched provider field" % name)
        if name not in PROVIDER_BUILDERS:
            raise ProviderConfigurationError("unknown_provider", "provider '%s' has no registered builder" % name)
        model = entry.get("model")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise ProviderConfigurationError("invalid_model", "provider '%s' model must be a non-empty string" % name)
        for key, value in entry.items():
            if isinstance(value, str) and learning.contains_secret_like(value):
                raise ProviderConfigurationError("secret_in_config", "provider '%s' field '%s' looks like a credential" % (name, key))
        if name == "ollama":
            if "base_url" not in entry:
                raise ProviderConfigurationError("missing_base_url", "ollama provider requires base_url")
            validate_base_url(entry["base_url"])
        if name == "deepseek":
            if "base_url" not in entry:
                raise ProviderConfigurationError("missing_base_url", "deepseek provider requires base_url")
            validate_base_url(entry["base_url"])
            profile = entry.get("reasoning_profile")
            if profile is not None:
                _deepseek_reasoning_profile(profile)
            api_key_env = entry.get("api_key_env")
            if api_key_env is not None and (not isinstance(api_key_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env)):
                raise ProviderConfigurationError("invalid_api_key_env", "deepseek api_key_env must be an environment variable name")
    _validate_escalation(config, providers)
    return config


def load_runtime_config(path: Optional[Any] = None) -> dict:
    """Load and validate provider/runtime configuration, falling back to defaults."""
    if path is None:
        path = infra.ROOT / "config/runtime.yaml"
    path = pathlib.Path(path)
    if not path.exists():
        return json.loads(json.dumps(DEFAULT_RUNTIME_CONFIG))
    return validate_runtime_config(infra.load_json(path))


def build_provider(config: dict, provider_name: Optional[str] = None) -> ModelProvider:
    validate_runtime_config(config)
    name = provider_name or config.get("default_provider")
    entry = config["providers"].get(name)
    if entry is None:
        raise ProviderConfigurationError("unknown_provider", "provider '%s' is not configured" % name)
    builder = PROVIDER_BUILDERS.get(entry.get("provider"))
    if builder is None:
        raise ProviderConfigurationError("unknown_provider", "provider '%s' has no builder" % name)
    return builder(entry)


def new_span_id() -> str:
    return str(uuid.uuid4())
