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
   repository's identity comes from canonical Git metadata or a hashed canonical
   path, never from path spelling or a bare directory name.

Machine-policy authority is *reported*, never guessed: a malformed descriptor, an
invalid typed field, or an explicit selector that does not resolve is an explicit
INVALID state that fails closed. It is never collapsed into "no policy" (which
would be an unrestricted machine).

Read-only and side-effect free: no provider or model call, no write, no install.
Fields that have a single owner instead of a merge are listed as
``field-specific`` in docs/RUNTIME_AUTHORITY.md.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import re
import subprocess
from typing import Any, Optional

import infra  # scripts/common, on sys.path

ROOT = infra.ROOT
MACHINE_DIR = ROOT / "machines"
MACHINE_LOCAL_NAME = "machine.local.yaml"
DEFAULT_STATE_DIR = "~/.local/state/local-ai-infra"

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

# Machine-policy resolution states. The invalid states are deliberately distinct
# from "unregistered": an unresolvable authority must never read as an unrestricted
# machine.
VALID_REGISTERED_MACHINE = "VALID_REGISTERED_MACHINE"
GENUINELY_UNREGISTERED_MACHINE = "GENUINELY_UNREGISTERED_MACHINE"
NO_MACHINE_POLICY = "NO_MACHINE_POLICY"
EXPLICIT_MACHINE_SELECTOR_INVALID = "EXPLICIT_MACHINE_SELECTOR_INVALID"
MACHINE_DESCRIPTOR_MALFORMED = "MACHINE_DESCRIPTOR_MALFORMED"
MACHINE_POLICY_FIELD_INVALID = "MACHINE_POLICY_FIELD_INVALID"
INVALID_POLICY_STATES = (EXPLICIT_MACHINE_SELECTOR_INVALID, MACHINE_DESCRIPTOR_MALFORMED,
                         MACHINE_POLICY_FIELD_INVALID)

# Machine-identity provenance.
MACHINE_ID_ENV = "explicit_env"
MACHINE_ID_REPO_PATH = "repo_path"

_UNSET: Any = object()


class AuthorityError(RuntimeError):
    """Explicit authority/parsing failure. Never guessed, never silently ignored."""


# --------------------------------------------------------------------------- #
# Machine policy (HARD) and machine-local preferences (OVERRIDABLE)
# --------------------------------------------------------------------------- #


def _root(root: Optional[pathlib.Path]) -> pathlib.Path:
    """Resolved machine-descriptor root. Read at call time so it stays injectable."""
    return pathlib.Path(MACHINE_DIR.parent if root is None else root)


def load_machine_descriptor(machine_id: str, *, root: Optional[pathlib.Path] = None) -> dict[str, Any]:
    """Load one committed machine profile. The descriptor is the policy owner."""
    path = _root(root) / "machines" / f"{machine_id}.yaml"
    if not path.exists():
        raise AuthorityError(f"unknown machine profile: {machine_id} ({path})")
    try:
        profile = infra.load_json(path)
    except infra.InfraError as exc:
        raise AuthorityError(f"malformed machine profile {path}: {exc}") from exc
    if profile.get("id") != machine_id:
        raise AuthorityError(f"machine profile id mismatch in {path}")
    return profile


def validate_machine_policy_fields(descriptor: dict[str, Any]) -> Optional[str]:
    """Strict, deterministic typing for the hard-policy fields. Returns an error or None.

    ``false`` is a boolean prohibition; ``"false"`` is a mistyped value and is
    rejected rather than read through truthiness. A policy that cannot be read
    exactly is invalid, not permissive.
    """
    availability = descriptor.get("provider_availability")
    if availability is not None:
        if not isinstance(availability, dict):
            return "provider_availability must be an object"
        for provider, value in sorted(availability.items()):
            if not isinstance(value, bool):
                return (f"provider_availability.{provider} must be a boolean "
                        f"(got {type(value).__name__})")
    local = descriptor.get("local_inference")
    if local is not None and (not isinstance(local, str) or not local.strip()):
        return f"local_inference must be a non-empty string (got {type(local).__name__})"
    return None


def resolve_machine_id(env: dict[str, str], *,
                       root: pathlib.Path = MACHINE_DIR.parent) -> tuple[Optional[str], Optional[str]]:
    """(machine_id, source). Explicit ``LAI_MACHINE`` first, else the descriptor
    whose declared ``repo_path`` is exactly this repository. Never guessed."""
    machine_id = (env or {}).get("LAI_MACHINE")
    if machine_id:
        return machine_id, MACHINE_ID_ENV
    root = _root(root)
    for path in sorted((root / "machines").glob("*.yaml")):
        try:
            declared = (infra.load_json(path) or {}).get("repo_path")
        except infra.InfraError:
            continue  # handled as an explicit failure by machine_policy
        if declared and _same_path(str(declared), root):
            return path.stem, MACHINE_ID_REPO_PATH
    return None, None


