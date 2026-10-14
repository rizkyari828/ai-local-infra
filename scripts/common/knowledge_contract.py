#!/usr/bin/env python3
"""Shared Knowledge Contract V0.1 (generic protocol, standard library only).

Defines WHAT a knowledge/context requirement is, how a selection is identified, and
when delivery must be refused. It never decides where knowledge comes from and never
executes anything.

Ownership (fields that must not be centralised):

- ai-local-infra owns this protocol: versioning, identity rules, refusal semantics.
- A project repository owns its architecture, decision, API, skill and finding
  content.
- A machine owns its machine-local notes; reviewed learning owns derived observations.

A knowledge record can never grant provider permission, machine permission, a
``local_only`` override, mutation permission, tool permission or workspace
authority. Those keys are absent from the schema, so a record that declares one is
invalid rather than powerful (see ``PERMISSION_GRANT_FIELDS``).

Deliberately absent from V0.1: retrieval, a shared-knowledge runtime, active-work
claims, wait/join, leases, heartbeats, mutation locks, cache/resume enforcement
changes, and any adapter or provider rollout.

Pure and deterministic: no file read, no Git command, no provider call, no clock.
Repository identity is supplied by ``scripts/common/authority.py``; selected content
is supplied by the caller.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional

CONTRACT_VERSION = "shared-knowledge-contract-v1"
SELECTOR_VERSION = "knowledge-selector-v1"
VALIDATOR_VERSION = "knowledge-validator-v1"

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #

OWNER_CLASSES = ("project", "ai_local_infra", "machine_local", "derived")
SOURCE_KINDS = ("architecture", "decision", "api", "finding", "skill", "guidance",
                "contract", "note")
# Ordered weakest -> strongest. A requirement may demand a minimum strength.
AUTHORITY_ORDER = ("OBSERVED", "SUPPORTING", "AUTHORITATIVE")
REVIEW_STATES = ("REVIEWED", "UNREVIEWED", "NOT_APPLICABLE")
# Fail-closed default: an undeclared review state is NOT implied to be REVIEWED.
DEFAULT_REVIEW = "UNREVIEWED"
LIFECYCLE_STATES = ("ACTIVE", "SUPERSEDED", "REVOKED")

# Applicability facets: declared as lists, matched against a singular context key.
APPLICABILITY_FACETS = {"task_families": "task_family", "task_classes": "task_class",
                        "languages": "language", "surfaces": "surface"}

# Validation outcome. Any required-context failure yields the refusal outcome.
READY = "READY"
REQUIRED_CONTEXT_UNAVAILABLE = "REQUIRED_CONTEXT_UNAVAILABLE"

# Inspectable reasons (never a silent partial delivery).
MISSING = "missing"
INVALID = "invalid"
SUPERSEDED = "superseded"
REVOKED = "revoked"
AUTHORITY_INCOMPATIBLE = "authority_incompatible"
UNREVIEWED = "unreviewed"
NOT_APPLICABLE = "not_applicable"
REDACTION_INSUFFICIENT = "redaction_insufficient"
MISSING_CONTENT = "missing_content"
DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
PERMISSION_GRANT_REJECTED = "permission_grant_rejected"
UNSUPPORTED_CONTRACT_VERSION = "unsupported_contract_version"
DUPLICATE_KEY = "duplicate_key"
INVALID_DOCUMENT = "invalid_document"
UNKNOWN_FIELD = "unknown_field"

# A knowledge record that declares any of these is invalid, not authoritative.
PERMISSION_GRANT_FIELDS = ("provider", "providers", "provider_permission", "machine_permission",
                           "local_only", "mutation", "mutation_permission", "tool", "tools",
                           "tool_permission", "workspace", "workspace_authority", "grants",
                           "permissions", "capabilities", "execute", "allow", "bypass")

SOURCE_FIELDS = ("key", "owner", "kind", "locator", "authority", "review", "lifecycle",
                 "superseded_by", "supersedes", "depends_on", "required_semantics", "revision",
                 "applicability")
REQUIREMENT_FIELDS = ("key", "required", "scope", "reason", "min_authority", "applicability")
CONTENT_FIELDS = ("content", "delivered", "redactions", "revision")
DOCUMENT_FIELDS = ("contract_version", "repositories", "sources", "requirements", "contents",
                   "scope", "reason", "context")

CONTRACT_DOCUMENT_KEYS = ("contract_version", "selector_version", "validator_version", "scope",
                          "reason", "context", "entries", "omissions", "refusals", "outcome",
                          "manifest_fingerprint", "delivered_content_fingerprint",
                          "delivered_identity")

_LOCATOR_UNSAFE = re.compile(r"(^/|^[A-Za-z]:[\\/]|\\|(^|/)\.\.(/|$))")


class KnowledgeContractError(RuntimeError):
    """Explicit contract failure. Never guessed, never silently ignored."""


# --------------------------------------------------------------------------- #
# Deterministic identity primitives
# --------------------------------------------------------------------------- #


def _stable(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _digest(*parts: Any) -> str:
    return hashlib.sha256("\x1f".join(_stable(part) for part in parts).encode("utf-8",
                                                                             "replace")).hexdigest()[:32]


def normalize_locator(value: str) -> str:
    """A project-relative locator, independent of incidental path spelling.

    ``./docs/x.md``, ``docs//x.md`` and ``docs/./x.md`` are one locator. Absolute
    paths, backslashes and ``..`` escapes are invalid: a knowledge locator is never a
    machine-local absolute path.
    """
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        raise KnowledgeContractError("locator must be a non-empty project-relative path")
    if _LOCATOR_UNSAFE.search(text):
        raise KnowledgeContractError(f"locator must be project-relative (no absolute path or '..'): {value!r}")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts:
        raise KnowledgeContractError(f"locator must name a path: {value!r}")
    return "/".join(parts)


def normalize_content(text: Any) -> str:
    """Selected content normalization: line endings only.

    Nothing else is normalized, so a real content change can never be hidden while a
    CRLF/LF difference alone cannot create a spurious identity change.
    """
    if text is None:
        raise KnowledgeContractError("content must be text")
    return str(text).replace("\r\n", "\n").replace("\r", "\n")


def source_identity(*, owner: str, repository: str, locator: str, kind: str) -> str:
    """Identity of a source: owner class + authoritative repository + locator + kind.

    The locator is normalized, so path spelling alone never changes identity. The
    repository identity is supplied by ``authority.repository_identity``, so the same
    relative locator in an unrelated repository has a different source identity.
    """
    return "source:" + _digest("source", owner, repository, normalize_locator(locator), kind)


def content_fingerprint(text: Any) -> str:
    return "content:" + _digest("content", normalize_content(text))


def authority_fingerprint(source: dict[str, Any]) -> str:
    """Authority/review/lifecycle identity. An authority or lifecycle change revalidates."""
    return "authority:" + _digest("authority", source.get("authority"), source.get("review"),
                                  source.get("lifecycle"), source.get("superseded_by"))


def entry_fingerprint(entry: dict[str, Any]) -> str:
    """Identity of one selected entry: source, selected content, authority and
    requiredness, plus the fingerprints of the dependencies it actually relies on.

    A source that declares a ``revision`` pins its authority to that revision, so a
    revision change revalidates it; a source that declares none is unaffected by an
    unrelated commit.
    """
    return "entry:" + _digest("entry", entry["source_identity"], entry["content_fingerprint"],
                              entry["authority_fingerprint"], bool(entry["required"]),
                              entry.get("min_authority"), entry.get("revision"),
                              entry.get("dependency_fingerprints") or [])


def manifest_fingerprint(entries: list[dict[str, Any]], omissions: list[dict[str, Any]],
                         refusals: list[dict[str, Any]], *, scope: Optional[str]) -> str:
    """Identity of the selected manifest: the selected entries plus what was left out
    and why, so an availability or authority state change revalidates.

    The descriptive ``reason`` is recorded on the result but deliberately excluded:
    wording must never change authoritative knowledge identity across agents.
    """
    return "manifest:" + _digest("manifest", CONTRACT_VERSION, SELECTOR_VERSION,
                                 [entry["entry_fingerprint"] for entry in entries],
                                 [[record["key"], record["reason"]] for record in omissions],
                                 [[record["key"], record["reason"]] for record in refusals],
                                 scope)


def delivered_identity(*, manifest: str, delivered_content: str, omissions: list[dict[str, Any]],
                       refusals: list[dict[str, Any]], redactions: list[dict[str, Any]],
                       outcome: str) -> str:
    """Bounded delivered-context identity: what was actually delivered, what was
    omitted or redacted, and the validation outcome. This is the validated knowledge
    identity a later phase compares."""
    return "delivered-identity:" + _digest("delivered", CONTRACT_VERSION, SELECTOR_VERSION,
                                           VALIDATOR_VERSION, manifest, delivered_content,
                                           [[record["key"], record["reason"]] for record in omissions],
                                           [[record["key"], record["reason"]] for record in refusals],
                                           redactions, outcome)


# --------------------------------------------------------------------------- #
# Strict record validation
# --------------------------------------------------------------------------- #


def _unknown_fields(record: dict[str, Any], allowed: tuple[str, ...]) -> list[str]:
    return sorted(set(record) - set(allowed))


def _permission_fields(record: dict[str, Any]) -> list[str]:
    return sorted(set(record) & set(PERMISSION_GRANT_FIELDS))


def validate_source(record: Any, *, index: int) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    if not isinstance(record, dict):
        return [{"error": INVALID_DOCUMENT, "detail": f"sources[{index}] must be an object"}]
    key = str(record.get("key") or f"sources[{index}]")
    granted = _permission_fields(record)
    if granted:
        return [{"key": key, "error": PERMISSION_GRANT_REJECTED,
                 "detail": f"knowledge cannot grant: {', '.join(granted)}"}]
    unknown = _unknown_fields(record, SOURCE_FIELDS)
    if unknown:
        errors.append({"key": key, "error": UNKNOWN_FIELD,
                       "detail": f"unsupported source field: {', '.join(unknown)}"})
    for field in ("key", "owner", "kind", "locator"):
        if not record.get(field):
            errors.append({"key": key, "error": INVALID, "detail": f"source requires {field}"})
    if record.get("owner") and record["owner"] not in OWNER_CLASSES:
        errors.append({"key": key, "error": INVALID, "detail": f"unknown owner class: {record['owner']}"})
    if record.get("kind") and record["kind"] not in SOURCE_KINDS:
        errors.append({"key": key, "error": INVALID, "detail": f"unknown source kind: {record['kind']}"})
    if record.get("authority") not in AUTHORITY_ORDER:
        errors.append({"key": key, "error": INVALID, "detail": f"unknown authority class: {record.get('authority')}"})
    if record.get("review", DEFAULT_REVIEW) not in REVIEW_STATES:
        errors.append({"key": key, "error": INVALID, "detail": f"unknown review state: {record.get('review')}"})
    if record.get("lifecycle", "ACTIVE") not in LIFECYCLE_STATES:
        errors.append({"key": key, "error": INVALID, "detail": f"unknown lifecycle: {record.get('lifecycle')}"})
    if record.get("lifecycle") == "SUPERSEDED" and not record.get("superseded_by"):
        errors.append({"key": key, "error": INVALID, "detail": "SUPERSEDED requires superseded_by"})
    if record.get("lifecycle") != "SUPERSEDED" and record.get("superseded_by"):
        errors.append({"key": key, "error": INVALID, "detail": "superseded_by requires lifecycle SUPERSEDED"})
    try:
        normalize_locator(record.get("locator"))
    except KnowledgeContractError as exc:
        errors.append({"key": key, "error": INVALID, "detail": str(exc)})
    for field in ("depends_on", "supersedes", "required_semantics"):
        value = record.get(field, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            errors.append({"key": key, "error": INVALID, "detail": f"{field} must be a list of strings"})
    if "applicability" in record and not _valid_applicability(record["applicability"]):
        errors.append({"key": key, "error": INVALID, "detail": "applicability must use known facets as lists"})
    return errors


def _valid_applicability(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    for facet, wanted in value.items():
        if facet not in APPLICABILITY_FACETS:
            return False
        if not isinstance(wanted, list) or not all(isinstance(item, str) for item in wanted):
            return False
    return True


def validate_requirement(record: Any, *, index: int) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    if not isinstance(record, dict):
        return [{"error": INVALID_DOCUMENT, "detail": f"requirements[{index}] must be an object"}]
    key = str(record.get("key") or f"requirements[{index}]")
    granted = _permission_fields(record)
    if granted:
        return [{"key": key, "error": PERMISSION_GRANT_REJECTED,
                 "detail": f"a requirement cannot grant: {', '.join(granted)}"}]
    unknown = _unknown_fields(record, REQUIREMENT_FIELDS)
    if unknown:
        errors.append({"key": key, "error": UNKNOWN_FIELD,
                       "detail": f"unsupported requirement field: {', '.join(unknown)}"})
    if not record.get("key"):
        errors.append({"key": key, "error": INVALID, "detail": "requirement requires key"})
    if not isinstance(record.get("required", False), bool):
        errors.append({"key": key, "error": INVALID, "detail": "required must be a boolean"})
    minimum = record.get("min_authority", "OBSERVED")
    if minimum not in AUTHORITY_ORDER:
        errors.append({"key": key, "error": INVALID, "detail": f"unknown min_authority: {minimum}"})
    if "applicability" in record and not _valid_applicability(record["applicability"]):
        errors.append({"key": key, "error": INVALID, "detail": "applicability must use known facets as lists"})
    return errors


def validate_content(record: Any, *, key: str) -> list[dict[str, str]]:
    if not isinstance(record, dict):
        return [{"key": key, "error": INVALID, "detail": "content entry must be an object"}]
    granted = _permission_fields(record)
    if granted:
        return [{"key": key, "error": PERMISSION_GRANT_REJECTED,
                 "detail": f"content cannot grant: {', '.join(granted)}"}]
    errors = []
    unknown = _unknown_fields(record, CONTENT_FIELDS)
    if unknown:
        errors.append({"key": key, "error": UNKNOWN_FIELD,
                       "detail": f"unsupported content field: {', '.join(unknown)}"})
    if "content" not in record:
        errors.append({"key": key, "error": MISSING_CONTENT, "detail": "content is required"})
    for field in ("redactions",):
        value = record.get(field, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            errors.append({"key": key, "error": INVALID, "detail": f"{field} must be a list of strings"})
    return errors


def validate_document(document: Any) -> dict[str, Any]:
    """Strict, deterministic document validation. Returns ``{valid, errors}``."""
    errors: list[dict[str, str]] = []
    if not isinstance(document, dict):
        return {"valid": False, "errors": [{"error": INVALID_DOCUMENT, "detail": "document must be an object"}]}
    version = document.get("contract_version")
    if version != CONTRACT_VERSION:
        return {"valid": False, "errors": [{"error": UNSUPPORTED_CONTRACT_VERSION,
                                           "detail": f"expected {CONTRACT_VERSION}, got {version!r}"}]}
    unknown = _unknown_fields(document, DOCUMENT_FIELDS)
    if unknown:
        errors.append({"error": UNKNOWN_FIELD, "detail": f"unsupported document field: {', '.join(unknown)}"})
    repositories = document.get("repositories")
    if not isinstance(repositories, dict) or not repositories:
        errors.append({"error": INVALID_DOCUMENT, "detail": "repositories must map owner class -> identity"})
    else:
        for owner, identity in sorted(repositories.items()):
            if owner not in OWNER_CLASSES:
                errors.append({"error": INVALID_DOCUMENT, "detail": f"unknown owner class: {owner}"})
            if not isinstance(identity, str) or not identity.strip():
                errors.append({"error": INVALID_DOCUMENT, "detail": f"empty repository identity for {owner}"})
    for field in ("sources", "requirements"):
        if not isinstance(document.get(field), list):
            errors.append({"error": INVALID_DOCUMENT, "detail": f"{field} must be a list"})
    if not isinstance(document.get("contents", {}), dict):
        errors.append({"error": INVALID_DOCUMENT, "detail": "contents must be an object"})
    if errors:
        return {"valid": False, "errors": errors}

    seen: set[str] = set()
    for index, record in enumerate(document["sources"]):
        errors.extend(validate_source(record, index=index))
        key = (record or {}).get("key")
        if key and key in seen:
            errors.append({"key": key, "error": DUPLICATE_KEY, "detail": "duplicate source key"})
        seen.add(key)
    keys: set[str] = set()
    for index, record in enumerate(document["requirements"]):
        errors.extend(validate_requirement(record, index=index))
        key = (record or {}).get("key")
        if key and key in keys:
            errors.append({"key": key, "error": DUPLICATE_KEY, "detail": "duplicate requirement key"})
        keys.add(key)
    for key, record in sorted(document.get("contents", {}).items()):
        errors.extend(validate_content(record, key=key))
    return {"valid": not errors, "errors": errors}


def index_sources(document: dict[str, Any]) -> dict[str, Any]:
    return {record["key"]: record for record in document.get("sources", []) if record.get("key")}


# --------------------------------------------------------------------------- #
# Applicability
# --------------------------------------------------------------------------- #


def applies(applicability: Optional[dict[str, Any]], context: Optional[dict[str, Any]]) -> tuple[bool, Optional[str]]:
    """Every declared facet must match the context. An absent facet is unconstrained.

    Returns ``(applicable, unmatched_facet)`` so a decision is always inspectable.
    """
    if not applicability:
        return True, None
    context = context or {}
    for facet, wanted in sorted(applicability.items()):
        actual = context.get(APPLICABILITY_FACETS[facet])
        if actual is None or actual not in wanted:
            return False, facet
    return True, None


def authority_satisfies(source: dict[str, Any], min_authority: str) -> Optional[str]:
    """``None`` when the source clears the requirement, else the limiting reason."""
    strength = source.get("authority", "OBSERVED")
    if AUTHORITY_ORDER.index(strength) < AUTHORITY_ORDER.index(min_authority):
        return AUTHORITY_INCOMPATIBLE
    if min_authority == "AUTHORITATIVE" and source.get("review", DEFAULT_REVIEW) != "REVIEWED":
        return UNREVIEWED
    return None


# --------------------------------------------------------------------------- #
# Selection + delivery
# --------------------------------------------------------------------------- #


def _ineligible(source: Optional[dict[str, Any]], contents: dict[str, Any], *,
                min_authority: str, context: dict[str, Any],
                requirement_applicable: bool) -> Optional[tuple[str, str]]:
    """First decisive ineligibility, in a fixed evaluation order, as
    ``(reason, level)``.

    Applicability is a *scope* decision (the requirement or the source says it does
    not apply to this context), so it is recorded as an inspectable omission rather
    than an availability refusal. Requiredness governs availability: missing,
    superseded, revoked, authority-incompatible, missing content, insufficient
    redaction and unavailable dependencies refuse when required.
    """
    if not requirement_applicable:
        return NOT_APPLICABLE, "requirement"
    if source is None:
        return MISSING, "source"
    applicable, _facet = applies(source.get("applicability"), context)
    if not applicable:
        return NOT_APPLICABLE, "source"
    if source.get("lifecycle") == "SUPERSEDED":
        return SUPERSEDED, "source"
    if source.get("lifecycle") == "REVOKED":
        return REVOKED, "source"
    limiting = authority_satisfies(source, min_authority)
    if limiting:
        return limiting, "source"
    content = contents.get(source["key"])
    if content is None:
        return MISSING_CONTENT, "source"
    redactions = set(content.get("redactions") or [])
    if redactions & set(source.get("required_semantics") or []):
        return REDACTION_INSUFFICIENT, "source"
    return None


def select(document: dict[str, Any], *, context: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Deterministically select, validate and identify the knowledge for one context.

    Pure: the caller supplies the document (with repository identities and content).
    Required knowledge that is missing, invalid, superseded, revoked,
    authority-incompatible or insufficiently redacted yields
    ``REQUIRED_CONTEXT_UNAVAILABLE``; optional sources are omitted with an
    inspectable reason. Nothing partial is ever delivered silently.
    """
    context = dict(context if context is not None else document.get("context") or {})
    validation = validate_document(document)
    scope = document.get("scope") if isinstance(document, dict) else None
    reason = document.get("reason") if isinstance(document, dict) else None
    if not validation["valid"]:
        refusals = [{"key": record.get("key"), "reason": record["error"],
                     "detail": record["detail"]} for record in validation["errors"]]
        return _assemble(entries=[], omissions=[], refusals=refusals, scope=scope, reason=reason,
                         context=context, redactions=[])

    sources = index_sources(document)
    contents = document.get("contents") or {}

    base: dict[str, dict[str, Any]] = {}
    refusals: list[dict[str, Any]] = []
    omissions: list[dict[str, Any]] = []
    for requirement in sorted(document["requirements"], key=lambda item: item["key"]):
        key = requirement["key"]
        required = bool(requirement.get("required", False))
        min_authority = requirement.get("min_authority", "OBSERVED")
        source = sources.get(key)
        requirement_applicable, _facet = applies(requirement.get("applicability"), context)
        failure = _ineligible(source, contents, min_authority=min_authority, context=context,
                              requirement_applicable=requirement_applicable)
        if failure is None:
            base[key] = {"requirement": requirement, "source": source, "content": contents[key],
                         "required": required, "min_authority": min_authority,
                         "identity": source_identity(
                             owner=source["owner"],
                             repository=document["repositories"][source["owner"]],
                             locator=source["locator"], kind=source["kind"])}
            continue
        reason, level = failure
        record = {"key": key, "reason": reason, "level": level, "required": required,
                  "scope": requirement.get("scope"),
                  "detail": _detail(reason, source, key)}
        if reason == NOT_APPLICABLE:
            omissions.append(record)  # scope decision, never a silent drop
        else:
            (refusals if required else omissions).append(record)

    entries = _attach_dependencies(base, refusals, omissions)

    delivered: list[dict[str, Any]] = []
    redaction_meta: list[dict[str, Any]] = []
    for key in sorted(entries):
        entry = entries[key]
        source = entry["source"]
        content = entry["content"]
        delivered_text = normalize_content(content.get("delivered") if content.get("delivered") is not None
                                           else content["content"])
        redactions = sorted(set(content.get("redactions") or []))
        if redactions:
            redaction_meta.append({"key": key, "redactions": redactions})
        delivered.append({
            "key": key, "source_identity": entry["identity"],
            "content_fingerprint": content_fingerprint(content["content"]),
            "delivered_content_fingerprint": content_fingerprint(delivered_text),
            "authority": source.get("authority", "OBSERVED"),
            "review": source.get("review", DEFAULT_REVIEW),
            "lifecycle": source.get("lifecycle", "ACTIVE"),
            "revision": source.get("revision") or content.get("revision"),
            "required": entry["required"], "min_authority": entry["min_authority"],
            "scope": entry["requirement"].get("scope"),
            "select_reason": entry["requirement"].get("reason"),
            "authority_fingerprint": authority_fingerprint(source),
            "dependency_fingerprints": entry.get("dependency_fingerprints") or [],
        })
    for record in delivered:
        record["entry_fingerprint"] = entry_fingerprint(record)
    return _assemble(entries=delivered, omissions=sorted(omissions, key=lambda item: item["key"]),
                     refusals=sorted(refusals, key=lambda item: item["key"]), scope=scope,
                     reason=reason, context=context, redactions=redaction_meta)


