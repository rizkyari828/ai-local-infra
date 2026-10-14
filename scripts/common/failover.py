"""Capability-preserving backend failover V0.1 (local, deterministic, cheap health).

Separates symbolic CAPABILITY (tier/effort) from RUNTIME AVAILABILITY. Resolution
selects only candidates that are actually runnable (healthy backend/provider/model,
not in cooldown) and that PRESERVE the symbolic capability floor; it never silently
downgrades. Health checks are cheap (binary presence, local endpoints/inventory);
they never send a completion request and never consume the AI-call budget.

Infrastructure failures (rate limit/503/timeout/unavailable/account-unsupported)
fail over to an equivalent-or-stronger candidate; REASONING_INSUFFICIENT is handled
by intelligence escalation, not failover.

Hard machine policy (a provider the machine forbids) is NOT a health state: it is
checked before any observation in ``Health.status`` and ``policy_block``, so a
discovered binary or a reachable endpoint can never authorize a prohibited provider.
See scripts/common/authority.py.
"""

from __future__ import annotations

import pathlib
from typing import Any, Optional

import authority  # scripts/common, on sys.path
import infra  # scripts/common, on sys.path
import routing  # scripts/common, on sys.path
import discovery  # scripts/common, on sys.path

# Runtime availability states (distinct from discovery's KNOWN/AVAILABLE/VERIFIED).
BACKEND_HEALTHY = "BACKEND_HEALTHY"
BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
ACCOUNT_MODEL_UNSUPPORTED = "ACCOUNT_MODEL_UNSUPPORTED"
TEMPORARILY_RATE_LIMITED = "TEMPORARILY_RATE_LIMITED"
UNKNOWN = "UNKNOWN"
# Hard machine policy (config-owned, never inferred) shares the vocabulary of the
# one authority model in scripts/common/authority.py.
NOT_CONFIGURED_BY_MACHINE_POLICY = authority.NOT_CONFIGURED_BY_MACHINE_POLICY
PROHIBITED_BY_MACHINE_POLICY = authority.PROHIBITED
# A locally configured model whose configuration cannot be read exactly is not the
# same as "no model configured": the invalid state is reported, never inferred away.
MODEL_CONFIG_INVALID = "MODEL_CONFIG_INVALID"
RUNTIME_STATUSES = (BACKEND_HEALTHY, BACKEND_UNAVAILABLE, PROVIDER_UNAVAILABLE, MODEL_UNAVAILABLE,
                    ACCOUNT_MODEL_UNSUPPORTED, TEMPORARILY_RATE_LIMITED, UNKNOWN,
                    NOT_CONFIGURED_BY_MACHINE_POLICY, PROHIBITED_BY_MACHINE_POLICY,
                    MODEL_CONFIG_INVALID)

# Failures that justify capability-preserving backend failover (not intelligence).
FAILOVER_REASONS = ("RATE_LIMIT", "PROVIDER_503", "TIMEOUT", "ACCOUNT_MODEL_UNSUPPORTED",
                    "MODEL_UNAVAILABLE", "BACKEND_UNAVAILABLE", "PROVIDER_UNAVAILABLE")
NO_CAPABILITY_PRESERVING_FALLBACK = "NO_CAPABILITY_PRESERVING_FALLBACK"
FAILURE_TO_RUNTIME = {"RATE_LIMIT": TEMPORARILY_RATE_LIMITED, "PROVIDER_503": PROVIDER_UNAVAILABLE,
                      "TIMEOUT": UNKNOWN, "ACCOUNT_MODEL_UNSUPPORTED": ACCOUNT_MODEL_UNSUPPORTED}


class FailoverError(RuntimeError):
    pass


def _rank(order: list[str], value: Optional[str]) -> int:
    return order.index(value) if value in order else -1


