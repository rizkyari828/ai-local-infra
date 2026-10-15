"""Bounded project-file consumer of the frozen Shared Knowledge Contract. No inference."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import stat

import authority
import context_retrieval
import knowledge_contract as kc
import learning

VERSION = 'shared-knowledge-runtime-v1'
REGISTRY = '.local-ai/knowledge.json'
LIMITS = {'max_sources': 16, 'max_source_bytes': 65536,
          'max_bundle_bytes': 16384, 'max_bundle_chars': 16384}
MAX_REGISTRY_BYTES = 131072
MAX_REGISTERED_SOURCES = 128
DENIED_PARTS = {'.git', '.ssh', '.aws', 'sessions', 'transcripts', 'scratchpad',
                'reasoning', 'credentials', 'token-store', 'private-keys'}


class KnowledgeRuntimeError(RuntimeError):
    """Content-free refusal code; never expose a rejected source in diagnostics."""


def stable(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(stable(value).encode('utf-8')).hexdigest()


def _read(root, locator, bound):
    # Never follow even an internal symlink: open every component relative to held
    # directory descriptors, so a check/open path replacement cannot escape ownership.
    if not isinstance(locator, str) or '\\' in locator:
        raise KnowledgeRuntimeError('KNOWLEDGE_PATH_UNSAFE')
    try:
        locator = kc.normalize_locator(locator)
    except kc.KnowledgeContractError:
        raise KnowledgeRuntimeError('KNOWLEDGE_PATH_UNSAFE') from None
    parts = locator.split('/')
    if any(part.lower() in DENIED_PARTS or pathlib.PurePosixPath(part).stem.lower() in {
               'credentials', 'secrets', 'tokens', 'token', 'scratchpad', 'reasoning', 'transcript', 'session'}
           or part.lower().startswith(('.env', 'id_rsa', 'id_ed25519'))
           or part.lower().endswith(('.pem', '.key')) for part in parts):
        raise KnowledgeRuntimeError('KNOWLEDGE_PATH_UNSAFE')
    descriptors = []
    try:
        current = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(current)
        for i, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if i < len(parts) - 1:
                flags |= os.O_DIRECTORY
            current = os.open(part, flags, dir_fd=current)
            descriptors.append(current)
        if not stat.S_ISREG(os.fstat(current).st_mode):
            raise KnowledgeRuntimeError('KNOWLEDGE_PATH_UNSAFE')
        with os.fdopen(os.dup(current), 'rb') as stream:
            data = stream.read(bound + 1)
        if len(data) > bound:
            raise KnowledgeRuntimeError('KNOWLEDGE_SOURCE_TOO_LARGE')
        text = data.decode('utf-8')
        if '\x00' in text:
            raise KnowledgeRuntimeError('KNOWLEDGE_CONTENT_INVALID')
        return text
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError):
        raise KnowledgeRuntimeError('KNOWLEDGE_PATH_UNSAFE') from None
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise KnowledgeRuntimeError('KNOWLEDGE_DUPLICATE_JSON_KEY')
        result[key] = value
    return result


def _validate(document, repository):
    try:
        if not isinstance(document, dict) or document.get('contents'):
            raise ValueError()
        if document.get('repositories') != {'project': repository}:
            raise KnowledgeRuntimeError('KNOWLEDGE_REPOSITORY_MISMATCH')
        sources, requirements = document.get('sources'), document.get('requirements')
        if not isinstance(sources, list) or len(sources) > MAX_REGISTERED_SOURCES:
            raise ValueError()
        if not isinstance(requirements, list) or len(requirements) > MAX_REGISTERED_SOURCES:
            raise ValueError()
        if not all(isinstance(r, dict) and isinstance(r.get('key'), str)
                   and 0 < len(r['key']) <= 128 for r in sources + requirements):
            raise ValueError()
        if any(s.get('owner') != 'project' or not isinstance(s.get('locator'), str) for s in sources):
            raise KnowledgeRuntimeError('KNOWLEDGE_OWNER_INVALID')
        if not kc.validate_document(document)['valid']:
            raise ValueError()
        identities = [kc.source_identity(owner=s['owner'], repository=repository,
                      locator=s['locator'], kind=s['kind']) for s in sources]
        if len(set(identities)) != len(identities):
            raise KnowledgeRuntimeError('KNOWLEDGE_SOURCE_AMBIGUOUS')
    except KnowledgeRuntimeError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError):
        raise KnowledgeRuntimeError('KNOWLEDGE_METADATA_INVALID') from None


def evidence(snapshot):
    """Persist identities/provenance, never source bodies or the registration document."""
    b = snapshot['bundle']
    result = {key: copy.deepcopy(b[key]) for key in (
        'bundle_version', 'repository', 'workspace', 'context', 'scope', 'selection_identity',
        'delivered_identity', 'delivered_content_fingerprint', 'bundle_fingerprint', 'provenance',
        'omissions', 'source_disposition')}
    for entry in result['provenance']:
        entry.pop('select_reason', None)  # explanatory prose is not execution authority
    return result


def resolve(root, node=None):
    """Read one explicit registry and its selected allowlisted files. No recursive scan.

    Requirements/dependencies retain the frozen selector semantics: a dependency must
    itself have a declared requirement; existence alone never silently selects it.
    No registry and no task obligations means this runtime is inactive.
    """
    root = authority.canonical_workspace(pathlib.Path(root))
    repo = authority.repository_identity(root)
    node = node or {}
    if not isinstance(node.get('knowledge_requirements', []), list):
        raise KnowledgeRuntimeError('KNOWLEDGE_REQUIREMENT_INVALID')
    limits = dict(LIMITS)
    requested = node.get('knowledge_limits', {})
    if (not isinstance(requested, dict) or set(requested) - set(limits)
            or any(type(v) is not int or not 0 < v <= limits[k] for k, v in requested.items())):
        raise KnowledgeRuntimeError('KNOWLEDGE_LIMIT_INVALID')
    limits.update(requested)
    try:
        raw = _read(root, REGISTRY, MAX_REGISTRY_BYTES)
    except FileNotFoundError:
        if not node.get('knowledge_requirements'):
            return None
        document = {'contract_version': kc.CONTRACT_VERSION, 'repositories': {'project': repo['id']},
                    'sources': [], 'requirements': [], 'contents': {}}
    else:
        if learning.contains_secret_like(raw):
            raise KnowledgeRuntimeError('KNOWLEDGE_METADATA_SENSITIVE')
        try:
            document = json.loads(raw, object_pairs_hook=_object)
        except (ValueError, RecursionError):
            raise KnowledgeRuntimeError('KNOWLEDGE_METADATA_INVALID') from None
    _validate(document, repo['id'])
    document = copy.deepcopy(document)
    additional = node.get('knowledge_requirements', [])
    if not isinstance(additional, list):
        raise KnowledgeRuntimeError('KNOWLEDGE_REQUIREMENT_INVALID')
    by_key = {r['key']: r for r in document['requirements']}
    if len({r.get('key') for r in additional if isinstance(r, dict) and isinstance(r.get('key'), str)}) != len(additional):
        raise KnowledgeRuntimeError('KNOWLEDGE_REQUIREMENT_INVALID')
    for requirement in additional:
        if not isinstance(requirement, dict) or not isinstance(requirement.get('key'), str):
            raise KnowledgeRuntimeError('KNOWLEDGE_REQUIREMENT_INVALID')
        key = requirement['key']
        if key in by_key and kc._semantic_requirement(by_key[key]) != kc._semantic_requirement(requirement):
            raise KnowledgeRuntimeError('KNOWLEDGE_REQUIREMENT_CONFLICT')
        by_key[key] = requirement
    document['requirements'] = list(by_key.values())
    _validate(document, repo['id'])
    context = {'task_family': node.get('task_family'), 'task_class': node.get('task_class')}
    extra = node.get('knowledge_context', {})
    if (not isinstance(extra, dict) or set(extra) - {'language', 'surface'}
            or any(not isinstance(v, str) for v in extra.values())):
        raise KnowledgeRuntimeError('KNOWLEDGE_CONTEXT_INVALID')
    context.update(extra)
    if learning.contains_secret_like(stable(document) + stable(context)):
        raise KnowledgeRuntimeError('KNOWLEDGE_METADATA_SENSITIVE')
    document['context'] = context
    document['contents'] = {}
    sources = kc.index_sources(document)
    disposition = []
    for requirement in sorted(document['requirements'], key=lambda r: r['key']):
        source = sources.get(requirement['key'])
        if (not kc.applies(requirement.get('applicability'), context)[0] or source is None
                or not kc.applies(source.get('applicability'), context)[0]
                or source.get('lifecycle', 'ACTIVE') != 'ACTIVE'
                or kc.authority_satisfies(source, requirement.get('min_authority', 'OBSERVED'))):
            continue
        try:
            text = _read(root, source['locator'], limits['max_source_bytes'])
            safe, changed = context_retrieval._redact(text)
            if changed:
                # The generic secret detector cannot prove which semantics survived.
                # Withhold the whole source rather than claiming sufficient redaction.
                raise KnowledgeRuntimeError('REDACTION_SUFFICIENCY_UNPROVEN')
            document['contents'][source['key']] = {'content': kc.normalize_content(safe)}
        except (FileNotFoundError, KnowledgeRuntimeError) as exc:
            disposition.append({'key': source['key'], 'reason': 'MISSING_FILE' if isinstance(exc, FileNotFoundError) else str(exc)})
    selected = kc.select(document, context=context)
    if selected['outcome'] != kc.READY:
        raise KnowledgeRuntimeError(kc.REQUIRED_CONTEXT_UNAVAILABLE)

    def items(selection):
        return [{'key': e['key'], 'source_identity': e['source_identity'],
                 'content': document['contents'][e['key']]['content']}
                for e in selection['entries']]

    def fits(selection):
        encoded = stable(items(selection))
        return (len(selection['entries']) <= limits['max_sources']
                and len(encoded) <= limits['max_bundle_chars']
                and len(encoded.encode('utf-8')) <= limits['max_bundle_bytes'])

    # ponytail: at most 128 explicitly registered sources; repeat the frozen selector
    # after optional omissions rather than introducing another dependency solver.
    while not fits(selected):
        for entry in reversed(selected['entries']):
            if entry['required']:
                continue
            content = document['contents'].pop(entry['key'])
            reduced = kc.select(document, context=context)
            if reduced['outcome'] == kc.READY:
                disposition.append({'key': entry['key'], 'reason': 'CONTEXT_BOUND'})
                selected = reduced
                break
            document['contents'][entry['key']] = content
        else:
            raise KnowledgeRuntimeError('KNOWLEDGE_BUDGET_INSUFFICIENT')
    delivered = items(selected)
    bundle = {'bundle_version': VERSION, 'repository': repo['id'], 'workspace': repo['workspace_instance'],
        'context': context, 'scope': selected['scope'], 'outcome': selected['outcome'],
        'selection_identity': selected['manifest_fingerprint'], 'delivered_identity': selected['delivered_identity'],
        'delivered_content_fingerprint': selected['delivered_content_fingerprint'],
        'provenance': selected['entries'], 'omissions': selected['omissions'],
        'source_disposition': sorted(disposition, key=lambda d: d['key']), 'items': delivered}
    bundle['bundle_fingerprint'] = digest({k: bundle[k] for k in (
        'bundle_version', 'repository', 'workspace', 'context', 'scope', 'delivered_identity')}
        | {'items': delivered})
    return {'document': document, 'selection': selected, 'bundle': bundle}
