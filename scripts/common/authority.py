#!/usr/bin/env python3
"""ONE effective runtime authority model (V0.1, stdlib only).

Three questions used to be answered in more than one place, or by more than one
kind of evidence. This module is the single place that decides them.

1. HARD POLICY vs OVERRIDABLE DEFAULTS. A machine profile
   (``machines/<id>.yaml``) may *prohibit* a provider outright
   (``provider_availability: false``, or
   ``local_inference: NOT_CONFIGURED_BY_MACHINE_POLICY``). A prohibition is
   config-owned and never inferred; no CLI flag, machine-local preference, or
   runtime observation can lift it. Every other value is an overridable default
   that resolves ``explicit -> machine-local -> portable``.
2. OBSERVATION IS EVIDENCE. A discovered binary, a health probe, an installed
   model, or a previous success can only *narrow* eligibility (unhealthy,
   missing, in cooldown). It can never grant permission.
3. THE WORKSPACE. The active project workspace is resolved explicitly, and a
   repository's identity comes from its remote/name, never from path spelling.

Read-only and side-effect free: no provider or model call, no write, no install.
Fields that have a single owner instead of a merge are listed as
``field-specific`` in docs/RUNTIME_AUTHORITY.md.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
from typing import Any, Optional

import infra  # scripts/common, on sys.path

ROOT = infra.ROOT
MACHINE_DIR = ROOT / "machines"
DEFAULT_STATE_DIR = "~/.local/state/local-ai-infra"
MACHINE_LOCAL_NAME = "machine.local.yaml"

# Provenance: which source supplied an effective value.
PORTABLE_DEFAULT = "portable_default"
MACHINE_LOCAL = "machine_local"
EXPLICIT = "explicit_invocation"
MACHINE_POLICY = "machine_policy"
RUNTIME_OBSERVATION = "runtime_observation"

# Permission states (hard policy) and observation states (runtime evidence).
PERMITTED = "PERMITTED"
PROHIBITED = "PROHIBITED_BY_MACHINE_POLICY"
NOT_CONFIGURED_BY_MACHINE_POLICY = "NOT_CONFIGURED_BY_MACHINE_POLICY"
HEALTHY = "HEALTHY"
UNHEALTHY = "UNHEALTHY"
UNKNOWN = "UNKNOWN"

# Machine-identity provenance.
MACHINE_ID_ENV = "explicit_env"
MACHINE_ID_REPO_PATH = "repo_path"

_UNSET: Any = object()


class AuthorityError(RuntimeError):
    """Explicit authority/parsing failure. Never guessed, never silently ignored."""


# --------------------------------------------------------------------------- #
# Machine policy (HARD) and machine-local preferences (OVERRIDABLE)
# --------------------------------------------------------------------------- #


def load_machine_descriptor(machine_id: str, *, root: pathlib.Path = MACHINE_DIR.parent) -> dict[str, Any]:
    """Load one committed machine profile. The descriptor is the policy owner."""
    path = pathlib.Path(root) / "machines" / f"{machine_id}.yaml"
    if not path.exists():
        raise AuthorityError(f"unknown machine profile: {machine_id} ({path})")
    try:
        profile = infra.load_json(path)
    except infra.InfraError as exc:
        raise AuthorityError(f"malformed machine profile {path}: {exc}") from exc
    if profile.get("id") != machine_id:
        raise AuthorityError(f"machine profile id mismatch in {path}")
    return profile


def resolve_machine_id(env: dict[str, str], *, root: pathlib.Path = MACHINE_DIR.parent) -> tuple[Optional[str], Optional[str]]:
    """(machine_id, source). Explicit ``LAI_MACHINE`` first, else the descriptor
    whose declared ``repo_path`` is exactly this repository. Never guessed."""
    machine_id = (env or {}).get("LAI_MACHINE")
    if machine_id:
        return machine_id, MACHINE_ID_ENV
    root = pathlib.Path(root)
    for path in sorted((root / "machines").glob("*.yaml")):
        try:
            declared = (infra.load_json(path) or {}).get("repo_path")
        except infra.InfraError:
            continue  # an unreadable *other* machine profile is not this machine's error
        if declared and pathlib.Path(os.path.expanduser(str(declared))) == root:
            return path.stem, MACHINE_ID_REPO_PATH
    return None, None


def machine_policy(env: Optional[dict[str, str]] = None, *,
                   root: pathlib.Path = MACHINE_DIR.parent) -> dict[str, Any]:
    """The HARD machine policy: what this machine forbids, and why.

    An unresolved or unknown machine has no prohibition (nothing is prohibited by
    policy that was never declared); a *declared but malformed* profile is an
    explicit error rather than a silent "permitted".
    """
    env = dict(os.environ) if env is None else dict(env)
    root = pathlib.Path(root)
    machine_id, id_source = resolve_machine_id(env, root=root)
    policy: dict[str, Any] = {
        "schema_version": 1,
        "source": MACHINE_POLICY,
        "machine_id": machine_id,
        "machine_id_source": id_source,
        "descriptor": None,
        "local_inference": None,
        "prohibited": {},
    }
    if machine_id is None:
        return policy
    descriptor_path = root / "machines" / f"{machine_id}.yaml"
    if not descriptor_path.exists():
        return policy  # unknown id is treated like an undeclared machine
    descriptor = load_machine_descriptor(machine_id, root=root)
    policy["descriptor"] = str(descriptor_path)
    for provider, available in sorted((descriptor.get("provider_availability") or {}).items()):
        if available is False:
            policy["prohibited"][provider] = PROHIBITED
    if descriptor.get("local_inference") == NOT_CONFIGURED_BY_MACHINE_POLICY:
        policy["local_inference"] = NOT_CONFIGURED_BY_MACHINE_POLICY
        # A discovered Ollama binary or model must not contradict this policy.
        policy["prohibited"]["ollama"] = NOT_CONFIGURED_BY_MACHINE_POLICY
    return policy


def state_dir(env: dict[str, str], machine: Optional[dict[str, Any]] = None) -> pathlib.Path:
    """Machine-local state root, mirroring the bootstrap precedence."""
    raw = (env or {}).get("LAI_STATE_DIR") or (machine or {}).get("state_dir") or DEFAULT_STATE_DIR
    return pathlib.Path(os.path.expanduser(str(raw)))


def load_machine_local(env: Optional[dict[str, str]], machine: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Optional machine-local preferences. Absent means "no local preference";
    unreadable means an explicit failure, never a silent fallback."""
    path = state_dir(env or {}, machine) / MACHINE_LOCAL_NAME
    if not path.exists():
        return {}
    try:
        return infra.load_json(path)
    except infra.InfraError as exc:
        raise AuthorityError(f"malformed machine-local config {path}: {exc}") from exc


