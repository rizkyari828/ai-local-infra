#!/usr/bin/env python3
"""Declarative multi-machine desired state for local-ai-infra.

M2 of multi-machine sync. Reads portable desired state (config + templates),
resolves it through the per-key precedence common -> platform -> machine -> local,
then reports (status), previews (dry-run), or applies owned blocks/keys only.

It never replaces whole user config files, never syncs secrets or runtime state,
and never restarts any process.
"""

from __future__ import annotations

import copy
import datetime as dt
import functools
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import tomllib
import uuid
from typing import Any

import infra  # scripts/common, on sys.path
import secret_guard as secret_scan

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config/bootstrap.yaml"
PLATFORM_DIR = ROOT / "config/platform"
MACHINE_DIR = ROOT / "machines"
DEFAULT_STATE_DIR = "~/.local/state/local-ai-infra"
BACKUP_SUBDIR = "bootstrap"
MANAGED_MARKER = "local-ai-infra managed"
DEFAULT_MANIFEST_NAME = "lai-freshness.json"
GRAPH_FILE = "graph.json"

PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")
MAX_RESOLVE_DEPTH = 12
MANAGED_SOURCE_GLOBS = (
    "config/*.yaml",
    "config/**/*.yaml",
    "machines/*.yaml",
    "templates/**/*",
)

# ownership vs drift: deferred/not-managed units never carry a drift state.
OWNERSHIP_MANAGED = "MANAGED"
OWNERSHIP_NOT_MANAGED = "NOT_MANAGED"
MUTABILITY_APPLY = "APPLY"
MUTABILITY_READ_ONLY = "READ_ONLY"
MUTABILITY_NONE = "NONE"

FRESHNESS_TO_DRIFT = {"CURRENT": "ALIGNED", "STALE": "STALE",
                      "MISSING": "MISSING", "BLOCKED": "BLOCKED"}


class BootstrapError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Loading and precedence
# --------------------------------------------------------------------------- #


def load_json(path: pathlib.Path) -> dict[str, Any]:
    return infra.load_json(path)


