#!/usr/bin/env python3
"""Read-only runtime readiness doctor (V0.1).

Answers "is this machine ready for AI coding right now?" without mutating the
runtime: no apply, no backups, no graph rebuild, no process changes, no
agent/Codex task launches. It reports only.

Version and capability expectations come from the declarative, versioned tested
baseline (``config/tool-baseline.yaml``). The baseline records what was verified;
it is not a hard pin and is never enforced here. Exact version match is PASS; any
difference is WARN (no auto upgrade/downgrade). Capability flags in the baseline
are declarative verification, never live runtime truth: doctor reports the
capability as verified-by-baseline and the current probe separately, and never
claims runtime AUTO from config alone.

Machine identity is explicit via LAI_MACHINE / --machine; doctor never guesses.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

import bootstrap  # scripts/common, on sys.path

ROOT = bootstrap.ROOT
BASELINE_PATH = ROOT / "config/tool-baseline.yaml"

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
SKIP = "SKIP"

VERSION_RE = re.compile(r"\d+\.\d+\.\d+")

# Doctor diagnostic states are deliberately separate from bootstrap drift states
# (ALIGNED / DRIFTED / MISSING / LOCAL-OVERRIDE / STALE / BLOCKED).
FRESHNESS_TO_STATUS = {"CURRENT": PASS, "STALE": WARN, "BLOCKED": FAIL}


@dataclass(frozen=True)
class Check:
    component: str
    check: str
    status: str
    reason: str
    category: str = "core"
    observed: Optional[str] = None
    expected: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "check": self.check,
            "status": self.status,
            "reason": self.reason,
            "category": self.category,
            "observed": self.observed,
            "expected": self.expected,
        }


# --------------------------------------------------------------------------- #
# Low-level bounded probes (read-only)
# --------------------------------------------------------------------------- #


def parse_version(text: str | None) -> str | None:
    match = VERSION_RE.search(text or "")
    return match.group(0) if match else None


def _ver_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def probe_version(binary: str, args: list[str], timeout: float) -> tuple[str | None, str | None, str | None]:
    """Resolve ``binary`` and run a side-effect-free probe. Returns (path, version, raw)."""
    path = shutil.which(binary)
    if not path:
        return None, None, None
    try:
        result = subprocess.run([path, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return path, None, None
    text = (result.stdout or result.stderr or "").strip()
    raw = text.splitlines()[0] if text else None
    return path, parse_version(text), raw


def http_health(host: str, port: int, timeout: float) -> bool:
    """Bounded, read-only GET of the loopback health endpoint. Never starts anything."""
    if not port:
        return False
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def load_baseline(path: pathlib.Path = BASELINE_PATH) -> dict[str, Any]:
    try:
        baseline = bootstrap.load_json(path)
    except bootstrap.infra.InfraError as exc:
        raise bootstrap.BootstrapError(f"tool baseline unreadable: {exc}") from exc
    if baseline.get("schema_version") != 1:
        raise bootstrap.BootstrapError("unsupported tool baseline schema_version")
    return baseline


# --------------------------------------------------------------------------- #
# Check helpers
# --------------------------------------------------------------------------- #


def _required(components: dict[str, Any], name: str) -> bool:
    entry = components.get(name) or {}
    return bool(entry.get("required", True))


def _verified_version(base: dict[str, Any], machine: str | None) -> str | None:
    """Per-machine verified version, falling back to the default ``verified_version``.

    Versions legitimately differ across machines, so the baseline may carry a
    ``per_machine`` override keyed by machine id. Capabilities stay separate.
    """
    per_machine = base.get("per_machine") or {}
    if machine and machine in per_machine:
        return per_machine[machine]
    return base.get("verified_version")


def _version_check(component: str, version: str | None, verified: str | None, raw: str | None) -> Check:
    if verified is None:
        status = PASS if version else WARN
        return Check(component, "version", status, "version observed" if version else "version unparseable",
                     "tools", observed=version or raw)
    if version is None:
        return Check(component, "version", WARN, "version unparseable from probe output", "tools",
                     observed=raw, expected=verified)
    if version == verified:
        return Check(component, "version", PASS, "verified baseline version", "tools",
                     observed=version, expected=verified)
    try:
        newer = _ver_tuple(version) > _ver_tuple(verified)
    except ValueError:
        newer = False
    reason = "unverified newer version" if newer else "version differs from verified baseline"
    return Check(component, "version", WARN, reason, "tools", observed=version, expected=verified)


def _tool_binary_version(component: str, base: dict[str, Any], required: bool, timeout: float,
                         binary: str | None = None,
                         machine: str | None = None) -> tuple[list[Check], str | None, str | None]:
    binary = binary or base.get("binary") or component
    probe = base.get("probe") or ["--version"]
    verified = _verified_version(base, machine)
    path, version, raw = probe_version(binary, probe, timeout)
    if path is None:
        status = FAIL if required else SKIP
        reason = "required binary not found" if required else "optional binary not found"
        return ([Check(component, "binary", status, reason, "tools"),
                 Check(component, "version", status, "version unavailable", "tools",
                       expected=verified)],
                None, None)
    checks = [Check(component, "binary", PASS, "binary resolvable", "tools", observed=path),
              _version_check(component, version, verified, raw)]
    return checks, path, version


# --------------------------------------------------------------------------- #
# Component checks
# --------------------------------------------------------------------------- #


def _repo_checks(ctx: dict[str, Any]) -> list[Check]:
    repository = ctx.get("repository") or {}
    root = repository.get("root")
    checks: list[Check] = []
    if not root:
        return [Check("repository", "git", FAIL, "repository root unresolved", "core")]
    head = repository.get("head") or bootstrap._git_in(root, "rev-parse", "HEAD")
    checks.append(Check("repository", "head", PASS if head else FAIL,
                        "HEAD resolvable" if head else "HEAD unresolvable", "core",
                        observed=head[:12] if head else None))
    branch = bootstrap._git_in(root, "rev-parse", "--abbrev-ref", "HEAD")
    checks.append(Check("repository", "branch", PASS if branch else WARN,
                        "branch resolved" if branch else "branch unresolved", "core", observed=branch))
    try:
        dirty = bool(bootstrap.worktree_changes(root))
    except bootstrap.BootstrapError as exc:
        dirty = True
        checks.append(Check("repository", "worktree", WARN, f"worktree unreadable: {exc}", "core"))
    else:
        checks.append(Check("repository", "worktree", WARN if dirty else PASS,
                            "working tree dirty" if dirty else "working tree clean", "core"))
    upstream = bootstrap._git_in(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if not upstream:
        checks.append(Check("repository", "remote", SKIP, "no upstream tracking branch configured", "core"))
    else:
        counts = bootstrap._git_in(root, "rev-list", "--left-right", "--count", f"@{upstream}...HEAD")
        behind = ahead = 0
        if counts:
            try:
                behind, ahead = (int(part) for part in counts.split()[:2])
            except ValueError:
                pass
        status = PASS if ahead == 0 and behind == 0 else WARN
        checks.append(Check("repository", "remote", status,
                            f"ahead {ahead}, behind {behind} of {upstream}", "core", observed=upstream))
    return checks


def _bootstrap_checks(env: dict[str, str], ctx: dict[str, Any]) -> list[Check]:
    checks: list[Check] = []
    try:
        plan, _ = bootstrap.build_plan(env)
    except bootstrap.BootstrapError as exc:
        checks.append(Check("bootstrap", "managed-state", FAIL, f"plan failed: {exc}", "core"))
    else:
        blocked = [unit for unit in plan if unit.get("hard")]
        checks.append(Check("bootstrap", "managed-state", FAIL if blocked else PASS,
                            f"{len(blocked)} blocked managed unit(s)" if blocked else "managed state aligned",
                            "core"))
    findings = bootstrap.secret_scan.scan_paths(bootstrap.managed_source_paths())
    checks.append(Check("bootstrap", "secrets", FAIL if findings else PASS,
                        f"{len(findings)} potential secret(s) in managed sources" if findings
                        else "no obvious secrets in managed sources", "core"))
    return checks


def _opencode_checks(ctx: dict[str, Any], base: dict[str, Any], comps: dict[str, Any],
                     timeout: float) -> list[Check]:
    required = _required(comps, "opencode")
    checks, _path, _version = _tool_binary_version("opencode", base, required, timeout,
                                                   machine=ctx.get("machine_id"))
    bin_cfg = ctx.get("bin") or {}
    wrapper = bin_cfg.get(base.get("wrapper", "oc")) or bin_cfg.get("oc")
    if wrapper and pathlib.Path(wrapper).exists():
        checks.append(Check("opencode", "wrapper", PASS, "local wrapper present", "integrations",
                            observed=wrapper))
    elif not required:
        checks.append(Check("opencode", "wrapper", SKIP, "component not required", "integrations"))
    else:
        checks.append(Check("opencode", "wrapper", WARN, "expected local wrapper absent",
                            "integrations", expected=wrapper))
    return checks


def _codex_checks(ctx: dict[str, Any], base: dict[str, Any], comps: dict[str, Any],
                  timeout: float) -> list[Check]:
    required = _required(comps, "codex")
    checks, _path, _version = _tool_binary_version("codex", base, required, timeout,
                                                   machine=ctx.get("machine_id"))
    codex = ctx.get("codex") or {}
    paths = [p for p in (codex.get("agents_path"), codex.get("config_path")) if p]
    missing = [p for p in paths if not pathlib.Path(p).is_file()]
    if not paths:
        checks.append(Check("codex", "config", SKIP, "no codex config paths declared", "integrations"))
    elif not missing:
        checks.append(Check("codex", "config", PASS, "codex config paths readable", "integrations"))
    else:
        checks.append(Check("codex", "config", WARN if required else SKIP,
                            f"{len(missing)} codex config path(s) unreadable", "integrations"))
    return checks


def _rtk_checks(ctx: dict[str, Any], base: dict[str, Any], comps: dict[str, Any],
                timeout: float) -> list[Check]:
    required = _required(comps, "rtk")
    checks, _path, _version = _tool_binary_version("rtk", base, required, timeout,
                                                   machine=ctx.get("machine_id"))
    files = (comps.get("rtk") or {}).get("integration_files") or []
    if not files:
        return checks
    resolved = [pathlib.Path(bootstrap.expand(str(bootstrap.resolve(item, ctx)))) for item in files]
    missing = [str(path) for path in resolved if not path.is_file()]
    if not missing:
        checks.append(Check("rtk", "integration", PASS,
                            "integration files present; runtime AUTO not re-tested by doctor",
                            "integrations"))
    elif not required:
        checks.append(Check("rtk", "integration", SKIP, "component not required", "integrations"))
    else:
        checks.append(Check("rtk", "integration", WARN, f"{len(missing)} integration file(s) absent",
                            "integrations", expected=", ".join(str(path) for path in resolved)))
    return checks


def _graphify_checks(ctx: dict[str, Any], base: dict[str, Any], comps: dict[str, Any],
                     timeout: float) -> list[Check]:
    required = _required(comps, "graphify")
    binary = (ctx.get("graphify") or {}).get("binary") or base.get("binary", "graphify")
    checks, path, version = _tool_binary_version("graphify", base, required, timeout, binary=binary,
                                                 machine=ctx.get("machine_id"))
    if path is None:
        checks.append(Check("graphify", "freshness", FAIL if required else SKIP,
                            "graphify unavailable; freshness unchecked", "tools"))
        return checks
    try:
        graph_dir = bootstrap.resolve_graph_dir(ctx)
    except bootstrap.BootstrapError as exc:
        checks.append(Check("graphify", "freshness", SKIP, f"no graph capability: {exc}", "tools"))
        return checks
    try:
        dirty = bool(bootstrap.worktree_changes((ctx.get("repository") or {}).get("root")))
    except bootstrap.BootstrapError:
        dirty = True
    manifest_name = (ctx.get("graphify") or {}).get("manifest_name", bootstrap.DEFAULT_MANIFEST_NAME)
    result = bootstrap.evaluate_graph_freshness(graph_dir, ctx.get("repository") or {}, version,
                                                version is not None, manifest_name, worktree_dirty=dirty)
    state = result["state"]
    status = FRESHNESS_TO_STATUS.get(state, FAIL if required else WARN)
    checks.append(Check("graphify", "freshness", status, f"{state}: {result['reason']}", "tools",
                        observed=str(graph_dir)))
    return checks


def _headroom_checks(ctx: dict[str, Any], base: dict[str, Any], comps: dict[str, Any],
                     timeout: float, health_timeout: float) -> list[Check]:
    required = _required(comps, "headroom")
    binary = (ctx.get("headroom") or {}).get("bin") or base.get("binary", "headroom")
    checks, _path, _version = _tool_binary_version("headroom", base, required, timeout, binary=binary,
                                                   machine=ctx.get("machine_id"))
    if not bool((ctx.get("feature") or {}).get("headroom_proxy", False)):
        checks.append(Check("headroom", "health", SKIP, "profile does not expect the proxy", "integrations"))
        return checks
    host = (ctx.get("headroom") or {}).get("host", "127.0.0.1")
    port = (ctx.get("headroom") or {}).get("port")
    if http_health(host, port, health_timeout):
        checks.append(Check("headroom", "health", PASS, f"healthy at {host}:{port}", "integrations"))
    else:
        checks.append(Check("headroom", "health", FAIL if required else WARN,
                            f"unreachable at {host}:{port}; 'oc' wrapper normally starts it",
                            "integrations", expected=f"http://{host}:{port}/health"))
    return checks


def _codex_mcp_checks(ctx: dict[str, Any]) -> list[Check]:
    config = (ctx.get("codex") or {}).get("config_path")
    if not config or not pathlib.Path(config).is_file():
        return [Check("codex", "headroom-mcp", SKIP, "codex config absent", "integrations")]
    try:
        lines = pathlib.Path(config).read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return [Check("codex", "headroom-mcp", WARN, "codex config unreadable", "integrations")]
    status, start, end, detail = bootstrap.find_toml_region(lines, "mcp_servers.headroom")
    if status != "ok":
        reason = f"[mcp_servers.headroom] {status}" + (f": {detail}" if detail else "")
        return [Check("codex", "headroom-mcp", WARN, reason, "integrations")]
    segment = "".join(lines[start:end])
    match = re.search(r'^\s*command\s*=\s*["\']([^"\']+)["\']', segment, re.MULTILINE)
    command = match.group(1) if match else None
    if not command:
        return [Check("codex", "headroom-mcp", WARN, "declaration missing command", "integrations")]
    ok = bool(shutil.which(command)) or pathlib.Path(command).exists()
    return [Check("codex", "headroom-mcp", PASS if ok else WARN,
                  "MCP command resolvable" if ok else "MCP command not resolvable",
                  "integrations", observed=command)]


def run(ctx: dict[str, Any], env: dict[str, str]) -> list[Check]:
    baseline = load_baseline().get("tools", {})
    doctor_cfg = ctx.get("doctor") or {}
    comps = doctor_cfg.get("components") or {}
    timeouts = doctor_cfg.get("timeouts") or {}
    version_timeout = float(timeouts.get("version_seconds", 5))
    health_timeout = float(timeouts.get("health_seconds", 3))
    checks: list[Check] = []
    checks += _repo_checks(ctx)
    checks += _bootstrap_checks(env, ctx)
    checks += _opencode_checks(ctx, baseline.get("opencode", {}), comps, version_timeout)
    checks += _codex_checks(ctx, baseline.get("codex", {}), comps, version_timeout)
    checks += _rtk_checks(ctx, baseline.get("rtk", {}), comps, version_timeout)
    checks += _graphify_checks(ctx, baseline.get("graphify", {}), comps, version_timeout)
    checks += _headroom_checks(ctx, baseline.get("headroom", {}), comps, version_timeout, health_timeout)
    checks += _codex_mcp_checks(ctx)
    return checks


# --------------------------------------------------------------------------- #
# Reporting / command
# --------------------------------------------------------------------------- #


def build_report(env: dict[str, str]) -> dict[str, Any]:
    ctx, meta = bootstrap.load_effective(env)
    checks = run(ctx, env)
    summary = {PASS: 0, WARN: 0, FAIL: 0, SKIP: 0}
    for check in checks:
        summary[check.status] += 1
    return {
        "machine": meta["machine_id"],
        "platform": meta["platform"],
        "ready": summary[FAIL] == 0,
        "summary": {"pass": summary[PASS], "warn": summary[WARN],
                    "fail": summary[FAIL], "skip": summary[SKIP]},
        "checks": [check.as_dict() for check in checks],
    }


def _print_human(report: dict[str, Any]) -> None:
    print(f"MACHINE {report['machine']} ({report['platform']})")
    for category, title in (("core", "CORE"), ("tools", "TOOLS"), ("integrations", "INTEGRATIONS")):
        rows = [check for check in report["checks"] if check["category"] == category]
        if not rows:
            continue
        print(title)
        for check in rows:
            extra = ""
            if check["observed"]:
                extra += f"  observed={check['observed']}"
            if check["expected"]:
                extra += f"  expected={check['expected']}"
            print(f"{check['status']:<4} {check['component']:<10} {check['check']:<14} "
                  f"{check['reason']}{extra}")
    summary = report["summary"]
    print("SUMMARY " + " ".join(f"{key.upper()}={summary[key]}" for key in ("pass", "warn", "fail", "skip")))
    print(f"READY {'yes' if report['ready'] else 'no'}")


def cmd_doctor(env: dict[str, str], as_json: bool = False) -> int:
    """Read-only. Exit 0 if no FAIL checks, 1 if any FAIL, 2 on config/exec error."""
    try:
        report = build_report(env)
    except bootstrap.BootstrapError:
        raise
    except RuntimeError as exc:  # infra.InfraError and friends
        raise bootstrap.BootstrapError(str(exc)) from exc
    if as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        _print_human(report)
    return 1 if report["summary"]["fail"] else 0
