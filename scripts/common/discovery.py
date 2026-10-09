"""Model capability discovery V0.1 (read-only, standard library only).

Keeps MODEL CAPABILITY (provider/model/variants/surface) and VERIFICATION
separate from TASK CLASS and ROUTING REQUIREMENT. Discovery is catalog-only:

- it reads trustworthy local configuration (models/roles.yaml,
  config/model-capabilities.yaml) and an optional local generated snapshot
  (``$LAI_STATE_DIR/model-capabilities.json``);
- it never calls a model or provider, never spends tokens, never installs or
  downloads anything, and never mutates configuration.

Status is explicit and never conflated: a model can be KNOWN (present in a
trustworthy catalog) without being AVAILABLE (usable by a surface/account), and
AVAILABLE without any variant being VERIFIED (a recorded successful execution).
Availability may be reused across surfaces that demonstrably share config, but
VERIFICATION is per surface and per model+native-variant: one verified variant
does not verify the others.
"""

from __future__ import annotations

import pathlib
from typing import Any, Optional

import infra  # scripts/common, on sys.path
import routing  # scripts/common, on sys.path

ROOT = infra.ROOT
CAPABILITIES_PATH = ROOT / "config/model-capabilities.yaml"
STATE_SNAPSHOT_NAME = "model-capabilities.json"

KNOWN = "KNOWN"
AVAILABLE = "AVAILABLE"
VERIFIED = "VERIFIED"
UNAVAILABLE = "UNAVAILABLE"
UNVERIFIED = "UNVERIFIED"
UNKNOWN = "UNKNOWN"

STATUSES = (KNOWN, AVAILABLE, VERIFIED, UNAVAILABLE, UNVERIFIED, UNKNOWN)
DEFAULT_EFFORT_ORDER = ("LOW", "MEDIUM", "HIGH", "VERY_HIGH", "MAX")

# Failure categories must not conflate infrastructure trouble with model quality.
_FAILURE_CLASSIFICATION = {
    "ACCOUNT_MODEL_UNSUPPORTED": {"scope": "availability", "intelligence_evidence": False,
                                  "note": "the account/surface cannot use this model"},
    "AUTH_FAILURE": {"scope": "environment", "intelligence_evidence": False,
                     "note": "authentication/account issue"},
    "RATE_LIMIT": {"scope": "environment", "intelligence_evidence": False,
                   "note": "rate limiting says nothing about model strength"},
    "PROVIDER_503": {"scope": "environment", "intelligence_evidence": False,
                     "note": "provider outage, not model weakness"},
    "TIMEOUT": {"scope": "environment", "intelligence_evidence": False,
                "note": "timeout is not automatically a reasoning failure"},
    "REASONING_INSUFFICIENT": {"scope": "outcome", "intelligence_evidence": True,
                               "note": "valid outcome evidence about reasoning capability"},
    "STALE_SESSION": {"scope": "lifecycle", "intelligence_evidence": False,
                      "note": "surface lifecycle/session issue"},
}


class DiscoveryError(RuntimeError):
    pass


def load_config(path: pathlib.Path = CAPABILITIES_PATH) -> dict[str, Any]:
    try:
        config = infra.load_json(path)
    except infra.InfraError as exc:
        raise DiscoveryError(f"capability config unreadable: {exc}") from exc
    if config.get("schema_version") != 1:
        raise DiscoveryError("unsupported capability config schema_version")
    return config


def snapshot_path() -> pathlib.Path:
    return infra.STATE_DIR / STATE_SNAPSHOT_NAME


def load_snapshot(path: Optional[pathlib.Path] = None) -> dict[str, Any]:
    """Optional local generated state. Absent/empty means 'no local evidence'."""
    target = path if path is not None else snapshot_path()
    if not target.exists():
        return {"records": []}
    try:
        data = infra.load_json(target)
    except infra.InfraError:
        return {"records": []}
    return data if isinstance(data.get("records"), list) else {"records": []}


def canonical_provider(provider: Optional[str], config: dict[str, Any]) -> Optional[str]:
    if not provider:
        return None
    aliases = config.get("provider_aliases", {})
    key = str(provider).strip()
    return aliases.get(key, aliases.get(key.lower(), key.lower()))


def canonical_model(model: Optional[str], config: dict[str, Any]) -> Optional[str]:
    if not model:
        return None
    return config.get("model_aliases", {}).get(model, model)


def normalize_effort(provider: Optional[str], native: Optional[str],
                     config: dict[str, Any]) -> Optional[str]:
    """Provider-native -> normalized ordinal, or None (caller reports UNKNOWN)."""
    if not native:
        return None
    mappings = config.get("effort_mappings", {})
    table = mappings.get(provider) or mappings.get("default") or {}
    key = str(native).strip()
    if key in table:
        return table[key]
    return {k.lower(): v for k, v in table.items()}.get(key.lower())


def effort_rank(config: dict[str, Any], normalized: Optional[str]) -> int:
    order = config.get("effort_order", list(DEFAULT_EFFORT_ORDER))
    return order.index(normalized) if normalized in order else -1


def classify_failure(category: str) -> dict[str, Any]:
    entry = _FAILURE_CLASSIFICATION.get((category or "").strip().upper())
    if entry is None:
        return {"category": category, "scope": "unknown", "intelligence_evidence": False,
                "note": "unrecognized category; not treated as model evidence"}
    return {"category": category, **entry}