def load_if_present(path: pathlib.Path) -> dict[str, Any]:
    return infra.load_json(path) if path.exists() else {}


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge overlay into base per key. Dicts merge recursively; scalars/lists replace."""
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def select_machine(env: dict[str, str]) -> tuple[str, dict[str, Any]]:
    machine_id = env.get("LAI_MACHINE")
    if not machine_id:
        raise BootstrapError("LAI_MACHINE is required; refusing to guess machine identity")
    path = MACHINE_DIR / f"{machine_id}.yaml"
    if not path.exists():
        raise BootstrapError(f"Unknown machine profile: {machine_id} ({path})")
    profile = load_json(path)
    if profile.get("id") != machine_id:
        raise BootstrapError(f"Machine profile id mismatch in {path}")
    return machine_id, profile


def expand(value: str) -> str:
    return os.path.expanduser(value)


def _expand_paths(node: Any) -> Any:
    if isinstance(node, str):
        return expand(node) if node.startswith("~") else node
    if isinstance(node, dict):
        return {key: _expand_paths(item) for key, item in node.items()}
    if isinstance(node, list):
        return [_expand_paths(item) for item in node]
    return node


def _lookup(ctx: dict[str, Any], dotted: str) -> Any:
    node: Any = ctx
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise BootstrapError(f"Unresolved placeholder: {{{{{dotted}}}}}")
        node = node[part]
    return node


def _has_key(ctx: dict[str, Any], dotted: str) -> bool:
    node: Any = ctx
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def _resolve_string(text: str, ctx: dict[str, Any]) -> Any:
    """Expand placeholders repeatedly: deterministic, bounded, cycle-safe.

    A whole-string placeholder preserves the referenced value's type (int, list,
    ...). An embedded placeholder substitutes the string form and is re-expanded,
    so nested references such as ``{{graphify.graph_dir}}`` resolve fully. An
    unknown key, a detected cycle, or exceeding ``MAX_RESOLVE_DEPTH`` raises a
    config error rather than guessing.
    """
    current: Any = text
    seen: list[str] = []
    for _ in range(MAX_RESOLVE_DEPTH + 1):
        if not isinstance(current, str):
            return current
        whole = PLACEHOLDER_RE.fullmatch(current)
        if whole:
            value = _lookup(ctx, whole.group(1))
            if not isinstance(value, str):
                return value
            if value == current or value in seen:
                raise BootstrapError(
                    f"placeholder cycle detected resolving {{{{{whole.group(1)}}}}}: {value!r}")
            seen.append(current)
            current = value
            continue
        if not PLACEHOLDER_RE.search(current):
            return current
        if current in seen:
            raise BootstrapError(f"placeholder cycle detected: {current!r}")
        seen.append(current)
        current = PLACEHOLDER_RE.sub(lambda match: str(_lookup(ctx, match.group(1))), current)
    raise BootstrapError(
        f"placeholder expansion exceeded max depth {MAX_RESOLVE_DEPTH}: {text!r}")


def resolve(value: Any, ctx: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return _resolve_string(value, ctx)
    if isinstance(value, dict):
        return {key: resolve(item, ctx) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item, ctx) for item in value]
    return value


def state_dir_for(env: dict[str, str], machine: dict[str, Any]) -> pathlib.Path:
    raw = env.get("LAI_STATE_DIR") or machine.get("state_dir") or DEFAULT_STATE_DIR
    return pathlib.Path(expand(raw))


def _git_in(root: pathlib.Path | str, *args: str) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(root), *args],
                                check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    value = result.stdout.strip()
    return value or None


def _git(*args: str) -> str | None:
    return _git_in(ROOT, *args)


def sanitize_remote(url: str | None) -> str | None:
    """Strip credential-bearing userinfo from a git remote URL."""
    if not url:
        return None
    return re.sub(r"^([a-zA-Z][a-zA-Z0-9+.\-]*://)[^/@]*@", r"\1", url)


def _sanitize_identity(identity: Any) -> Any:
    if isinstance(identity, str) and identity.startswith("remote:"):
        return "remote:" + (sanitize_remote(identity[len("remote:"):]) or "")
    return identity


def compute_repository() -> dict[str, Any]:
    """Stable repository identity: remote when available, else the repo name.

    Absolute paths differ per machine and are diagnostics only; identity never
    depends on path equality.
    """
    toplevel = _git("rev-parse", "--show-toplevel") or str(ROOT)
    name = pathlib.Path(toplevel).name
    remote = sanitize_remote(_git("config", "--get", "remote.origin.url"))
    head = _git("rev-parse", "HEAD")
    identity = f"remote:{remote}" if remote else f"name:{name}"
    return {"name": name, "identity": identity, "root": str(pathlib.Path(toplevel).resolve()),
            "head": head, "remote": remote}


@functools.lru_cache(maxsize=8)
def detect_graphify_version(binary: str) -> tuple[str | None, bool]:
    """Return (version, available). Never installs or mutates anything."""
    executable = shutil.which(binary)
    if not executable:
        return None, False
    try:
        result = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None, True
    match = re.search(r"(\d+\.\d+\.\d+)", result.stdout or result.stderr or "")
    return (match.group(1) if match else None), True


def _resolved_inside(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        resolved = path.resolve()
        root_real = root.resolve()
    except OSError:
        return False
    return resolved == root_real or root_real in resolved.parents


def graph_path_safety(graph_dir: pathlib.Path, repo_root: pathlib.Path) -> str | None:
    """Return a BLOCKED reason if the graph dir resolves inside the repository."""
    if _resolved_inside(graph_dir, repo_root):
        return f"graph root resolves inside repository: {graph_dir}"
    return None


REQUIRED_MANIFEST_FIELDS = ("schema_version", "repository_identity", "git_head",
                            "graphify_version", "built_at", "graph_path")


def evaluate_graph_freshness(graph_dir: pathlib.Path, repository: dict[str, Any],
                             installed_version: str | None, graphify_available: bool,
                             manifest_name: str = DEFAULT_MANIFEST_NAME,
                             worktree_dirty: bool = False) -> dict[str, Any]:
    """Classify graph freshness. Warn-only: never rebuilds or mutates anything."""
    result: dict[str, Any] = {"state": "MISSING", "reason": None,
                              "graph_path": str(graph_dir / GRAPH_FILE), "manifest": None}
    unsafe = graph_path_safety(graph_dir, pathlib.Path(repository["root"]))
    if unsafe:
        result.update(state="BLOCKED", reason=unsafe)
        return result
    graph_path = graph_dir / GRAPH_FILE
    manifest_path = graph_dir / manifest_name
    if not graph_path.exists():
        result.update(state="MISSING", reason="graph absent")
        return result
    if not manifest_path.exists():
        result.update(state="MISSING",
                      reason="provenance manifest absent (existing graph not adopted)")
        return result
    try:
        manifest = load_json(manifest_path)
    except infra.InfraError as exc:
        result.update(state="BLOCKED", reason=f"malformed manifest: {exc}")
        return result
    missing = [key for key in REQUIRED_MANIFEST_FIELDS if key not in manifest]
    if manifest.get("schema_version") != 1 or missing:
        result.update(state="BLOCKED",
                      reason="malformed manifest" + (f": missing {', '.join(missing)}" if missing else ""))
        return result
    if not repository.get("identity"):
        result.update(state="BLOCKED", reason="repository identity unresolved")
        return result
    if manifest["repository_identity"] != repository["identity"]:
        result.update(state="BLOCKED", reason="repository identity mismatch")
        return result
    result["manifest"] = manifest
    if manifest["git_head"] != repository.get("head"):
        result.update(state="STALE", reason="graph HEAD differs from current HEAD")
        return result
    if not graphify_available:
        result.update(state="STALE", reason="installed graphify binary unavailable; version unverified")
        return result
    if not installed_version:
        result.update(state="STALE", reason="installed graphify version unparseable")
        return result
    if manifest["graphify_version"] != installed_version:
        result.update(state="STALE",
                      reason=f"graphify version differs ({manifest['graphify_version']} != {installed_version})")
        return result
    if pathlib.Path(manifest["graph_path"]).resolve() != graph_path.resolve():
        result.update(state="STALE", reason="manifest graph path differs from located graph")
        return result
    if worktree_dirty:
        result.update(state="STALE",
                      reason="graph matches committed HEAD but working tree has uncommitted changes")
        return result
    result.update(state="CURRENT", reason="provenance matches HEAD and version")
    return result


def write_graph_manifest(graph_dir: pathlib.Path, repository: dict[str, Any],
                         installed_version: str, graph_path: pathlib.Path,
                         manifest_name: str = DEFAULT_MANIFEST_NAME,
                         counts: dict[str, int] | None = None) -> dict[str, Any]:
    """Atomic, sanitized provenance manifest. Callers must have just built the graph."""
    unsafe = graph_path_safety(graph_dir, pathlib.Path(repository["root"]))
    if unsafe:
        raise BootstrapError(unsafe)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "repository_identity": _sanitize_identity(repository.get("identity")),
        "repository_name": repository.get("name"),
        "repository_root": repository.get("root"),
        "git_head": repository.get("head"),
        "graphify_version": installed_version,
        "built_at": infra.utc_now(),
        "graph_path": str(graph_path),
    }
    if counts:
        manifest.update(counts)
    findings = secret_scan.scan_values(manifest, "graph-manifest")
    if findings:
        raise BootstrapError("secret gate rejected graph manifest: " + secret_scan.describe(findings))
    infra.atomic_json(graph_dir / manifest_name, manifest, mode=0o600)
    return manifest


# --------------------------------------------------------------------------- #
# Explicit graph rebuild (M3.1): user-invoked only, never automatic
# --------------------------------------------------------------------------- #


def graph_unit(ctx: dict[str, Any]) -> dict[str, Any] | None:
    return next((unit for unit in ctx.get("units", []) if unit.get("mode") == "graph-freshness"), None)


def worktree_changes(repo_root: pathlib.Path | str) -> str:
    """Porcelain status of the working tree (tracked, staged, untracked; ignores ignored).

    Returns "" when clean. Raises if git cannot inspect the repository.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain", "--untracked-files=normal"],
            check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise BootstrapError(f"cannot inspect repository cleanliness: {exc}") from exc
    return result.stdout.strip()


