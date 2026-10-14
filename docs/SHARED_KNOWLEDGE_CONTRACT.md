# Shared Knowledge Contract

`scripts/common/knowledge_contract.py` defines **what** a knowledge/context
requirement is, how a selection is identified, and when delivery must be refused.
It is a protocol, not a runtime: it reads no file, runs no Git command, calls no
provider, and holds no clock.

## Ownership

| Layer | Owner |
|---|---|
| Protocol, versioning, identity rules, refusal semantics | ai-local-infra (this repository) |
| Architecture, decision, API, skill and finding content | the project repository |
| Machine-local notes | the machine's descriptor/notes |
| Reviewed derived observations | the learning/telemetry stream (later phase) |

Content is supplied to the contract by the caller; repository identity is supplied
by [`authority.py`](RUNTIME_AUTHORITY.md), never invented here. A knowledge record
**can never grant** provider permission, machine permission, a `local_only`
override, mutation permission, tool permission or workspace authority — those keys
are absent from the schema, so a record that declares one is *invalid*
(`permission_grant_rejected`), not powerful.

## Document

One contract document describes one selection for one context:

```
contract_version, repositories{owner → repository identity},
sources[], requirements[], contents{}, scope, reason, context{}
```

- **source** — `key`, `owner` (owner class), `kind`, `locator` (project-relative),
  `authority` (`OBSERVED`/`SUPPORTING`/`AUTHORITATIVE`), `review`
  (`REVIEWED`/`UNREVIEWED`/`NOT_APPLICABLE`, default `UNREVIEWED` — fail-closed),
  `lifecycle` (`ACTIVE`/`SUPERSEDED`/`REVOKED`), `superseded_by`, `supersedes`,
  `depends_on`, `required_semantics`, optional `revision`, optional `applicability`.
- **requirement** — `key`, `required`, `scope`, `reason`, `min_authority`, optional
  `applicability`.
- **content** — `content`, optional `delivered` (post-redaction text), `redactions`
  (semantic tags removed), optional `revision`.

Unknown fields are rejected. A locator must be project-relative: absolute paths,
backslashes and `..` escapes are invalid.

## Identity

```
source:<hash>       owner + repository identity + normalized locator + kind
content:<hash>      the selected content (line endings normalized, nothing else)
authority:<hash>    authority + review + lifecycle + supersession reference
entry:<hash>        source + content + authority + requiredness + min_authority
                    + requirement semantic scope + declared revision
                    + the fingerprints of the whole dependency closure
manifest:<hash>     contract/selector version + entry fingerprints + omissions/refusals
                    (key + reason) + scope
delivered-content   the post-redaction delivered text
delivered-identity  version + manifest + delivered content + omissions/refusals
                    + redaction metadata + validation outcome
```

Rules this shape proves:

| Change | Effect |
|---|---|
| selected content changes | `content` → `entry` → `manifest` → `delivered-identity` change |
| an unselected/unrelated source or content changes | nothing changes (no repository-wide hashing) |
| path spelling (`./x`, `x//y`, `x/./y`) | same locator, same identity |
| different authoritative repository, same relative locator | different `source` identity |
| requiredness changes | validation and identity change |
| authority/review/lifecycle changes | revalidation; identity changes |
| superseded/revoked | ineligible (refused when required, omitted when optional) |
| declared `revision` changes | revalidates; an unrelated commit does not |
| a dependency anywhere in the closure changes | the dependent entry's identity changes |
| a dependency in the closure becomes unavailable/revoked/inapplicable | the dependent becomes unavailable, transitively |
| requirement semantic `scope` changes (`api:payments` ≠ `api:refunds`) | `entry` → `manifest` → `delivered-identity` change |
| document `scope` changes | identity changes (different knowledge was requested) |
| selection prose (`reason`) wording changes | identity unchanged (prose is not authority) |
| redaction removes a required semantic tag | refusal (`redaction_insufficient`) |

## Dependency closure

A source may satisfy a requirement only when **its entire declared dependency
closure is available, valid and acyclic**:

- an unavailable dependency (missing, invalid, revoked, superseded, inapplicable,
  or itself withdrawn) withdraws every affected ancestor, transitively;
- a required ancestor that is withdrawn refuses the whole selection
  (`REQUIRED_CONTEXT_UNAVAILABLE`); an optional ancestor is omitted with the same
  inspectable reason;