def effective(field: str, *, portable: Any = _UNSET, machine_local: Any = _UNSET,
              explicit: Any = _UNSET, prohibited_by: Optional[str] = None) -> dict[str, Any]:
    """Resolve ONE overridable field, with the source that supplied it.

    Precedence (highest first): explicit invocation, machine-local preference,
    portable default. A hard policy prohibition is authoritative for the whole
    field and cannot be overridden by any layer — availability is not
    authorization.
    """
    layers = ((EXPLICIT, explicit), (MACHINE_LOCAL, machine_local), (PORTABLE_DEFAULT, portable))
    if prohibited_by:
        return {"field": field, "value": None, "source": MACHINE_POLICY, "allowed": False,
                "blocked_by": prohibited_by,
                "layers": [source for source, value in layers if value is not _UNSET]}
    for source, value in layers:
        if value is not _UNSET:
            return {"field": field, "value": value, "source": source, "allowed": True,
                    "blocked_by": None,
                    "layers": [name for name, item in layers if item is not _UNSET]}
    return {"field": field, "value": None, "source": None, "allowed": True, "blocked_by": None,
            "layers": []}


def observation_gate(provider: str, status: str, *, policy: Optional[dict[str, Any]] = None,
                     tripped: bool = False) -> dict[str, Any]:
    """Filter one candidate by runtime observation. Observation may only remove
    eligibility; a prohibited provider stays prohibited even when it looks healthy."""
    prohibited = (policy or {}).get("prohibited") or {}
    if provider in prohibited:
        return {"provider": provider, "allowed": False, "eligible": False,
                "reason": prohibited[provider], "source": MACHINE_POLICY, "observed": status}
    if tripped:
        return {"provider": provider, "allowed": True, "eligible": False, "reason": "cooldown",
                "source": RUNTIME_OBSERVATION, "observed": status}
    if status != HEALTHY:
        return {"provider": provider, "allowed": True, "eligible": False, "reason": status,
                "source": RUNTIME_OBSERVATION, "observed": status}
    return {"provider": provider, "allowed": True, "eligible": True, "reason": None,
            "source": RUNTIME_OBSERVATION, "observed": status}


# --------------------------------------------------------------------------- #
# Workspace authority
# --------------------------------------------------------------------------- #


def canonical_workspace(root: Any) -> pathlib.Path:
    """One validation rule for an explicit workspace root (symlinks resolved)."""
    resolved = pathlib.Path(root).expanduser()
    if not resolved.exists() or not resolved.is_dir():
        raise AuthorityError(f"WORKSPACE_INVALID: workspace root is not a directory: {resolved}")
    return resolved.resolve()