def _same_path(declared: str, root: pathlib.Path) -> bool:
    return pathlib.Path(os.path.expanduser(declared)).resolve() == pathlib.Path(root).resolve()


def _policy_stub() -> dict[str, Any]:
    return {"schema_version": 1, "source": MACHINE_POLICY, "machine_id": None,
            "machine_id_source": None, "state": GENUINELY_UNREGISTERED_MACHINE,
            "descriptor": None, "local_inference": None, "prohibited": {}, "error": None}


def _policy_from_descriptor(policy: dict[str, Any], machine_id: str, id_source: Optional[str],
                            path: pathlib.Path, descriptor: dict[str, Any]) -> dict[str, Any]:
    policy.update(machine_id=machine_id, machine_id_source=id_source, descriptor=str(path))
    error = validate_machine_policy_fields(descriptor)
    if error:
        policy.update(state=MACHINE_POLICY_FIELD_INVALID, error=error, prohibited={},
                      local_inference=None)
        return policy
    prohibited = {provider: PROHIBITED
                  for provider, available in sorted((descriptor.get("provider_availability") or {}).items())
                  if available is False}
    local_inference = descriptor.get("local_inference")
    if local_inference == NOT_CONFIGURED_BY_MACHINE_POLICY:
        # A discovered Ollama binary or model must not contradict this policy.
        prohibited["ollama"] = NOT_CONFIGURED_BY_MACHINE_POLICY
    policy.update(state=VALID_REGISTERED_MACHINE, error=None, prohibited=prohibited,
                  local_inference=local_inference)
    return policy


def machine_policy(env: Optional[dict[str, str]] = None, *,
                   root: Optional[pathlib.Path] = None) -> dict[str, Any]:
    """The HARD machine policy: what this machine forbids, and why.

    Never raises: the resolved state is always reported. ``state`` is one of
    VALID_REGISTERED_MACHINE, GENUINELY_UNREGISTERED_MACHINE, or an
    INVALID_POLICY_STATES member. Only the valid and genuinely-unregistered states
    may be treated as permission-bearing; callers that must fail closed use
    :func:`machine_policy_or_fail`.
    """
    env = dict(os.environ) if env is None else dict(env)
    root = _root(root)
    disk = root / "machines"
    policy = _policy_stub()
    selector = env.get("LAI_MACHINE")
    if selector:
        path = disk / f"{selector}.yaml"
        if not path.exists():
            # An explicit selector is authoritative intent: it must resolve or fail.
            # It is never downgraded to automatic discovery.
            policy.update(machine_id=selector, machine_id_source=MACHINE_ID_ENV,
                          state=EXPLICIT_MACHINE_SELECTOR_INVALID,
                          error=f"LAI_MACHINE={selector} is not a registered machine ({path})")
            return policy
        try:
            descriptor = load_machine_descriptor(selector, root=root)
        except AuthorityError as exc:
            policy.update(machine_id=selector, machine_id_source=MACHINE_ID_ENV,
                          state=MACHINE_DESCRIPTOR_MALFORMED, error=str(exc))
            return policy
        return _policy_from_descriptor(policy, selector, MACHINE_ID_ENV, path, descriptor)

    malformed: list[str] = []
    paths = sorted(disk.glob("*.yaml")) if disk.exists() else []
    for path in paths:
        try:
            descriptor = infra.load_json(path)
        except infra.InfraError as exc:
            malformed.append(f"{path}: {exc}")
            continue
        declared = descriptor.get("repo_path")
        if declared and _same_path(str(declared), root):
            return _policy_from_descriptor(policy, path.stem, MACHINE_ID_REPO_PATH, path, descriptor)
    if malformed:
        # A registered descriptor that cannot be read may be THIS machine's policy:
        # unreadable policy must not masquerade as an unregistered, unrestricted one.
        policy.update(state=MACHINE_DESCRIPTOR_MALFORMED,
                      error="unreadable machine descriptor: " + malformed[0])
        return policy
    return policy