def _attach_dependencies(base: dict[str, dict[str, Any]], refusals: list[dict[str, Any]],
                         omissions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Attach the fingerprints of the dependencies a selected entry actually relies on.

    A declared dependency that is not selected makes the dependent entry unavailable
    (refused when required, omitted when optional) rather than silently dropped.
    """
    entries: dict[str, dict[str, Any]] = {}
    for key in sorted(base):
        record = base[key]
        fingerprints: list[str] = []
        unavailable: Optional[str] = None
        for dependency in sorted(set(record["source"].get("depends_on") or [])):
            other = base.get(dependency)
            if other is None:
                unavailable = dependency
                break
            fingerprints.append("dependency:" + _digest(
                "dependency", other["identity"], content_fingerprint(other["content"]["content"])))
        if unavailable is None:
            entries[key] = {**record, "dependency_fingerprints": fingerprints}
            continue
        problem = {"key": key, "reason": DEPENDENCY_UNAVAILABLE, "required": record["required"],
                   "scope": record["requirement"].get("scope"),
                   "detail": f"declared dependency {unavailable!r} is not available in this selection"}
        (refusals if record["required"] else omissions).append(problem)
    return entries


def _detail(failure: str, source: Optional[dict[str, Any]], key: str) -> str:
    if failure == MISSING:
        return f"no source declares {key!r}"
    if failure == SUPERSEDED:
        return f"{key!r} was superseded by {source.get('superseded_by')!r}"
    if failure == REVOKED:
        return f"{key!r} is revoked"
    if failure == NOT_APPLICABLE:
        return f"{key!r} does not apply to this context"
    if failure in (AUTHORITY_INCOMPATIBLE, UNREVIEWED):
        return f"{key!r} is {source.get('authority')}/{source.get('review')}"
    if failure == MISSING_CONTENT:
        return f"selected content for {key!r} was not supplied"
    if failure == REDACTION_INSUFFICIENT:
        return f"redaction removed required semantics of {key!r}"
    return f"{key!r} is not available: {failure}"


def _assemble(*, entries: list[dict[str, Any]], omissions: list[dict[str, Any]],
              refusals: list[dict[str, Any]], scope: Optional[str], reason: Optional[str],
              context: dict[str, Any], redactions: list[dict[str, Any]]) -> dict[str, Any]:
    manifest = manifest_fingerprint(entries, omissions, refusals, scope=scope)
    delivered_content = "delivered:" + _digest(
        "delivered-content", [record["delivered_content_fingerprint"] for record in entries])
    outcome = REQUIRED_CONTEXT_UNAVAILABLE if refusals else READY
    document = {
        "contract_version": CONTRACT_VERSION,
        "selector_version": SELECTOR_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "scope": scope,
        "reason": reason,
        "context": context,
        "entries": entries,
        "omissions": omissions,
        "refusals": refusals,
        "outcome": outcome,
        "manifest_fingerprint": manifest,
        "delivered_content_fingerprint": delivered_content,
    }
    document["delivered_identity"] = delivered_identity(
        manifest=manifest, delivered_content=delivered_content, omissions=omissions,
        refusals=refusals, redactions=redactions, outcome=outcome)
    return document


# --------------------------------------------------------------------------- #
# Task identity vs delivered knowledge identity
# --------------------------------------------------------------------------- #


def task_identity(*, objective_identity: str, acceptance_identity: str, permissions_identity: str,
                  requirements_identity: str) -> dict[str, Any]:
    """TASK identity: the authoritative objective, acceptance contract,
    permissions/tool policy and declared knowledge requirements. Kept separate from
    the delivered knowledge identity so neither is collapsed into one opaque hash."""
    components = {"objective": objective_identity, "acceptance": acceptance_identity,
                  "permissions": permissions_identity, "requirements": requirements_identity}
    return {**components, "task_identity": "task:" + _digest("task", components)}


def active_work_equivalence_input(*, task: dict[str, Any], knowledge_identity: str,
                                  execution_compatibility: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The equivalence input a later Master B phase consumes. Identity only: this
    module implements no claim, lock, wait/join, lease or heartbeat."""
    compatibility = dict(execution_compatibility or {})
    payload = {"task_identity": task.get("task_identity"), "knowledge_identity": knowledge_identity,
               "execution_compatibility": compatibility}
    return {**payload, "equivalence_identity": "equivalence:" + _digest("equivalence", payload)}


def equivalence_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return before.get("equivalence_identity") != after.get("equivalence_identity")


def reuse_eligible(*, accepted_knowledge_identity: str, current_knowledge_identity: str) -> bool:
    """Contract-level reuse predicate for a later phase: an accepted result is reusable
    only under the validated identity it was accepted with. This changes no
    production cache/resume behavior."""
    return bool(accepted_knowledge_identity) and accepted_knowledge_identity == current_knowledge_identity


# --------------------------------------------------------------------------- #
# Cross-agent semantic parity
# --------------------------------------------------------------------------- #


def agent_semantic_inputs(document: dict[str, Any]) -> dict[str, Any]:
    """The authoritative semantic inputs of a document, with agent branding and prompt
    wrapper text removed. Two agents asking the same thing must produce the same
    value here, and therefore the same delivered identity."""
    if not isinstance(document, dict):
        raise KnowledgeContractError("document must be an object")
    return {
        "contract_version": document.get("contract_version"),
        "repositories": document.get("repositories"),
        "sources": sorted(document.get("sources") or [], key=lambda item: str(item.get("key"))),
        "requirements": sorted(document.get("requirements") or [], key=lambda item: str(item.get("key"))),
        "contents": {key: value for key, value in sorted((document.get("contents") or {}).items())},
        "context": {key: value for key, value in sorted((document.get("context") or {}).items())},
        "scope": document.get("scope"),
        # The descriptive selection reason is intentionally omitted: prompt wording and
        # agent branding must not alter authoritative semantic identity.
    }


def semantic_parity(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    """Compare the authoritative semantic inputs of two agents' requests."""
    left, right = agent_semantic_inputs(first), agent_semantic_inputs(second)
    differences = sorted(field for field in set(left) | set(right) if left.get(field) != right.get(field))
    return {"parity": not differences, "differences": differences,
            "semantic_fingerprint": "semantic:" + _digest("semantic", right)}