def require_clean_worktree(repo_root: pathlib.Path | str) -> None:
    status = worktree_changes(repo_root)
    if not status:
        return
    entries = status.splitlines()
    sample = "; ".join(entries[:5])
    more = f" (+{len(entries) - 5} more)" if len(entries) > 5 else ""
    raise BootstrapError(
        "working tree is dirty; graph provenance requires a clean worktree at build "
        f"time (HEAD plus uncommitted changes cannot be declared clean). First changes: {sample}{more}")


def resolve_graph_dir(ctx: dict[str, Any]) -> pathlib.Path:
    unit = graph_unit(ctx)
    if unit is None:
        raise BootstrapError("no graph-freshness capability configured")
    return pathlib.Path(expand(str(resolve(unit["graph_dir"], ctx))))


def _graphify_env() -> dict[str, str]:
    """A clean env: drop GRAPHIFY_* so no external redirect/compare leaks in."""
    env = dict(os.environ)
    for key in [key for key in env if key.startswith("GRAPHIFY_")]:
        env.pop(key, None)
    return env


def _tail(text: str | None, limit: int = 400) -> str:
    cleaned = (text or "").strip()
    return cleaned[-limit:]


def locate_graph_output(staging: pathlib.Path) -> pathlib.Path:
    preferred = staging / "graphify-out" / GRAPH_FILE
    if preferred.is_file():
        return preferred
    found = sorted(staging.rglob(GRAPH_FILE))
    if not found:
        raise BootstrapError("graphify produced no graph.json")
    if len(found) > 1:
        raise BootstrapError(f"ambiguous graphify output: {len(found)} graph.json files")
    return found[0]