class Health:
    """Cheap runtime health with injectable inventories (tests supply fakes).

    binaries:   backend -> bool (present)
    providers:  backend -> set(provider) it can serve (None = unknown)
    ollama_models: installed local model names (None = ollama unreachable/unknown)
    codex_model:   locally configured Codex model (None = unknown)
    unavailable:   backend or "backend:provider" -> runtime status (explicit override)
    tripped:       per-run cooldown set of "backend" or "backend:provider" keys
    local_inference: machine policy for local inference (NOT_CONFIGURED_BY_MACHINE_POLICY)
    prohibited:    provider -> hard machine-policy reason. Config-owned and checked
                   BEFORE any observation, so availability can never authorize a
                   provider the machine prohibits.
    codex_model_error: explicit error reading the local Codex model configuration.
                   Reported as MODEL_CONFIG_INVALID, never collapsed into "unset".
    """

    def __init__(self, *, binaries: Optional[dict[str, bool]] = None,
                 providers: Optional[dict[str, set[str]]] = None,
                 ollama_models: Optional[set[str]] = None,
                 codex_model: Optional[str] = None,
                 unavailable: Optional[dict[str, str]] = None,
                 tripped: Optional[set[str]] = None,
                 local_inference: Optional[str] = None,
                 prohibited: Optional[dict[str, str]] = None,
                 codex_model_error: Optional[str] = None) -> None:
        self.binaries = dict(binaries or {})
        self.providers = {k: set(v) for k, v in (providers or {}).items()}
        self.ollama_models = set(ollama_models) if ollama_models is not None else None
        self.codex_model = codex_model
        self.codex_model_error = codex_model_error
        self.unavailable = dict(unavailable or {})
        self.tripped = set(tripped or ())
        self.local_inference = local_inference
        self.prohibited = dict(prohibited or {})
        if local_inference == NOT_CONFIGURED_BY_MACHINE_POLICY:
            self.prohibited.setdefault("ollama", NOT_CONFIGURED_BY_MACHINE_POLICY)

    def _override(self, candidate: dict[str, Any]) -> Optional[str]:
        backend, provider = candidate["backend"], candidate["provider"]
        return self.unavailable.get(f"{backend}:{provider}") or self.unavailable.get(backend)

    def status(self, candidate: dict[str, Any]) -> str:
        # Hard policy first: a machine prohibition is not a health observation and
        # cannot be contradicted by a discovered binary, a reachable endpoint, or a
        # model that happens to be installed.
        prohibited = self.prohibited.get(candidate["provider"])
        if prohibited:
            return prohibited
        override = self._override(candidate)
        if override:
            return override
        if self.binaries.get(candidate["backend"]) is False:
            return BACKEND_UNAVAILABLE
        served = self.providers.get(candidate["backend"])
        if served is not None and candidate["provider"] not in served:
            return PROVIDER_UNAVAILABLE
        if candidate["provider"] == "ollama":
            if self.ollama_models is None:
                return UNKNOWN
            if candidate.get("model") and candidate["model"] not in self.ollama_models:
                return MODEL_UNAVAILABLE
        if candidate["provider"] == "codex":
            if self.codex_model_error:
                return MODEL_CONFIG_INVALID
            if self.codex_model is None:
                return UNKNOWN
        return BACKEND_HEALTHY

    def is_tripped(self, candidate: dict[str, Any]) -> bool:
        backend = candidate["backend"]
        return backend in self.tripped or f"{backend}:{candidate['provider']}" in self.tripped

    def trip(self, candidate: dict[str, Any], reason: str) -> None:
        """Bounded per-run cooldown. No permanent blacklist, no daemon."""
        self.tripped.add(candidate["backend"])
        if reason in ("PROVIDER_503", "RATE_LIMIT"):
            self.tripped.add(f"{candidate['backend']}:{candidate['provider']}")


def policy_block(provider: Optional[str], tier: Optional[str],
                 constraints: Optional[dict[str, Any]],
                 *, enforce_machine_policy: bool = True) -> Optional[str]:
    """ONE shared hard-policy authority every resolved candidate must pass, regardless of
    resolution path (health-aware, alternate, fallback or resume). A hard constraint that
    cannot be satisfied fails resolution clearly instead of leaking a remote candidate.

    A machine-policy prohibition is checked first and cannot be lifted by an explicit
    invocation, a machine-local preference, or a healthy observation.
    ``enforce_machine_policy`` is False only when no adapter in the run can reach a
    provider, so there is no launch to authorize (see execution.launch_capable)."""
    constraints = constraints or {}
    if enforce_machine_policy:
        prohibited = constraints.get("prohibited_providers") or {}
        if provider in prohibited:
            return prohibited[provider] or "prohibited_by_machine_policy"
    if constraints.get("local_only") and provider != "ollama":
        return "local_only"
    if constraints.get("no_premium") and tier == "MAX":
        return "no_premium"
    return None


