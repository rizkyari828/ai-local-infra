#!/usr/bin/env python3
"""Shared stdlib-only helpers for local-ai-infra v0.1."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable

ROOT = pathlib.Path(__file__).resolve().parents[2]
PROFILE_DIR = ROOT / "profiles"
QWEN_PATH = pathlib.Path(os.environ.get("LAI_QWEN_SETTINGS", "~/.qwen/settings.json")).expanduser()
OLLAMA_PATH = pathlib.Path(os.environ.get("LAI_OLLAMA_OVERRIDE", "/etc/systemd/system/ollama.service.d/override.conf"))
STATE_DIR = pathlib.Path(os.environ.get("LAI_STATE_DIR", "~/.local/state/local-ai-infra")).expanduser()
ACTIVE_PATH = STATE_DIR / "active-profile.json"
BACKUP_DIR = STATE_DIR / "backups"
MANAGED_ENV = (
    "OLLAMA_CONTEXT_LENGTH",
    "OLLAMA_FLASH_ATTENTION",
    "OLLAMA_KV_CACHE_TYPE",
    "OLLAMA_NUM_PARALLEL",
    "OLLAMA_MAX_LOADED_MODELS",
)
HEALTH_READY_TIMEOUT_SECONDS = 30.0
HEALTH_READY_INTERVAL_SECONDS = 0.5


class InfraError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def load_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise InfraError(f"Missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise InfraError(f"Invalid JSON/YAML subset in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InfraError(f"Expected an object in {path}")
    return value


def atomic_json(path: pathlib.Path, value: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = pathlib.Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_restore(source: pathlib.Path, destination: pathlib.Path) -> None:
    """Restore exact backed-up bytes with an atomic same-directory replace."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    os.close(fd)
    tmp = pathlib.Path(raw)
    try:
        shutil.copy2(source, tmp)
        os.replace(tmp, destination)
    finally:
        tmp.unlink(missing_ok=True)


def profile_path(profile_id: str) -> pathlib.Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", profile_id):
        raise InfraError(f"Invalid profile id: {profile_id}")
    return PROFILE_DIR / f"{profile_id}.yaml"


def validate_profile(profile: dict[str, Any]) -> None:
    required = {"schema_version", "id", "version", "stability", "model", "ollama", "qwen"}
    missing = sorted(required - profile.keys())
    if missing:
        raise InfraError(f"Profile missing fields: {', '.join(missing)}")
    if profile["schema_version"] != 1:
        raise InfraError("Unsupported profile schema_version")
    if profile["stability"] not in {"experimental", "candidate", "stable"}:
        raise InfraError("Invalid profile stability")
    env = profile.get("ollama", {}).get("environment", {})
    if set(env) != set(MANAGED_ENV):
        raise InfraError("Ollama environment must define exactly the managed v0.1 keys")
    if not all(isinstance(env[key], str) and env[key] for key in MANAGED_ENV):
        raise InfraError("Ollama environment values must be non-empty strings")
    qwen = profile["qwen"]
    if qwen.get("manage_developer_client"):
        for key in ("provider", "base_url", "model", "context_window", "memory"):
            if key not in qwen:
                raise InfraError(f"Managed Qwen profile missing {key}")
        if profile["model"]["id"] != qwen["model"]:
            raise InfraError("Coding profile model and Qwen model must match")


def load_profile(profile_id: str) -> dict[str, Any]:
    profile = load_json(profile_path(profile_id))
    validate_profile(profile)
    if profile["id"] != profile_id:
        raise InfraError("Profile id does not match filename")
    return profile


def render_ollama(profile: dict[str, Any]) -> str:
    lines = ["[Service]"]
    for key in MANAGED_ENV:
        value = profile["ollama"]["environment"][key]
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
            raise InfraError(f"Unsafe Ollama value for {key}")
        lines.append(f'Environment="{key}={value}"')
    return "\n".join(lines) + "\n"