def validate_generated_graph(graph_path: pathlib.Path) -> dict[str, int]:
    try:
        data = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BootstrapError(f"generated graph is unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise BootstrapError("generated graph is not an object")
    nodes = data.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise BootstrapError("generated graph has no nodes")
    counts = {"node_count": len(nodes)}
    edges = data.get("edges", data.get("links"))
    if isinstance(edges, list):
        counts["edge_count"] = len(edges)
    sources = data.get("extracted_sources")
    if isinstance(sources, list):
        counts["source_count"] = len(sources)
    return counts


def run_graph_rebuild(ctx: dict[str, Any]) -> dict[str, Any]:
    """Rebuild into a sibling staging dir, then atomically promote on success."""
    cfg = ctx.get("graphify", {})
    manifest_name = cfg.get("manifest_name", DEFAULT_MANIFEST_NAME)
    binary = cfg.get("binary", "graphify")
    repo = ctx["repository"]
    repo_root = pathlib.Path(repo["root"])
    graph_dir = resolve_graph_dir(ctx)
    unsafe = graph_path_safety(graph_dir, repo_root)
    if unsafe:
        raise BootstrapError(unsafe)
    require_clean_worktree(repo_root)
    executable = shutil.which(binary)
    if not executable:
        raise BootstrapError("graphify binary unavailable; refusing to rebuild")
    version, _available = detect_graphify_version(binary)
    if not version:
        raise BootstrapError("cannot determine graphify version; refusing to write provenance")
    head_before = _git_in(repo_root, "rev-parse", "HEAD")
    if not head_before:
        raise BootstrapError("cannot determine repository HEAD; refusing to rebuild")
    graph_dir.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:12]
    staging = graph_dir.parent / f".lai-staging-{token}"
    old = graph_dir.parent / f".lai-old-{token}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, mode=0o700)
    invocation = [executable, "extract", str(repo_root), "--code-only",
                  "--no-cluster", "--out", str(staging)]
    try:
        print("INVOKE " + " ".join(invocation))
        result = subprocess.run(invocation, capture_output=True, text=True, env=_graphify_env())
        if result.returncode != 0:
            raise BootstrapError(f"graphify extract failed ({result.returncode}): {_tail(result.stderr)}")
        if _git_in(repo_root, "rev-parse", "HEAD") != head_before:
            raise BootstrapError("repository HEAD changed during rebuild; refusing to promote")
        graph_path = locate_graph_output(staging)
        counts = validate_generated_graph(graph_path)
        built_dir = graph_path.parent
        if graph_dir.exists():
            os.rename(graph_dir, old)
        try:
            os.rename(built_dir, graph_dir)
        except Exception:
            if old.exists():
                os.replace(old, graph_dir)
            raise
        try:
            manifest = write_graph_manifest(graph_dir, {**repo, "head": head_before}, version,
                                            graph_dir / GRAPH_FILE, manifest_name, counts)
        except Exception:
            shutil.rmtree(graph_dir, ignore_errors=True)
            if old.exists():
                os.replace(old, graph_dir)
            raise
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)
        return {"invocation": invocation, "counts": counts,
                "graph_dir": str(graph_dir), "manifest": manifest}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def load_effective(env: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    machine_id, machine = select_machine(env)
    platform = machine.get("platform") or env.get("LAI_PLATFORM")
    if not platform:
        raise BootstrapError("Machine profile must declare a platform")
    state_dir = state_dir_for(env, machine)
    local = load_if_present(state_dir / "machine.local.yaml")
    merged = deep_merge(load_json(CONFIG_PATH), load_if_present(PLATFORM_DIR / f"{platform}.yaml"))
    merged = deep_merge(merged, machine)
    merged = deep_merge(merged, local)
    merged["machine_id"] = machine_id
    merged["platform"] = platform
    merged["machine_local"] = local
    merged["state_dir"] = str(state_dir)
    if not merged.get("repository"):
        merged["repository"] = compute_repository()
    merged = _expand_paths(merged)
    if merged.get("schema_version") != 1:
        raise BootstrapError("Unsupported bootstrap schema_version")
    return merged, {"machine_id": machine_id, "platform": platform, "state_dir": state_dir, "env": env}


# --------------------------------------------------------------------------- #
# Template + managed-block helpers
# --------------------------------------------------------------------------- #


def render_template(relative: str, ctx: dict[str, Any]) -> str:
    path = ROOT / relative
    if not path.exists():
        raise BootstrapError(f"Missing template: {relative}")
    return resolve(path.read_text(encoding="utf-8"), ctx)


def markers(style: str, block_id: str) -> tuple[str, str]:
    if style == "html":
        return (f"<!-- >>> {MANAGED_MARKER}: {block_id} >>> -->",
                f"<!-- <<< {MANAGED_MARKER}: {block_id} <<< -->")
    return (f"# >>> {MANAGED_MARKER}: {block_id} >>>",
            f"# <<< {MANAGED_MARKER}: {block_id} <<<")


def render_block(style: str, block_id: str, body: str) -> str:
    begin, end = markers(style, block_id)
    return f"{begin}\n{body.strip(chr(10))}\n{end}\n"


def append_block(text: str, block_text: str) -> str:
    prefix = text
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    if prefix and not prefix.endswith("\n\n"):
        prefix += "\n"
    return prefix + block_text


def find_toml_region(lines: list[str], table: str) -> tuple[str, int | None, int | None, str | None]:
    header = re.compile(r"^\s*\[" + re.escape(table) + r"\]\s*$")
    child = re.compile(r"^\s*\[" + re.escape(table) + r"\.")
    any_header = re.compile(r"^\s*\[")
    dotted = re.compile(r"^\s*" + re.escape(table) + r"\.[^\s=.\[\]]+\s*=")
    starts = [index for index, line in enumerate(lines) if header.match(line)]
    if len(starts) > 1:
        return "blocked", None, None, f"duplicate [{table}] table"
    if not starts:
        if any(dotted.match(line) for line in lines):
            return "blocked", None, None, f"ambiguous dotted keys for {table}"
        return "missing", None, None, None
    if any(dotted.match(line) for line in lines):
        return "blocked", None, None, f"ambiguous dotted keys alongside [{table}]"
    start = starts[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if any_header.match(lines[index]) and not child.match(lines[index]):
            end = index
            break
    return "ok", start, end, None


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #


def _unit_plan(unit: dict[str, Any], ctx: dict[str, Any], disabled: set[str],
               local_overrides: set[str]) -> dict[str, Any]:
    unit_id = unit.get("id")
    component = unit.get("component")
    raw_target = unit.get("target")
    target = None
    if raw_target:
        try:
            target = str(pathlib.Path(expand(str(resolve(raw_target, ctx)))))
        except BootstrapError:
            target = raw_target
    base = {"id": unit_id, "component": component, "mode": unit.get("mode"),
            "target": target, "reason": None, "changes": False, "write": None,
            "hard": False, "create": False, "ownership": OWNERSHIP_MANAGED,
            "mutability": MUTABILITY_APPLY, "capability": None, "freshness": None,
            "state": None}
    if not unit_id or not component:
        base.update(state="BLOCKED", hard=True, reason="unit missing id/component")
        return base
    if unit_id in disabled or unit_id in local_overrides:
        base.update(state="LOCAL-OVERRIDE", mutability=MUTABILITY_NONE,
                    reason="superseded by machine.local override")
        return base
    required = unit.get("requires")
    if required and not _has_key(ctx, required):
        base.update(ownership=OWNERSHIP_NOT_MANAGED, mutability=MUTABILITY_NONE,
                    reason=f"not declared for this machine ({required} absent)")
        return base
    mode = unit.get("mode")
    if mode == "deferred":
        base.update(ownership=OWNERSHIP_NOT_MANAGED, mutability=MUTABILITY_NONE,
                    reason=unit.get("reason") or "not managed in v0.1")
        return base
    if mode == "graph-freshness":
        return _plan_graph_freshness(unit, ctx, base)
    if mode == "managed-block":
        return _plan_managed_block(unit, ctx, base)
    if mode == "toml-table":
        return _plan_toml(unit, ctx, base)
    if mode == "toml-key":
        return _plan_toml_key(unit, ctx, base)
    if mode == "generated-json":
        return _plan_json(unit, ctx, base)
    base.update(state="BLOCKED", hard=True, reason=f"unsupported mode: {mode}")
    return base


def _plan_managed_block(unit: dict[str, Any], ctx: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    target = pathlib.Path(expand(str(resolve(unit["target"], ctx))))
    block_id = unit["block_id"]
    style = unit.get("comment", "html")
    create = bool(unit.get("create"))
    block_text = render_block(style, block_id, render_template(unit["template"], ctx))
    base.update(target=str(target), block_id=block_id, create=create,
                desired=block_text)
    if not target.exists():
        base.update(state="MISSING", reason="target file absent",
                    changes=create, write=append_block("", block_text) if create else None)
        return base
    lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    begin, end = markers(style, block_id)
    begins = [i for i, line in enumerate(lines) if line.strip() == begin]
    ends = [i for i, line in enumerate(lines) if line.strip() == end]
    if not begins and not ends:
        base.update(state="MISSING", reason="managed block absent",
                    changes=True, write=append_block("".join(lines), block_text))
    elif len(begins) == 1 and len(ends) == 1 and begins[0] < ends[0]:
        existing = "".join(lines[begins[0]:ends[0] + 1]).rstrip("\n")
        if existing == block_text.rstrip("\n"):
            base.update(state="ALIGNED", reason="managed block matches")
        else:
            base.update(state="DRIFTED", reason="managed block differs", changes=True,
                        write="".join(lines[:begins[0]]) + block_text + "".join(lines[ends[0] + 1:]))
    else:
        base.update(state="BLOCKED", hard=True, reason="malformed or duplicate managed markers")
    return base


def _plan_toml(unit: dict[str, Any], ctx: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """Own exactly one TOML table. Creates the table in an existing file only.

    A missing target file is reported MISSING rather than fabricated: the M1/M2
    contract never invents a whole ``~/.codex/config.toml``. Every unrelated key,
    table, comment outside the owned table, and project-trust block is preserved.
    """
    target = pathlib.Path(expand(str(resolve(unit["target"], ctx))))
    table = unit["table"]
    create = bool(unit.get("create"))
    desired = render_template(unit["template"], ctx).strip("\n") + "\n"
    base.update(target=str(target), table=table, create=create, desired=desired)
    if not target.exists():
        base.update(state="MISSING",
                    reason="target config absent; refusing to create whole file",
                    changes=False)
        return base
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        base.update(state="BLOCKED", hard=True, reason=f"unreadable config: {exc}")
        return base
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        base.update(state="BLOCKED", hard=True, reason=f"malformed TOML: {exc}")
        return base
    lines = text.splitlines(keepends=True)
    status, start, end, detail = find_toml_region(lines, table)
    if status == "blocked":
        base.update(state="BLOCKED", hard=True, reason=detail)
        return base
    if status == "missing":
        if not create:
            base.update(state="MISSING", reason=f"[{table}] table absent", changes=False)
            return base
        base.update(state="MISSING", reason=f"[{table}] table absent; creating owned table",
                    changes=True, write=append_block(text, desired))
        return base
    assert start is not None and end is not None
    existing = "".join(lines[start:end]).strip("\n")
    if existing == desired.strip("\n"):
        base.update(state="ALIGNED", reason=f"[{table}] matches")
    else:
        base.update(state="DRIFTED", reason=f"[{table}] differs", changes=True,
                    write="".join(lines[:start]) + desired + "".join(lines[end:]))
    return base


def _toml_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value)


def _plan_toml_key(unit: dict[str, Any], ctx: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """Own exactly one top-level TOML scalar key, never a table or whole file.

    Used for a machine-specific default such as ``model``. A missing target file
    is reported MISSING, not fabricated. The key is only considered at top level
    (before the first table header); a table-scoped key of the same name is left
    untouched. Duplicate top-level declarations and malformed TOML are BLOCKED.
    """
    target = pathlib.Path(expand(str(resolve(unit["target"], ctx))))
    key = unit["key"]
    desired_value = resolve(unit["value"], ctx)
    desired_line = f"{key} = {_toml_literal(desired_value)}\n"
    create = bool(unit.get("create"))
    base.update(target=str(target), key=key, create=create, desired=desired_value)
    if not target.exists():
        base.update(state="MISSING",
                    reason="target config absent; refusing to create whole file",
                    changes=False)
        return base
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        base.update(state="BLOCKED", hard=True, reason=f"unreadable config: {exc}")
        return base
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        base.update(state="BLOCKED", hard=True, reason=f"malformed TOML: {exc}")
        return base
    lines = text.splitlines(keepends=True)
    first_header = next((i for i, line in enumerate(lines) if line.lstrip().startswith("[")),
                        len(lines))
    key_pattern = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
    offsets = [i for i in range(first_header) if key_pattern.match(lines[i])]
    if len(offsets) > 1:
        base.update(state="BLOCKED", hard=True, reason=f"duplicate top-level {key} key")
        return base
    if not offsets and key in parsed:
        # The name is already bound as a table ([model]) or a dotted key, so
        # inserting a scalar would produce invalid TOML. Refuse rather than guess.
        base.update(state="BLOCKED", hard=True,
                    reason=f"top-level {key} is defined as a table/dotted key")
        return base
    if offsets:
        # ponytail: owns a single-line top-level scalar only; a multi-line value
        # would need structured rewriting. Codex `model` is a quoted string.
        if '"""' in lines[offsets[0]] or "'''" in lines[offsets[0]]:
            base.update(state="BLOCKED", hard=True, reason=f"multi-line top-level {key} unsupported")
            return base
        if parsed.get(key) == desired_value:
            base.update(state="ALIGNED", reason=f"top-level {key} matches")
        else:
            write = "".join(lines[:offsets[0]]) + desired_line + "".join(lines[offsets[0] + 1:])
            base.update(state="DRIFTED", reason=f"top-level {key} differs",
                        changes=True, write=write)
        return base
    if not create:
        base.update(state="MISSING", reason=f"top-level {key} key absent", changes=False)
        return base
    head = lines[:first_header]
    while head and not head[-1].strip():
        head.pop()
    head_text = "".join(head)
    if head_text and not head_text.endswith("\n"):
        head_text += "\n"
    tail_text = "".join(lines[first_header:])
    write = head_text + desired_line + ("\n" + tail_text if tail_text else "")
    base.update(state="MISSING", reason=f"top-level {key} key absent; adding owned key",
                changes=True, write=write)
    return base


def _plan_json(unit: dict[str, Any], ctx: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    target = pathlib.Path(expand(str(resolve(unit["target"], ctx))))
    desired = resolve(unit.get("values", {}), ctx)
    base.update(target=str(target), create=bool(unit.get("create")), desired=desired)
    if not target.exists():
        base.update(state="MISSING", reason="generated file absent", changes=True, write=desired)
        return base
    try:
        current = load_json(target)
    except infra.InfraError as exc:
        base.update(state="BLOCKED", hard=True, reason=f"unreadable generated file: {exc}")
        return base
    if current == desired:
        base.update(state="ALIGNED", reason="generated file matches")
    else:
        base.update(state="DRIFTED", reason="generated file differs", changes=True, write=desired)
    return base


def _plan_graph_freshness(unit: dict[str, Any], ctx: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    graphify_cfg = ctx.get("graphify", {})
    manifest_name = graphify_cfg.get("manifest_name", DEFAULT_MANIFEST_NAME)
    binary = graphify_cfg.get("binary", "graphify")
    graph_dir = pathlib.Path(expand(str(resolve(unit["graph_dir"], ctx))))
    installed_version, available = detect_graphify_version(binary)
    try:
        worktree_dirty = bool(worktree_changes(ctx["repository"]["root"]))
    except BootstrapError:
        worktree_dirty = True
    result = evaluate_graph_freshness(graph_dir, ctx["repository"],
                                      installed_version, available, manifest_name,
                                      worktree_dirty=worktree_dirty)
    base.update(ownership=OWNERSHIP_MANAGED, mutability=MUTABILITY_NONE,
                capability="graph-freshness", graph_dir=str(graph_dir),
                freshness=result["state"], state=FRESHNESS_TO_DRIFT[result["state"]],
                target=result["graph_path"], changes=False,
                hard=(result["state"] == "BLOCKED"),
                reason=f"freshness={result['state']}: {result['reason']}")
    return base


def build_plan(env: dict[str, str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ctx, meta = load_effective(env)
    disabled = set(ctx.get("disabled_units") or [])
    local_overrides = set(ctx.get("override_units") or [])
    plan = [_unit_plan(unit, ctx, disabled, local_overrides) for unit in ctx.get("units", [])]
    return plan, {"ctx": ctx, **meta}


# --------------------------------------------------------------------------- #
# Secret gate
# --------------------------------------------------------------------------- #


def managed_source_paths() -> list[pathlib.Path]:
    paths: list[pathlib.Path] = []
    for pattern in MANAGED_SOURCE_GLOBS:
        paths.extend(sorted(ROOT.glob(pattern)))
    return [path for path in paths if path.is_file()]


def secret_gate(ctx: dict[str, Any]) -> None:
    findings = secret_scan.scan_paths(managed_source_paths())
    for unit in ctx.get("units", []):
        if unit.get("mode") == "generated-json":
            findings.extend(secret_scan.scan_values(resolve(unit.get("values", {}), ctx),
                                                    f"unit:{unit.get('id')}"))
    if findings:
        raise BootstrapError("secret gate rejected: " + secret_scan.describe(findings))


# --------------------------------------------------------------------------- #
# Writing / backup / rollback
# --------------------------------------------------------------------------- #


def atomic_text(path: pathlib.Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = pathlib.Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _target_mode(path: pathlib.Path) -> int:
    return (path.stat().st_mode & 0o777) if path.exists() else 0o600


def write_unit(plan: dict[str, Any]) -> None:
    target = pathlib.Path(plan["target"])
    if plan["mode"] == "generated-json":
        infra.atomic_json(target, plan["write"], mode=0o600)
        return
    atomic_text(target, plan["write"], mode=_target_mode(target))


def create_backup(changed: list[dict[str, Any]], ctx: dict[str, Any]) -> pathlib.Path:
    root = pathlib.Path(ctx["state_dir"]) / "backups" / BACKUP_SUBDIR
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    target = root / stamp
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    os.chmod(target, 0o700)
    manifest: dict[str, Any] = {"schema_version": 1, "kind": "bootstrap",
                                "created_at": infra.utc_now(), "machine": ctx["machine_id"],
                                "files": []}
    for plan in changed:
        path = pathlib.Path(plan["target"])
        existed = path.exists()
        item = {"unit": plan["id"], "component": plan["component"], "mode": plan["mode"],
                "path": str(path), "existed": existed, "checksum": None, "backup": None}
        if existed:
            data = path.read_bytes()
            item["checksum"] = "sha256:" + hashlib.sha256(data).hexdigest()
            destination = target / f"{len(manifest['files'])}-{path.name}"
            shutil.copy2(path, destination)
            os.chmod(destination, 0o600)
            item["backup"] = destination.name
        manifest["files"].append(item)
    infra.atomic_json(target / "manifest.json", manifest)
    return target


def latest_backup(state_dir: pathlib.Path) -> pathlib.Path:
    candidates = sorted((path.parent for path in
                         (state_dir / "backups" / BACKUP_SUBDIR).glob("*/manifest.json")), reverse=True)
    if not candidates:
        raise BootstrapError("No bootstrap backup is available")
    return candidates[0]


def restore_backup(target: pathlib.Path) -> dict[str, Any]:
    manifest = load_json(target / "manifest.json")
    for item in manifest["files"]:
        destination = pathlib.Path(item["path"])
        if item["existed"]:
            infra.atomic_restore(target / item["backup"], destination)
        else:
            destination.unlink(missing_ok=True)
    return manifest


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def _print_plan(plan: list[dict[str, Any]], meta: dict[str, Any]) -> None:
    print(f"MACHINE {meta['machine_id']} ({meta['platform']})  state_dir={meta['state_dir']}")
    managed = [unit for unit in plan if unit["ownership"] == OWNERSHIP_MANAGED]
    not_managed = [unit for unit in plan if unit["ownership"] != OWNERSHIP_MANAGED]
    print("MANAGED")
    for unit in managed:
        reason = unit.get("reason") or ""
        suffix = f"  # {reason}" if reason else ""
        capability = unit.get("capability") or unit["id"]
        state = unit.get("state") or "-"
        print(f"{unit['component']:<9} {capability:<17} {state:<14} "
              f"{unit.get('target') or '-'}{suffix}")
    print("NOT MANAGED")
    for unit in not_managed:
        capability = unit.get("capability") or unit["id"].split(".", 1)[-1]
        reason = unit.get("reason") or "not managed in v0.1"
        print(f"{unit['component']:<9} {capability:<17} {OWNERSHIP_NOT_MANAGED:<14} "
              f"# {reason}")


def cmd_status(env: dict[str, str]) -> int:
    plan, meta = build_plan(env)
    _print_plan(plan, meta)
    blocked = [unit for unit in plan if unit.get("hard")]
    return 1 if blocked else 0


def cmd_graph_status(env: dict[str, str]) -> int:
    plan, meta = build_plan(env)
    graph = next((unit for unit in plan if unit.get("mode") == "graph-freshness"), None)
    print(f"MACHINE {meta['machine_id']} ({meta['platform']})")
    if graph is None or graph.get("state") is None:
        print("graphify  graph-freshness   NOT_MANAGED  # no graph capability configured")
        return 0
    print(f"freshness={graph['freshness']}  state={graph['state']}")
    print(f"graph_dir={graph.get('graph_dir')}")
    print(f"graph_path={graph.get('target')}")
    print(f"reason={graph.get('reason')}")
    return 1 if graph.get("hard") else 0


def cmd_graph_rebuild(env: dict[str, str]) -> int:
    ctx, meta = load_effective(env)
    result = run_graph_rebuild(ctx)
    print(f"REBUILT {result['graph_dir']}")
    counts = ", ".join(f"{key}={value}" for key, value in sorted(result["counts"].items()))
    print(f"counts: {counts}")
    print(f"manifest: {result['manifest']['graph_path']}")
    return cmd_graph_status(env)


def cmd_apply(env: dict[str, str], dry_run: bool) -> int:
    plan, meta = build_plan(env)
    hard = [unit for unit in plan if unit.get("hard") and unit.get("mutability") == MUTABILITY_APPLY]
    _print_plan(plan, meta)
    if hard:
        print("BLOCKED: unsafe managed state; refusing to apply", flush=True)
        return 1
    secret_gate(meta["ctx"])
    changed = [unit for unit in plan if unit.get("changes") and unit.get("write") is not None]
    if not changed:
        print("No action taken: all managed config units aligned or local-overridden")
        return 0
    for unit in changed:
        print(f"CHANGE {unit['id']} -> {unit['target']} ({unit['state']})")
    if dry_run:
        print("DRY RUN: no files changed; no backup created; no graph rebuilt")
        return 0
    backup = create_backup(changed, meta["ctx"])
    print(f"Backup: {backup}")
    try:
        for unit in changed:
            # Re-plan per unit so several owned units in one file (Codex
            # config.toml: the model key and the Headroom table) compose instead
            # of each overwriting the other from a stale pre-apply snapshot.
            refreshed, _ = build_plan(env)
            current = next((candidate for candidate in refreshed
                            if candidate["id"] == unit["id"]), None)
            if current and current.get("changes") and current.get("write") is not None:
                write_unit(current)
        verified, _ = build_plan(env)
        changed_ids = {unit["id"] for unit in changed}
        unresolved = [unit for unit in verified
                      if unit["id"] in changed_ids and unit["state"] != "ALIGNED"]
        if unresolved:
            raise BootstrapError("post-apply verification failed: " +
                                 ", ".join(f"{u['id']}={u['state']}" for u in unresolved))
    except Exception:
        print("Apply failed; restoring backup", flush=True)
        restore_backup(backup)
        raise
    infra.atomic_json(pathlib.Path(meta["state_dir"]) / "bootstrap-applied.json",
                      {"applied_at": infra.utc_now(), "machine": meta["machine_id"],
                       "backup": str(backup), "units": [u["id"] for u in changed]})
    print(f"Applied {len(changed)} managed unit(s)")
    return 0


def cmd_rollback(env: dict[str, str], dry_run: bool) -> int:
    machine_id, machine = select_machine(env)
    state_dir = state_dir_for(env, machine)
    target = latest_backup(state_dir)
    manifest = load_json(target / "manifest.json")
    print(f"MACHINE {machine_id}  backup={target}")
    for item in manifest["files"]:
        action = "restore" if item["existed"] else "remove newly created"
        print(f"{item['component']:<9} {item['unit']:<18} {action} {item['path']}")
    if dry_run:
        print("DRY RUN: no files changed; no backup created")
        return 0
    restore_backup(target)
    print("Rollback completed")
    return 0


def cmd_secrets() -> int:
    findings = secret_scan.scan_paths(managed_source_paths())
    if findings:
        for finding in findings:
            print(f"FAIL {finding}")
        return 1
    print("PASS no obvious secrets in managed config/templates")
    return 0