def resolve(required_tier: str, required_effort: str, specs: list[dict[str, Any]],
            health: Health, *, constraints: Optional[dict[str, Any]] = None,
            enforce_machine_policy: bool = True) -> list[dict[str, Any]]:
    """Annotate candidate specs. Runnable = capability-preserving + healthy + not tripped
    + satisfies user constraints. Registry presence alone never makes a candidate runnable.
    """
    constraints = constraints or {}
    tier_order = list(routing.DEFAULT_TIER_ORDER)
    effort_order = list(routing.DEFAULT_EFFORT_ORDER)
    resolved: list[dict[str, Any]] = []
    for spec in specs:
        candidate = dict(spec)
        candidate.setdefault("tier", required_tier)
        candidate.setdefault("effort", required_effort)
        # Concrete machine-specific model fill (e.g., the locally configured Codex model);
        # never hardcoded in routing — supplied by local runtime capability.
        if candidate["provider"] == "codex" and not candidate.get("model") and health.codex_model:
            candidate["model"] = health.codex_model
        # Concrete capability qualification is NOT proven by provider identity, backend
        # availability, or tier-list membership. Until a reviewed qualification path
        # exists (Master C), every candidate is retained as UNVERIFIED capability.
        candidate["verified"] = False
        candidate["status"] = health.status(candidate)
        # Normalized effort is derived ONLY from the provider-native variant, never from the tier.
        try:
            candidate["normalized_effort"] = discovery.normalize_effort(
                candidate["provider"], candidate.get("native_variant"), discovery.load_config())
        except discovery.DiscoveryError:
            candidate["normalized_effort"] = None
        blocking: list[str] = []
        if _rank(tier_order, candidate["tier"]) < _rank(tier_order, required_tier):
            blocking.append("below_tier_floor")
        if _rank(effort_order, candidate["effort"]) < _rank(effort_order, required_effort):
            blocking.append("below_effort_floor")
        if health.is_tripped(candidate):
            blocking.append("cooldown")
        block = policy_block(candidate["provider"], candidate["tier"], constraints,
                             enforce_machine_policy=enforce_machine_policy)
        if block:
            blocking.append(block)
        if candidate["status"] != BACKEND_HEALTHY:
            blocking.append(candidate["status"])
        candidate["blocking"] = blocking
        candidate["runnable"] = not blocking
        resolved.append(candidate)
    return _order(resolved, constraints)


def _order(candidates: list[dict[str, Any]], constraints: dict[str, Any]) -> list[dict[str, Any]]:
    prefer_provider = constraints.get("prefer_provider")
    prefer_model = constraints.get("prefer_model")

    def key(candidate: dict[str, Any]) -> tuple:
        preferred = prefer_provider and (candidate["provider"] == prefer_provider
                                         or candidate["backend"] == prefer_provider)
        return (0 if candidate["runnable"] else 1,
                0 if preferred else 1,
                0 if (prefer_model and candidate.get("model") == prefer_model) else 1)

    return sorted(candidates, key=key)


def select(candidates: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    for candidate in candidates:
        if candidate["runnable"]:
            return candidate
    return None


def failover(candidates: list[dict[str, Any]], failed: dict[str, Any], health: Health,
             reason: str) -> Optional[dict[str, Any]]:
    """On an infra/reason failure, cool down the failed backend/provider and return the
    next capability-preserving candidate. Returns None for reasoning failures (caller
    escalates) or when no equivalent candidate exists."""
    if reason not in FAILOVER_REASONS:
        return None
    health.trip(failed, reason)
    for candidate in candidates:
        if candidate["backend"] == failed["backend"] and candidate["provider"] == failed["provider"]:
            continue
        if candidate["runnable"] and not health.is_tripped(candidate):
            return candidate
    return None


def candidates_for_tier(tier: str, *, config: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """Read capability-preserving candidate specs for a symbolic tier from config."""
    config = config if config is not None else discovery.load_config()
    return [dict(spec) for spec in config.get("failover", {}).get(tier, [])]