- dependency cycles can never be satisfied and fail safely as explicit
  `dependency_cycle` failures, never as a silent `READY`;
- a dependency that exists but is not itself selected does **not** satisfy the
  dependent: surviving validation, not mere existence, is what counts;
- `dependency_fingerprints` covers the whole closure, so a change anywhere in it
  revalidates the dependent entry.

## Refusal

Any failure affecting **required** knowledge yields the outcome
`REQUIRED_CONTEXT_UNAVAILABLE`; nothing partial is delivered silently. Reasons:
`missing`, `superseded`, `revoked`, `authority_incompatible`, `unreviewed`,
`missing_content`, `redaction_insufficient`, `dependency_unavailable`,
`dependency_cycle`, plus document-level `permission_grant_rejected`,
`unknown_field`, `invalid`, `duplicate_key`, `unsupported_contract_version`,
`invalid_document`.

Optional sources are omitted deterministically with an inspectable reason
(`omissions[]`).

**Applicability has two distinct levels and they are never conflated:**

| case | meaning | outcome |
|---|---|---|
| the *requirement* does not apply to this context | the obligation does not exist for this task | deterministic non-selection (recorded omission, `level: requirement`) |
| the requirement applies but the *source* does not | the obligation exists and cannot be satisfied | **refusal** when required, omission when optional (`reason: not_applicable`, `level: source`) |

An omission is never a silent substitute for an obligation.

## Task identity vs delivered knowledge identity

Kept deliberately separate so neither is collapsed into one opaque hash:

```
task identity        objective + acceptance + permissions/tool policy + declared requirements
delivered identity   the exact selected knowledge, requiredness, authority,
                     provenance, fingerprints, redaction/omission metadata, outcome
```

## Active-work equivalence input

```
task identity + validated knowledge identity + execution compatibility
    = active-work equivalence input
```

`active_work_equivalence_input` and `equivalence_changed` are **identity only**.
V0.1 implements no claim, lock, wait/join, lease, heartbeat or recovery — those
belong to Master B. `reuse_eligible(accepted, current)` is a contract-level
predicate: an accepted result is reusable only under the validated identity it was
accepted with. It changes no production cache/resume behaviour.

## Cross-agent parity

`agent_semantic_inputs` reduces a document to its authoritative semantic inputs
(versions, repositories, sources, requirements, contents, context, scope) and
`semantic_parity` compares two agents' requests. Prompt wrapper text and agent
branding live **outside** the contract document; a document that carries them is
invalid, so branding can never silently alter authoritative knowledge identity.
The descriptive `reason` is excluded at **both** levels (document and
requirement), so two agents that phrase the same requirement differently still
produce one authoritative identity, while every authority-bearing field —
repository, source, requirement key, requiredness, minimum authority,
applicability and semantic scope — is compared. Codex and OpenCode must therefore
produce the same `delivered_identity` for equivalent requirements. Guidance that is loaded automatically and materially
affects execution is expected to participate in effective input identity in a
later phase; V0.1 only defines the requirement.

## Versioning

`contract_version`, `selector_version` and `validator_version` are recorded on
every result; an unknown contract version is refused rather than interpreted.

## Fixtures

`tests/test_knowledge_contract.py` is the deterministic offline fixture suite:
F1 selected content change · F2 unselected change · F3 missing required · F4
missing optional · F5 optional→required · F6 supersession/revocation · F7
review/applicability change · F8 redaction insufficiency · F9 path alias · F10
unrelated repository, same locator · F11 knowledge change while waiting · F12
Codex/OpenCode parity · F13 knowledge cannot grant permission, plus `REUSE`
(accepted K1 vs current K2 ⇒ not reusable).

Fix Pass 1 additions: the dependency matrix `D1`–`D7` (valid closure, transitive
withdrawal of required and optional ancestors, revoked/superseded/inapplicable
dependencies, cycles, closure-content and closure-authority invalidation) and the
adversarial regressions `A1`–`A10` (transitive propagation, cycle safety,
applicability obligation versus scope, requirement-scope identity, reason-wording
parity, equivalence and reuse under a scope change), with scope fixtures `S1`–`S4`.

```bash
python3 -m unittest tests.test_knowledge_contract
```