def _git_in(root: Any, *args: str) -> Optional[str]:
    try:
        result = subprocess.run(["git", "-C", str(root), *args],
                                check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    value = result.stdout.strip()
    return value or None


def sanitize_remote(url: Optional[str]) -> Optional[str]:
    """Strip credential-bearing userinfo from a git remote URL."""
    if not url:
        return None
    return re.sub(r"^([a-zA-Z][a-zA-Z0-9+.\-]*://)[^/@]*@", r"\1", url)


def repository_identity(root: Any) -> dict[str, Any]:
    """Stable repository identity. Path spelling is never identity: the remote URL
    (credential-stripped) is used when present, else the repository name."""
    root = pathlib.Path(root)
    toplevel = _git_in(root, "rev-parse", "--show-toplevel")
    base = pathlib.Path(toplevel).resolve() if toplevel else root.resolve()
    remote = sanitize_remote(_git_in(root, "config", "--get", "remote.origin.url"))
    head = _git_in(root, "rev-parse", "HEAD")
    return {"id": f"remote:{remote}" if remote else f"name:{base.name}",
            "name": base.name,
            "root": str(base),
            "head": head,
            "remote": remote,
            "is_repository": bool(toplevel),
            "worktree": _is_linked_worktree(base) if toplevel else False}


def _is_linked_worktree(root: pathlib.Path) -> bool:
    """A linked worktree shares the common git dir but has its own git dir."""
    git_dir = _git_in(root, "rev-parse", "--git-dir")
    common = _git_in(root, "rev-parse", "--git-common-dir")
    if not git_dir or not common:
        return False

    def absolute(value: str) -> pathlib.Path:
        path = pathlib.Path(value)
        return (path if path.is_absolute() else root / path).resolve()

    return absolute(git_dir) != absolute(common)


def _discover_workspace(invocation: Optional[pathlib.Path], infra_root: pathlib.Path) -> dict[str, Any]:
    """Discover the active workspace from the invocation directory.

    A repository subdirectory resolves to the repository toplevel, so
    ``cwd == workspace`` is never assumed.
    """
    toplevel = _git_in(invocation, "rev-parse", "--show-toplevel") if invocation else None
    if toplevel:
        workspace = pathlib.Path(toplevel).resolve()
        return {"workspace": str(workspace), "source": "discovered_git_toplevel",
                "is_repository": True, "repository": repository_identity(workspace)}
    if invocation is not None:
        return {"workspace": str(invocation), "source": "invocation_directory",
                "is_repository": False, "repository": repository_identity(invocation)}
    return {"workspace": str(infra_root.resolve()), "source": "infrastructure_repository",
            "is_repository": False, "repository": repository_identity(infra_root)}


def resolve_workspace(explicit: Any = None, *, invocation_dir: Any = None,
                      infra_root: pathlib.Path = ROOT) -> dict[str, Any]:
    """Resolve the ONE active workspace, with provenance.

    Explicit wins over discovery (``--workspace`` is the user's authority), and a
    disagreement between the two is reported as a conflict rather than hidden.
    An explicit path that is not a repository is still a valid workspace (tools
    and Git need a directory, not a remote).
    """
    infra_root = pathlib.Path(infra_root)
    raw_invocation = pathlib.Path(invocation_dir) if invocation_dir is not None else pathlib.Path.cwd()
    invocation = canonical_workspace(raw_invocation) if raw_invocation.exists() else None
    discovered = _discover_workspace(invocation, infra_root)

    if explicit is None:
        selected = dict(discovered)
    else:
        workspace = canonical_workspace(explicit)
        selected = {"workspace": str(workspace), "source": EXPLICIT,
                    "is_repository": repository_identity(workspace)["is_repository"],
                    "repository": repository_identity(workspace)}

    conflict = None
    if explicit is not None and discovered["source"] == "discovered_git_toplevel" \
            and discovered["repository"]["id"] != selected["repository"]["id"]:
        conflict = {"explicit_identity": selected["repository"]["id"],
                    "discovered_identity": discovered["repository"]["id"],
                    "note": "explicit workspace remains authoritative"}
    return {**selected,
            "invocation_dir": str(invocation) if invocation is not None else None,
            "invocation_source": discovered["source"],
            "subdirectory": bool(invocation is not None and selected["workspace"] != str(invocation)),
            "worktree": bool(selected["repository"]["worktree"]),
            "is_infrastructure_repo": selected["workspace"] == str(infra_root.resolve()),
            "conflict": conflict}


# --------------------------------------------------------------------------- #
# Structured configuration parsing (stdlib only, no ad-hoc extraction)
# --------------------------------------------------------------------------- #


def read_toml(path: Any) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Parse TOML with the stdlib parser. Returns ``(data, error)``.

    Absent files are ``(None, None)``. Malformed or unreadable files are an
    explicit error string, never a guessed value. Python < 3.11 has no
    ``tomllib``, so it is imported lazily to keep this module importable there.
    """
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
        return None, "tomllib unavailable (Python >= 3.11 required to parse TOML)"
    path = pathlib.Path(path)
    if not path.exists():
        return None, None
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle), None
    except OSError as exc:
        return None, f"unreadable TOML: {exc}"
    except tomllib.TOMLDecodeError as exc:
        return None, f"malformed TOML: {exc}"


def codex_config_path() -> pathlib.Path:
    return pathlib.Path.home() / ".codex" / "config.toml"


def read_codex_model(path: Any = None) -> tuple[Optional[str], Optional[str]]:
    """Read the locally configured Codex model with a real TOML parse.

    The old line scan matched ``model_provider``/``model_reasoning_effort`` as
    well; an exact top-level ``model`` key never does.
    """
    data, error = read_toml(codex_config_path() if path is None else path)
    if data is None:
        return None, error
    model = data.get("model")
    if model is None:
        return None, error
    if not isinstance(model, str) or not model.strip():
        return None, "codex model must be a non-empty string"
    return model, error