def machine_policy_or_fail(env: Optional[dict[str, str]] = None, *,
                           root: Optional[pathlib.Path] = None) -> dict[str, Any]:
    """Machine policy for an execution-critical caller: invalid policy fails closed."""
    policy = machine_policy(env, root=root)
    if policy["state"] in INVALID_POLICY_STATES:
        raise AuthorityError(f"MACHINE_POLICY_INVALID ({policy['state']}): {policy['error']}")
    return policy


def provider_permission(provider: str, *, env: Optional[dict[str, str]] = None,
                        root: Optional[pathlib.Path] = None) -> dict[str, Any]:
    """Is ``provider`` permitted by hard machine policy? Invalid policy raises.

    Availability is not authorization: this never consults health, a discovered
    binary, an installed model, or a previous success.
    """
    policy = machine_policy_or_fail(env, root=root)
    reason = policy["prohibited"].get(provider)
    return {"provider": provider, "allowed": reason is None, "reason": reason,
            "state": policy["state"], "machine_id": policy["machine_id"], "source": MACHINE_POLICY}


def bind_constraints(constraints: Optional[dict[str, Any]] = None, *,
                     env: Optional[dict[str, str]] = None,
                     root: Optional[pathlib.Path] = None) -> dict[str, Any]:
    """Merge authoritative machine policy into a run's hard constraints.

    This is the ONE place execution constraints are composed, so a real execution
    entrypoint cannot bypass policy by forgetting to attach it. A caller may
    narrow policy (add its own prohibitions or tighter constraints); it can never
    remove or weaken a machine prohibition.
    """
    policy = machine_policy_or_fail(env, root=root)
    bound = dict(constraints or {})
    prohibited = dict(bound.get("prohibited_providers") or {})
    prohibited.update(policy["prohibited"])  # hard policy always wins over the caller
    bound["prohibited_providers"] = prohibited
    bound["machine_policy"] = {"machine_id": policy["machine_id"], "state": policy["state"],
                               "source": policy["source"]}
    return bound


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
# Workspace and repository identity
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


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:24]


def _absolute(base: pathlib.Path, value: Optional[str]) -> Optional[pathlib.Path]:
    if not value:
        return None
    path = pathlib.Path(value)
    return (path if path.is_absolute() else base / path).resolve()


def repository_identity(root: Any) -> dict[str, Any]:
    """Canonical repository identity plus workspace/worktree instance identity.

    Identity is stable for the same actual repository, invariant to path spelling
    and symlink aliases, and distinct for unrelated repositories — a shared
    directory name is never proof of equivalence.

    - A sanitized remote URL is the strongest stable identity (shared by clones).
    - A remote-less Git repository uses a hash of its canonical common git
      directory, which is machine-local but collision-free.
    - A non-Git workspace uses a hash of its canonical path. Such identity is not
      portable, so it is reported as ``portable: False``; consumers must not treat
      it as cross-machine equivalence.

    ``workspace_instance`` distinguishes the mutable workspace (a linked worktree
    has its own git dir) while ``id`` stays shared across worktrees of one
    repository.
    """
    root = pathlib.Path(root)
    toplevel = _git_in(root, "rev-parse", "--show-toplevel")
    if toplevel:
        base = pathlib.Path(toplevel).resolve()
        remote = sanitize_remote(_git_in(root, "config", "--get", "remote.origin.url"))
        common = _absolute(base, _git_in(root, "rev-parse", "--git-common-dir"))
        git_dir = _absolute(base, _git_in(root, "rev-parse", "--git-dir"))
        head = _git_in(root, "rev-parse", "HEAD")
        if remote:
            identity, kind, portable = f"remote:{remote}", "remote", True
        else:
            identity, kind, portable = f"gitdir:{_digest(str(common or base))}", "git_common_dir", False
        return {"id": identity, "kind": kind, "portable": portable,
                "workspace_instance": f"worktree:{_digest(str(git_dir or base))}",
                "name": base.name, "root": str(base), "head": head, "remote": remote,
                "is_repository": True,
                "worktree": bool(common and git_dir and common != git_dir)}
    base = root.resolve()
    conservative = f"path:{_digest(str(base))}"
    return {"id": conservative, "kind": "path", "portable": False,
            "workspace_instance": conservative, "name": base.name, "root": str(base),
            "head": None, "remote": None, "is_repository": False, "worktree": False}


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
            "workspace_instance": selected["repository"]["workspace_instance"],
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

    Returns ``(model, error)``. An unreadable or malformed configuration is an
    explicit error, so a decision that depends on this value never silently
    degrades to "no model configured". The old line scan matched
    ``model_provider``/``model_reasoning_effort`` as well; an exact top-level
    ``model`` key never does.
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
