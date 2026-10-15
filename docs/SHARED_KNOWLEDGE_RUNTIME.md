# Shared Knowledge Runtime V0.1

The runtime is a project-file consumer of the frozen Shared Knowledge Contract.
It delegates validation, selection, applicability, dependency closure, revocation,
supersession and all semantic identities to `knowledge_contract.py`. It performs
no inference, classification, indexing, generation, or autonomous promotion.

## Registration and ownership

A project explicitly registers files in `.local-ai/knowledge.json`. This is an
ordinary frozen-contract document, with empty/absent `contents`: the runtime fills
contents from registered locators. There is no directory crawl. Skills under
`.claude/skills/` and files under `references/` are eligible only if registered.
The `repositories` mapping must contain only `project`, with the exact canonical
identity returned by `authority.repository_identity(project_root)['id']`.
Remote-less identities stay machine-local; copying a registration to another
repository does not authorize it. Other owner classes/external repositories are
not enabled by this runtime version. The project owns and reviews its metadata;
there is no cryptographic reviewer identity or automatic review promotion.

The registry is read relative to the authorized workspace. Every path component
is opened with held directory descriptors and no symlink following. Absolute
paths, traversal, credential paths, private keys, session/transcript/reasoning
stores, nonregular files, oversized files, duplicate JSON keys and ambiguous
source identities are refused. Even internal source symlinks are deliberately
unsupported. Targets are macOS and Linux/WSL, using Python standard library only.

## Selection and bounds

Requirements in the registry can use frozen applicability facets. Tasks may add
`knowledge_requirements` using the same contract fields, but cannot contradict
or weaken registry requirements. `knowledge_context` supplies explicit `language`
and `surface`; task family/class come from the task. Agent branding does not
implicitly change knowledge authority. A dependency must have its own declared
requirement, as required by the frozen contract; its mere file existence never
silently selects it. Superseded sources are refused/omitted, never automatically
substituted with their replacements.

Defaults: 128 registered sources/requirements, 128 KiB registry, 64 KiB per source,
16 delivered sources, 16 KiB UTF-8 delivery and 16,384 delivery characters. Tasks
can narrow delivery/source limits via `knowledge_limits`; they cannot enlarge the
hard bounds. Payload bounds count the deterministic serialized delivery items,
including their labels. Optional entries are omitted deterministically only when
the frozen selector still satisfies every required obligation and dependency.
Required content is never truncated or summarized to fit.

The existing secret detector/redactor is reused. If it changes a source, this
version withholds the whole source: a generic secret detector cannot prove which
required semantics survived. Required obligations then fail; optional omission
records the redaction-sufficiency reason. No rejected raw text enters the bundle.
These are representative secret checks, not a substitute for project curation.

## Bundle and provenance

`knowledge_runtime.resolve(workspace, task)` returns the hydrated contract document,
the frozen selection and a versioned deterministic bundle, or no snapshot when
no registry/obligations exist. A failure raises a content-free refusal code.
The bundle carries canonical repository/workspace, applicability, source and
closure provenance, selection identity, delivered validated identity, omissions,
redaction disposition and exact normalized delivered text. Line endings alone are
normalized, per contract. `evidence(snapshot)` strips source bodies and explanatory
requirement prose, retaining the fingerprints and provenance needed for adoption.
Selected and delivered identities remain distinct. No second database is created.

## Synthetic example

A task needs `api` and `architecture`; architecture depends on api. Register both
files and both requirements (replace REPOSITORY_ID with the real canonical id):

```json
{
  "contract_version": "shared-knowledge-contract-v1",
  "repositories": {"project": "REPOSITORY_ID"},
  "sources": [
    {"key":"api","owner":"project","kind":"api","locator":"references/api.md",
     "authority":"AUTHORITATIVE","review":"REVIEWED","lifecycle":"ACTIVE"},
    {"key":"architecture","owner":"project","kind":"architecture",
     "locator":"references/architecture.md","authority":"AUTHORITATIVE",
     "review":"REVIEWED","lifecycle":"ACTIVE","depends_on":["api"]}
  ],
  "requirements": [
    {"key":"api","required":true,"min_authority":"AUTHORITATIVE"},
    {"key":"architecture","required":true,"min_authority":"AUTHORITATIVE"}
  ]
}
```

With synthetic contents `API version 1` and `Architecture uses API version 1`, the
runtime validates ownership, review, applicability and closure before delivering
both facts. Changing api content changes the closure and delivered knowledge
identity even with the same filename and repository HEAD. Incompatible work must
be revalidated rather than adopted under the old identity.

Phase A core gate: focused file-consumer tests plus the unchanged frozen-contract
suite. Production integration and recovery have a separate Phase B gate.