def desired_qwen(current: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    qwen = profile["qwen"]
    if not qwen.get("manage_developer_client"):
        return current
    result = json.loads(json.dumps(current))
    result.setdefault("model", {})
    result["model"].update({"name": qwen["model"], "baseUrl": qwen["base_url"]})
    result.setdefault("memory", {})
    result["memory"].update(qwen["memory"])
    providers = result.setdefault("modelProviders", {}).setdefault(qwen["provider"], [])
    match = next((item for item in providers if item.get("id") == qwen["model"]), None)
    if match is None:
        match = {
            "id": qwen["model"],
            "name": qwen["model"],
            "baseUrl": qwen["base_url"],
            "envKey": "OLLAMA_ANTHROPIC_KEY",
        }
        providers.append(match)
    match["baseUrl"] = qwen["base_url"]
    match.setdefault("generationConfig", {})["contextWindowSize"] = qwen["context_window"]
    return result


def parse_ollama_env(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in MANAGED_ENV:
        match = re.search(rf"(?:^|[\s\"]){re.escape(key)}=([^\s\"]+)", text, re.MULTILINE)
        if match:
            result[key] = match.group(1)
    return result


def actual_ollama_env() -> dict[str, str]:
    if os.environ.get("LAI_SKIP_SYSTEMD") == "1":
        return parse_ollama_env(OLLAMA_PATH.read_text(encoding="utf-8") if OLLAMA_PATH.exists() else "")
    try:
        result = subprocess.run(
            ["systemctl", "show", "ollama.service", "-p", "Environment", "--value"],
            check=True, capture_output=True, text=True,
        )
        return parse_ollama_env(result.stdout)
    except (OSError, subprocess.CalledProcessError):
        return parse_ollama_env(OLLAMA_PATH.read_text(encoding="utf-8") if OLLAMA_PATH.exists() else "")


def qwen_projection(settings: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    qwen = profile["qwen"]
    if not qwen.get("manage_developer_client"):
        return {"managed": False}
    provider = qwen["provider"]
    entries = settings.get("modelProviders", {}).get(provider, [])
    entry = next((item for item in entries if item.get("id") == qwen["model"]), {})
    memory = settings.get("memory", {})
    return {
        "managed": True,
        "model": settings.get("model", {}).get("name"),
        "base_url": settings.get("model", {}).get("baseUrl"),
        "provider_context": entry.get("generationConfig", {}).get("contextWindowSize"),
        "memory": {key: memory.get(key) for key in qwen["memory"]},
    }


def desired_projection(profile: dict[str, Any]) -> dict[str, Any]:
    qwen = profile["qwen"]
    if not qwen.get("manage_developer_client"):
        return {"managed": False}
    return {
        "managed": True,
        "model": qwen["model"],
        "base_url": qwen["base_url"],
        "provider_context": qwen["context_window"],
        "memory": qwen["memory"],
    }


def compare(profile: dict[str, Any]) -> dict[str, Any]:
    desired_env = profile["ollama"]["environment"]
    actual_env = actual_ollama_env()
    qwen_current = load_json(QWEN_PATH) if QWEN_PATH.exists() else {}
    desired_q = desired_projection(profile)
    actual_q = qwen_projection(qwen_current, profile)
    ollama_status = "MATCH" if actual_env == desired_env else ("UNKNOWN" if not actual_env else "DRIFT")
    qwen_status = "MATCH" if actual_q == desired_q else ("UNKNOWN" if not QWEN_PATH.exists() else "DRIFT")
    overall = "MATCH" if ollama_status == qwen_status == "MATCH" else (
        "UNKNOWN" if "UNKNOWN" in {ollama_status, qwen_status} else "DRIFT"
    )
    return {
        "overall": overall,
        "ollama": {"status": ollama_status, "desired": desired_env, "actual": actual_env},
        "qwen": {"status": qwen_status, "desired": desired_q, "actual": actual_q},
    }


def active_marker() -> dict[str, Any] | None:
    try:
        return load_json(ACTIVE_PATH)
    except InfraError:
        return None


def backup(changed: list[pathlib.Path], profile: dict[str, Any]) -> pathlib.Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_DIR, 0o700)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(BACKUP_DIR, 0o700)
    target = BACKUP_DIR / stamp
    target.mkdir(mode=0o700)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "created_at": utc_now(),
        "before_profile": active_marker(),
        "requested_profile": {"id": profile["id"], "version": profile["version"]},
        "known_good": False,
        "files": [],
    }
    for index, source in enumerate(changed):
        existed = source.exists()
        item = {"path": str(source), "existed": existed, "backup": None}
        if existed:
            destination = target / f"{index}-{source.name}"
            shutil.copy2(source, destination)
            os.chmod(destination, 0o600)
            item["backup"] = destination.name
        manifest["files"].append(item)
    atomic_json(target / "manifest.json", manifest)
    return target


def mark_known_good(path: pathlib.Path) -> None:
    manifest_path = path / "manifest.json"
    manifest = load_json(manifest_path)
    manifest["known_good"] = True
    atomic_json(manifest_path, manifest)


def run_privileged(args: list[str]) -> None:
    prefix = [] if os.geteuid() == 0 or os.environ.get("LAI_SKIP_SYSTEMD") == "1" else ["sudo"]
    subprocess.run(prefix + args, check=True)


def install_ollama(text: str) -> None:
    if os.environ.get("LAI_SKIP_SYSTEMD") == "1":
        OLLAMA_PATH.parent.mkdir(parents=True, exist_ok=True)
        OLLAMA_PATH.write_text(text, encoding="utf-8")
        return
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(text)
        temp_name = handle.name
    try:
        run_privileged(["install", "-D", "-m", "0644", temp_name, str(OLLAMA_PATH)])
    finally:
        pathlib.Path(temp_name).unlink(missing_ok=True)


def restart_ollama() -> None:
    if os.environ.get("LAI_SKIP_SYSTEMD") == "1":
        return
    run_privileged(["systemctl", "daemon-reload"])
    run_privileged(["systemctl", "restart", "ollama.service"])


def health_check(endpoint: str, timeout: float = 10.0) -> bool:
    if os.environ.get("LAI_SKIP_SYSTEMD") == "1":
        return True
    try:
        with urllib.request.urlopen(endpoint.rstrip("/") + "/api/tags", timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError):
        return False


def wait_for_health(
    endpoint: str,
    timeout: float = HEALTH_READY_TIMEOUT_SECONDS,
    interval: float = HEALTH_READY_INTERVAL_SECONDS,
    check: Callable[[str], bool] = health_check,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Poll the readiness predicate until healthy or the bounded deadline expires.

    Ollama may need several seconds after restart to finish GPU discovery, so a
    single immediate probe is not a reliable readiness signal. Retries use a short
    fixed backoff and stop at the deadline; a genuine startup failure still
    returns False so the caller can roll back.
    """
    deadline = clock() + max(0.0, timeout)
    while True:
        if check(endpoint):
            return True
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleep(min(max(0.0, interval), remaining))


def restore_backup(path: pathlib.Path, restart: bool = True) -> None:
    manifest = load_json(path / "manifest.json")
    system_changed = False
    for item in manifest["files"]:
        destination = pathlib.Path(item["path"])
        if str(destination) == str(OLLAMA_PATH):
            system_changed = True
            if item["existed"]:
                install_ollama((path / item["backup"]).read_text(encoding="utf-8"))
            elif os.environ.get("LAI_SKIP_SYSTEMD") == "1":
                destination.unlink(missing_ok=True)
            else:
                run_privileged(["rm", "-f", str(destination)])
        elif item["existed"]:
            atomic_restore(path / item["backup"], destination)
        else:
            destination.unlink(missing_ok=True)
    before = manifest.get("before_profile")
    if before:
        atomic_json(ACTIVE_PATH, before)
    else:
        ACTIVE_PATH.unlink(missing_ok=True)
    if system_changed and restart:
        restart_ollama()


def latest_backup() -> pathlib.Path:
    candidates = sorted((path.parent for path in BACKUP_DIR.glob("*/manifest.json")), reverse=True)
    if not candidates:
        raise InfraError("No backup is available")
    return candidates[0]


def loaded_models(endpoint: str) -> list[str]:
    try:
        with urllib.request.urlopen(endpoint.rstrip("/") + "/api/ps", timeout=3) as response:
            data = json.load(response)
        return [item.get("name", "") for item in data.get("models", []) if item.get("name")]
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return []


def resource_conflicts(profile: dict[str, Any]) -> list[str]:
    if not profile["model"].get("heavy"):
        return []
    if os.environ.get("LAI_SKIP_SYSTEMD") == "1":
        # No service restart or `ollama stop` is performed in test mode, so do not
        # probe a real endpoint; keeps unit tests independent of a live Ollama.
        return []
    target = profile["model"]["id"]
    return [name for name in loaded_models(profile["ollama"]["endpoint"]) if name != target]


def append_jsonl(path: pathlib.Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n")


def error_fingerprint(client: str, model: str, operation: str, category: str, code: str) -> str:
    stable = "\x1f".join((client, model, operation, category, code)).lower()
    return "sha256:" + hashlib.sha256(stable.encode()).hexdigest()[:24]


def base_event(**values: Any) -> dict[str, Any]:
    event = {
        "schema_version": "1.0.0",
        "run_id": values.pop("run_id", str(uuid.uuid4())),
        "event_id": values.pop("event_id", str(uuid.uuid4())),
        "timestamp_utc": values.pop("timestamp_utc", utc_now()),
        "event_type": values.pop("event_type", "run_summary"),
        "project": values.pop("project", None),
        "agent_client": values.pop("agent_client", None),
        "model": values.pop("model", None),
        "model_digest": values.pop("model_digest", None),
        "profile": values.pop("profile", None),
        "operation_tool": values.pop("operation_tool", None),
        "task_type": values.pop("task_type", None),
        "duration_ms": values.pop("duration_ms", None),
        "input_tokens": values.pop("input_tokens", None),
        "output_tokens": values.pop("output_tokens", None),
        "context_configured": values.pop("context_configured", None),
        "context_used": values.pop("context_used", None),
        "prompt_tokens_per_second": values.pop("prompt_tokens_per_second", None),
        "generation_tokens_per_second": values.pop("generation_tokens_per_second", None),
        "tool_call_count": values.pop("tool_call_count", None),
        "status": values.pop("status", None),
        "error_category": values.pop("error_category", None),
        "error_code": values.pop("error_code", None),
        "error_fingerprint": values.pop("error_fingerprint", None),
        "retry_count": values.pop("retry_count", 0),
        "eval_suite": values.pop("eval_suite", None),
        "eval_version": values.pop("eval_version", None),
        "eval_score": values.pop("eval_score", None),
        "structured_output_compliant": values.pop("structured_output_compliant", None),
        "test_status": values.pop("test_status", None),
        "review_status": values.pop("review_status", "unreviewed"),
        "profile_version": values.pop("profile_version", None),
        "ollama_version": values.pop("ollama_version", None),
        "client_version": values.pop("client_version", None),
        "hardware_profile": values.pop("hardware_profile", None),
        "config_version": values.pop("config_version", None),
    }
    if values:
        raise InfraError(f"Unknown telemetry fields: {', '.join(sorted(values))}")
    return event