def _variant(provider: str, native: Any, config: dict[str, Any], verified: bool,
             source: str) -> dict[str, Any]:
    return {"native": native,
            "normalized": normalize_effort(provider, native, config) or UNKNOWN,
            "verified": verified, "source": source}


def _dedupe(variants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for variant in variants:
        key = str(variant.get("native"))
        if key not in merged:
            merged[key] = dict(variant)
        else:
            merged[key]["verified"] = merged[key].get("verified") or variant.get("verified", False)
    return list(merged.values())


def discover(*, config: Optional[dict[str, Any]] = None,
             snapshot: Optional[dict[str, Any]] = None,
             roles: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """Return capability records for every known model. Read-only, no network."""
    config = config if config is not None else load_config()
    snapshot = snapshot if snapshot is not None else load_snapshot()
    if roles is None:
        roles = routing.load_roles()

    records: dict[tuple, dict[str, Any]] = {}

    def record(provider: Optional[str], model: Optional[str]) -> Optional[dict[str, Any]]:
        if not provider or not model:
            return None
        return records.setdefault((provider, model),
                                  {"provider": provider, "model": model, "surfaces": {},
                                   "surface_variants": {}, "sources": set(), "roles": []})

    for role_id, role in sorted(roles.items()):
        entry = record(canonical_provider(role.get("provider"), config),
                       canonical_model(role.get("model_reference"), config))
        if entry is not None:
            entry["sources"].add("registry")
            entry["roles"].append(role_id)

    for override in config.get("manual_overrides", []):
        entry = record(canonical_provider(override.get("provider"), config),
                       canonical_model(override.get("model"), config))
        if entry is None:
            continue
        entry["sources"].add("manual_override")
        for surface in override.get("surfaces", []):
            entry["surfaces"][surface] = {"status": override.get("status", AVAILABLE),
                                          "source": "manual_override"}
            entry["surface_variants"].setdefault(surface, []).extend(
                _variant(entry["provider"], native, config, False, "manual_override")
                for native in override.get("efforts", []))

    for row in snapshot.get("records", []):
        entry = record(canonical_provider(row.get("provider"), config),
                       canonical_model(row.get("model"), config))
        if entry is None:
            continue
        source = row.get("source", "discovered")
        entry["sources"].add(source)
        surface = row.get("surface")
        if surface:
            entry["surfaces"][surface] = {"status": row.get("status", KNOWN), "source": source}
            entry["surface_variants"].setdefault(surface, []).extend(
                _variant(entry["provider"], variant.get("native"), config,
                         bool(variant.get("verified")), source)
                for variant in row.get("variants", []))

    result = []
    for entry in records.values():
        entry["surface_variants"] = {surface: _dedupe(variants)
                                     for surface, variants in entry["surface_variants"].items()}
        entry["variants"] = _dedupe([variant for variants in entry["surface_variants"].values()
                                     for variant in variants])
        entry["sources"] = sorted(entry["sources"])
        entry["status"] = KNOWN
        result.append(entry)
    result.sort(key=lambda row: (row["provider"], row["model"]))
    return result


def _shared_status(config: dict[str, Any], row: dict[str, Any], surface: str) -> Optional[tuple]:
    shared_with = (config.get("surfaces", {}).get(surface) or {}).get("shares_config_with")
    shared = row["surfaces"].get(shared_with) if shared_with else None
    if not shared or shared["status"] not in (KNOWN, AVAILABLE, VERIFIED):
        return None
    # Availability is shareable; verification is not, so VERIFIED downgrades to AVAILABLE.
    status = AVAILABLE if shared["status"] == VERIFIED else shared["status"]
    return status, f"shared_config:{shared_with}"


def capability(provider: Optional[str], model: Optional[str], surface: Optional[str] = None,
               *, config: Optional[dict[str, Any]] = None,
               snapshot: Optional[dict[str, Any]] = None,
               roles: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Resolve capability for one model (optionally on one surface)."""
    config = config if config is not None else load_config()
    provider = canonical_provider(provider, config)
    model = canonical_model(model, config)
    row = next((item for item in discover(config=config, snapshot=snapshot, roles=roles)
                if item["provider"] == provider and item["model"] == model), None)
    if surface is None:
        variants = row["variants"] if row else []
        return {"provider": provider, "model": model, "surface": None,
                "known": row is not None, "status": KNOWN if row else UNKNOWN,
                "source": row["sources"][0] if row and row["sources"] else None,
                "variants": variants}

    status, source = UNKNOWN, None
    if row:
        direct = row["surfaces"].get(surface)
        if direct:
            status, source = direct["status"], direct["source"]
        else:
            shared = _shared_status(config, row, surface)
            if shared:
                status, source = shared
    variants = row["surface_variants"].get(surface, []) if row else []
    return {"provider": provider, "model": model, "surface": surface,
            "known": row is not None, "status": status, "source": source, "variants": variants}


def effort_status(provider: Optional[str], model: Optional[str], surface: Optional[str],
                  normalized_effort: Optional[str], *, config: Optional[dict[str, Any]] = None,
                  snapshot: Optional[dict[str, Any]] = None,
                  roles: Optional[dict[str, Any]] = None) -> str:
    """Per-variant verification status. One verified variant never verifies others."""
    cap = capability(provider, model, surface, config=config, snapshot=snapshot, roles=roles)
    if cap["status"] == UNAVAILABLE:
        return UNAVAILABLE
    wanted = (normalized_effort or "").upper()
    for variant in cap["variants"]:
        if str(variant.get("normalized")) == wanted:
            return VERIFIED if variant.get("verified") else UNVERIFIED
    return UNKNOWN
