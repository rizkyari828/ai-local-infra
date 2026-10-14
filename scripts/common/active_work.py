"""Master B: same-machine durable work, using the existing operational SQLite DB.

Claims authorize publication, never provider permission. No inference runs in a
transaction. Accepted results precede terminal publication; interrupted mutations
retain exclusion until an explicit verified reconciliation.
"""
from __future__ import annotations

import copy
import contextlib
import hashlib
import json
import math
import os
import pathlib
import threading
import time
import uuid

import authority
import knowledge_contract as kc
import orchestration_store as ops
import result_cache

VERSION = 'master-b-v1'
BUSY = ('CLAIMED', 'RUNNING', 'OUTCOME_UNCERTAIN')


class WorkError(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def identity(node, root, context, dependencies, constraints, resolution, backend, knowledge):
    """Consume frozen identity components; do not infer missing required knowledge."""
    mode = node.get('intent')
    if mode not in ('READ_ONLY', 'MUTATING'):
        raise WorkError('WORK_MODE_INVALID')
    if mode == 'MUTATING':
        if backend.mode != 'SIMULATED' and (constraints or {}).get('mutation_scope') != 'workspace':
            raise WorkError('MUTATION_SCOPE_UNPROVEN')
        owned_root = pathlib.Path(authority.repository_identity(root)['root']).resolve()
        for target in (node.get('task_contract') or {}).get('targets') or []:
            path = pathlib.Path(str(target)).expanduser()
            candidate = (path if path.is_absolute() else pathlib.Path(root) / path).resolve()
            if '://' in str(target) or not candidate.is_relative_to(owned_root):
                raise WorkError('UNSCOPED_EXTERNAL_MUTATION')
    if context is not None and context.get('status') != 'CONTEXT_READY':
        raise WorkError(context.get('status', 'CONTEXT_UNAVAILABLE'))
    root = pathlib.Path(root).resolve()
    repo = authority.repository_identity(root)
    document = knowledge if knowledge is not None else {
        'contract_version': kc.CONTRACT_VERSION, 'repositories': {'project': repo['id']},
        'sources': [], 'requirements': [], 'contents': {}}
    selected = kc.select(document)
    for entry in selected['entries']:
        content = document['contents'][entry['key']]
        delivered = content.get('delivered')
        delivered = kc.normalize_content(content['content'] if delivered is None else delivered)
        if ops.learning.contains_secret_like(delivered):
            raise WorkError('KNOWLEDGE_DELIVERY_UNSAFE')
    if selected['outcome'] != kc.READY:
        raise WorkError(kc.REQUIRED_CONTEXT_UNAVAILABLE)
    if any(not d.get('result_fingerprint') for d in dependencies):
        raise WorkError('DEPENDENCY_IDENTITY_UNAVAILABLE')
    # Representational requirement prose is not an authority-bearing task input.
    requirements = kc.agent_semantic_inputs(document)['requirements']
    permissions = {k: v for k, v in (constraints or {}).items()
                   if k not in ('prefer_provider', 'prefer_model')}
    task = kc.task_identity(
        objective_identity=result_cache.task_semantic_fingerprint(node),
        acceptance_identity=digest(node.get('acceptance_criteria') or []),
        permissions_identity=digest(permissions), requirements_identity=digest(requirements))
    manifest = copy.deepcopy((context or {}).get('manifest'))
    if manifest is not None:
        # These semantics already participate through task/dependency identities. Their
        # retrieval-item hashes embed run-local task IDs and cannot define same work.
        manifest['items'] = [item for item in manifest.get('items') or []
                             if item.get('source_kind') not in
                             ('OBJECTIVE_SUMMARY', 'ACCEPTANCE_CRITERIA', 'DEPENDENCY_FACT')]
    compatibility = {
        'version': VERSION, 'repository': repo['id'], 'workspace': repo['workspace_instance'],
        'workspace_scope': os.path.relpath(root, repo['root']),
        'head': repo['head'], 'mode': node.get('intent', 'READ_ONLY'),
        'freshness_policy': result_cache.freshness_policy(node, constraints=constraints),
        'context': result_cache.context_identity(manifest),
        'dependencies': [d['result_fingerprint'] for d in dependencies],
        'tier': node['routing']['tier'], 'effort': node['routing']['effort'],
        'backend': backend.name, 'adapter_mode': backend.mode,
        'provider': resolution.get('provider'), 'model': resolution.get('model'),
        'native_variant': resolution.get('native_variant'),
        'requested_model': (constraints or {}).get('prefer_model'),
        'requested_effort': node['routing']['effort'],
        'resolved_model': resolution.get('model'),
        'resolved_effort': resolution.get('native_variant'),
        'acceptance_contract': result_cache.ACCEPTANCE_CONTRACT_VERSION,
    }
    if resolution.get('requirement'):
        req = resolution['requirement']
        compatibility.update(execution_requirement=req,
            requested_model=req['requested_model'], requested_effort=req['requested_effort'],
            allowed_bindings=[{k:c.get(k) for k in ('backend','provider','model','native_variant')}
                              for c in resolution.get('candidates', [])])
    equivalent = kc.active_work_equivalence_input(task=task,
        knowledge_identity=selected['delivered_identity'], execution_compatibility=compatibility)
    return {**equivalent, 'version': VERSION, 'workspace': repo['workspace_instance'],
            'mode': node.get('intent', 'READ_ONLY')}


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class WorkStore(ops.OrchestrationStore):
    @contextlib.contextmanager
    def atomic(self):
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def claim(self, work):
        row = self.connection.execute('SELECT * FROM active_work WHERE work_key=?',
                                      (work,)).fetchone()
        return dict(row) if row else None

    def _owned(self, claim):
        row = self.claim(claim['work_key'])
        if (not row or row['generation'] != claim['generation'] or row['token'] != claim['token']
                or row['state'] not in ('CLAIMED', 'RUNNING')):
            raise WorkError('STALE_GENERATION')
        return row

    def accepted(self, identity, *, postcondition=None, generation=None):
        row = self.connection.execute(
            'SELECT * FROM accepted_work WHERE work_key=? AND version=? ORDER BY rowid DESC LIMIT 1',
            (identity['equivalence_identity'], VERSION)).fetchone()
        if not row or json.loads(row['identity']) != identity:
            return None
        if generation is not None and row['generation'] != generation:
            return None
        result = json.loads(row['result'])
        if result.get('status') != 'SUCCESS' or result.get('acceptance') != 'PASSED':
            return None
        compatibility = identity['execution_compatibility']
        if identity['mode'] == 'READ_ONLY' and compatibility.get('freshness_policy') not in result_cache.REUSABLE_FRESHNESS:
            return None
        req = compatibility.get('execution_requirement') or {}
        resolved = {'backend': result.get('resolved_backend'), 'provider': result.get('resolved_provider'),
                    'model': result.get('resolved_model'), 'native_variant': result.get('resolved_effort')}
        allowed_fallback = (req.get('fallback_policy', {}).get('mode') == 'allowed'
                            and resolved in compatibility.get('allowed_bindings', []))
        for field in ('model', 'effort'):
            actual = result.get('actual_' + field)
            expected = result.get('resolved_' + field) if allowed_fallback else compatibility.get('resolved_' + field)
            if result.get('resolved_' + field) is not None and result['resolved_' + field] != expected:
                return None
            if (compatibility.get('execution_requirement') or {}).get('require_actual_' + field) and actual is None:
                return None
            if actual is not None and actual != expected:
                return None
        if identity['mode'] == 'MUTATING':
            if not row['postcondition_verified'] or postcondition is None or postcondition(result) is not True:
                return None
        return {**result, 'reused_from_run_id': row['owner_run'],
                'reused_from_task_id': row['owner_task'], 'cache_entry_id': row['result_id']}

    def acquire(self, identity, run, task, *, lease=30, now=None, reuse=False, result_generation=None, after_result=None):
        now = time.time() if now is None else now
        key = identity['equivalence_identity']
        with self.atomic():
            # Recovery and contention share the write transaction.
            self._recover(now)
            row = self.claim(key)
            published = self.connection.execute(
                'SELECT rowid FROM accepted_work WHERE work_key=? ORDER BY rowid DESC LIMIT 1', (key,)).fetchone()
            new_result = after_result is not None and published is not None and published[0] > after_result
            if (reuse or new_result) and identity['mode'] == 'READ_ONLY' and self.accepted(identity, generation=result_generation) is not None:
                return 'RESULT', row
            conflicts = self.connection.execute(
                "SELECT * FROM active_work WHERE workspace=? AND state IN ('CLAIMED','RUNNING','OUTCOME_UNCERTAIN')"
                " AND (mode='MUTATING' OR ?='MUTATING')",
                (identity['workspace'], identity['mode'])).fetchall()
            conflict = next(iter(conflicts), None)
            if conflict is not None:
                return 'BUSY', dict(conflict)
            if row and row['state'] in BUSY:
                return 'WAIT', row
            generation = (row['generation'] + 1) if row else 1
            token = uuid.uuid4().hex
            self.connection.execute(
                'INSERT OR REPLACE INTO active_work VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (key, VERSION, identity['workspace'], identity['mode'], run, task, os.getpid(),
                 generation, token, 'CLAIMED', 0, now + lease, None))
            self._event(run, 'CLAIM_ACQUIRED', task_id=task,
                        payload={'generation': generation, 'work_key': key})
            return 'OWNER', self.claim(key)

    def launch(self, claim):
        with self.atomic():
            self._owned(claim)
            self.connection.execute("UPDATE active_work SET state='RUNNING', launch_intent=1 WHERE work_key=?",
                                    (claim['work_key'],))

    def heartbeat(self, claim, *, lease=30):
        with self.atomic():
            self._owned(claim)
            self.connection.execute('UPDATE active_work SET lease_until=? WHERE work_key=?',
                                    (time.time() + lease, claim['work_key']))

    def persist_result(self, claim, identity, result, *, postcondition_verified=False):
        if identity['equivalence_identity'] != claim['work_key'] or identity.get('version') != VERSION:
            raise WorkError('RESULT_IDENTITY_MISMATCH')
        if result.get('status') != 'SUCCESS' or result.get('acceptance') != 'PASSED':
            raise WorkError('RESULT_NOT_ACCEPTED')
        if identity['mode'] == 'MUTATING' and postcondition_verified is not True:
            raise WorkError('POSTCONDITION_UNVERIFIED')
        result_id = uuid.uuid4().hex
        with self.atomic():
            self._owned(claim)
            self.connection.execute('INSERT INTO accepted_work VALUES (?,?,?,?,?,?,?,?,?,?)',
                (result_id, claim['work_key'], VERSION, claim['generation'], claim['token'],
                 json.dumps(result), json.dumps(identity), claim['owner_run'], claim['owner_task'],
                 int(postcondition_verified)))
        return result_id

    def complete(self, claim, result_id):
        with self.atomic():
            current = self.claim(claim['work_key'])
            if (current and current['generation'] == claim['generation'] and current['token'] == claim['token']
                    and current['state'] == 'COMPLETED' and current['result_id'] == result_id):
                return
            self._owned(claim)
            row = self.connection.execute('SELECT * FROM accepted_work WHERE result_id=?',
                                          (result_id,)).fetchone()
            if (not row or row['work_key'] != claim['work_key'] or row['generation'] != claim['generation']
                    or row['token'] != claim['token']):
                raise WorkError('DURABLE_RESULT_REQUIRED')
            self.connection.execute("UPDATE active_work SET state='COMPLETED', result_id=? WHERE work_key=?",
                                    (result_id, claim['work_key']))

    def fail(self, claim, *, uncertain=False):
        with self.atomic():
            row = self._owned(claim)
            persisted = self.connection.execute(
                'SELECT result_id FROM accepted_work WHERE work_key=? AND generation=? AND token=?',
                (row['work_key'], row['generation'], row['token'])).fetchone()
            if persisted:
                return  # recovery must finish publication, never discard accepted evidence
            uncertain = uncertain or (row['mode'] == 'MUTATING' and row['launch_intent'])
            state = 'OUTCOME_UNCERTAIN' if uncertain else 'FAILED_SAFE_TO_RETRY'
            self.connection.execute('UPDATE active_work SET state=?, mode=? WHERE work_key=?',
                                    (state, 'MUTATING' if uncertain else row['mode'], claim['work_key']))
            if uncertain:
                self._event(claim['owner_run'], 'CLAIM_UNCERTAIN', task_id=claim['owner_task'])

    def _recover(self, now):
        interrupted = self.connection.execute(
            "SELECT e.* FROM run_events e WHERE e.event_type='WAIT_STARTED' AND NOT EXISTS ("
            "SELECT 1 FROM run_events later WHERE later.run_id=e.run_id AND later.task_id=e.task_id"
            " AND later.event_id>e.event_id AND later.event_type IN ('WAIT_COMPLETED','RESULT_ADOPTED','WAIT_TIMEOUT'))").fetchall()
        for event in interrupted:
            payload = json.loads(event['payload'] or '{}')
            pid = payload.get('pid')
            if isinstance(pid, int) and pid > 0 and not alive(pid):
                self.connection.execute("UPDATE runs SET status='CANCELLED', completed_at=?, failure_category='WAITER_LOST' WHERE run_id=? AND status='RUNNING'",
                                        (ops._now(), event['run_id']))
                self.connection.execute("UPDATE tasks SET status='BLOCKED', failure_category='WAITER_LOST' WHERE run_id=? AND task_id=? AND status IN ('PENDING','RUNNING')",
                                        (event['run_id'], event['task_id']))
                self._event(event['run_id'], 'WAIT_TIMEOUT', task_id=event['task_id'],
                            payload={'reason': 'WAITER_LOST'})
        for raw in self.connection.execute(
                "SELECT * FROM active_work WHERE state IN ('CLAIMED','RUNNING')").fetchall():
            row = dict(raw)
            result = self.connection.execute(
                'SELECT result_id FROM accepted_work WHERE work_key=? AND generation=? AND token=?',
                (row['work_key'], row['generation'], row['token'])).fetchone()
            if result:
                self.connection.execute("UPDATE active_work SET state='COMPLETED', result_id=? WHERE work_key=?",
                                        (result[0], row['work_key']))
                continue
            if row['lease_until'] >= now or alive(row['owner_pid']):
                continue
            state = ('OUTCOME_UNCERTAIN' if row['mode'] == 'MUTATING' and row['launch_intent']
                     else 'FAILED_SAFE_TO_RETRY')
            self.connection.execute('UPDATE active_work SET state=?, generation=generation+1 WHERE work_key=?',
                                    (state, row['work_key']))
            self.connection.execute(
                "UPDATE attempts SET status='UNKNOWN_OUTCOME', failure_category='OWNER_LOST', completed_at=?"
                " WHERE run_id=? AND task_id=? AND status='LAUNCHED'",
                (ops._now(), row['owner_run'], row['owner_task']))
            self._event(row['owner_run'], 'CLAIM_RECOVERED', task_id=row['owner_task'], payload={'state': state})

    def recover(self, *, now=None):
        with self.atomic():
            self._recover(time.time() if now is None else now)

    def reconcile(self, claim, identity, result, *, owner_stopped, postcondition):
        """Explicit reconciliation requires proof of stopped owner and current postcondition.

        Caller-owned verifier is trusted execution policy, never worker-produced prose.
        """
        if owner_stopped() is not True or postcondition(result) is not True:
            raise WorkError('RECONCILIATION_UNPROVEN')
        if result.get('status') != 'SUCCESS' or result.get('acceptance') != 'PASSED':
            raise WorkError('RESULT_NOT_ACCEPTED')
        with self.atomic():
            row = self.claim(claim['work_key'])
            if (not row or row['state'] != 'OUTCOME_UNCERTAIN'
                    or row['generation'] != claim['generation'] or row['token'] != claim['token']):
                raise WorkError('STALE_GENERATION')
            generation, token, ref = row['generation'] + 1, uuid.uuid4().hex, uuid.uuid4().hex
            self.connection.execute('INSERT INTO accepted_work VALUES (?,?,?,?,?,?,?,?,?,?)',
                (ref, row['work_key'], VERSION, generation, token, json.dumps(result),
                 json.dumps(identity), row['owner_run'], row['owner_task'], 1))
            self.connection.execute(
                "UPDATE active_work SET state='COMPLETED', generation=?, token=?, result_id=? WHERE work_key=?",
                (generation, token, ref, row['work_key']))
            return self.claim(row['work_key'])

    def settle_attempt(self, attempt_id, row):
        with self.connection:
            self.connection.execute('UPDATE attempts SET status=?, failure_category=?, completion_signal=?, completed_at=?, requested_model=?, requested_effort=?, resolved_model=?, resolved_effort=?, actual_model=?, actual_effort=? WHERE attempt_id=?',
                (row['status'], row.get('failure_category'), row.get('completion_signal'), ops._now(),
                 row.get('requested_model'), row.get('requested_effort', row.get('required_effort')), row.get('model'),
                 row.get('native_effort'), row.get('actual_model'), row.get('actual_effort'), attempt_id))
            owner = self.connection.execute('SELECT run_id, task_id FROM attempts WHERE attempt_id=?',
                                            (attempt_id,)).fetchone()
            self._event(owner['run_id'], 'ATTEMPT_SUCCEEDED' if row['status'] == 'SUCCESS' else 'ATTEMPT_FAILED',
                        task_id=owner['task_id'], attempt_id=attempt_id, payload={'status': row['status']})


class Lease:
    """Each heartbeat owns its SQLite connection; never share one across threads."""
    def __init__(self, path, claim):
        self.path, self.claim = path, claim
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        store = WorkStore(self.path)
        try:
            while not self.stop.wait(5):
                try:
                    store.heartbeat(self.claim)
                except Exception:
                    return  # final publication still checks the generation
        finally:
            store.close()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join()


def coordinate(store, refresh, execute, *, run, task, reuse, force_refresh=False,
               postcondition=None, wait_seconds=30):
    """Bounded contention; refresh includes policy, knowledge, context and dependencies."""
    if not isinstance(wait_seconds, (int, float)) or not math.isfinite(wait_seconds) or wait_seconds < 0:
        raise WorkError('WAIT_BOUND_INVALID')
    deadline = time.monotonic() + wait_seconds
    waited = False
    joined_generation = None
    result_marker = store.connection.execute('SELECT COALESCE(MAX(rowid),0) FROM accepted_work').fetchone()[0]
    while True:
        current, context = refresh()
        key = current['equivalence_identity']
        prior = store.claim(key)
        if prior is not None and prior['state'] in ('CLAIMED', 'RUNNING'):
            joined_generation = prior['generation']
        can_reuse = (reuse and not force_refresh) or waited or joined_generation is not None
        accepted = store.accepted(current, postcondition=postcondition,
            generation=joined_generation if force_refresh else None) if can_reuse and current['mode'] != 'MUTATING' else None
        if accepted is not None:
            store.recover()  # closes result-persisted/claim-publication crash window
            again, _ = refresh()
            if again != current:
                store.cache_event(run, 'WAIT_REVALIDATION_FAILED', task_id=task)
                continue
            store.cache_event(run, 'RESULT_ADOPTED', task_id=task)
            return {**accepted, 'task_id': task, 'attempts': 0, 'execution_source': 'CACHE',
                    'master_b_identity': current}, current, context
        state, claim = store.acquire(current, run, task, reuse=can_reuse,
            result_generation=joined_generation if force_refresh else None, after_result=result_marker)
        if state == 'RESULT':
            waited = True
            continue
        if state != 'OWNER':
            if claim['state'] == 'OUTCOME_UNCERTAIN':
                return {'status': 'BLOCKED', 'task_id': task,
                        'failure_category': 'OUTCOME_UNCERTAIN'}, current, context
            if not waited:
                store.cache_event(run, 'ACTIVE_WORK_FOUND', task_id=task)
                store.cache_event(run, 'WAIT_STARTED', task_id=task, payload={'pid': os.getpid()})
                waited = True
            if time.monotonic() >= deadline:
                store.cache_event(run, 'WAIT_TIMEOUT', task_id=task)
                return {'status': 'BLOCKED', 'task_id': task,
                        'failure_category': 'WAIT_TIMEOUT'}, current, context
            time.sleep(.02)
            continue
        try:
            # Known real mutations cannot be blindly replayed under the same work identity.
            known_mutation = current['mode'] == 'MUTATING' and current['execution_compatibility']['adapter_mode'] != 'SIMULATED' and store.connection.execute(
                'SELECT 1 FROM accepted_work WHERE work_key=? AND version=? LIMIT 1',
                (key, VERSION)).fetchone() is not None
            if known_mutation:
                can_reuse = True
            # Close the reuse-lookup/acquisition race, independent of claim liveness.
            accepted = store.accepted(current, postcondition=postcondition,
            generation=joined_generation if force_refresh and not known_mutation else None) if can_reuse else None
            if accepted is not None:
                again, _ = refresh()
                store.fail(claim)  # current postcondition checked under workspace exclusion
                if again != current:
                    continue
                store.cache_event(run, 'RESULT_ADOPTED', task_id=task)
                return {**accepted, 'task_id': task, 'attempts': 0, 'execution_source': 'CACHE',
                        'master_b_identity': current}, current, context
            if known_mutation:
                store.fail(claim)
                return {'status': 'BLOCKED', 'task_id': task,
                        'failure_category': 'MUTATION_RECONCILIATION_REQUIRED'}, current, context
            again, context = refresh()
            if again != current:
                store.fail(claim)
                continue
            with Lease(store.path, claim):
                result = execute(claim, current, context)
            result['master_b_identity'] = current
            if result.get('status') == 'SUCCESS' and result.get('acceptance') == 'PASSED':
                verified = (current['mode'] != 'MUTATING' or
                    current['execution_compatibility']['adapter_mode'] == 'SIMULATED') or (
                    postcondition is not None and postcondition(result) is True)
                if verified:
                    ref = store.persist_result(claim, current, result, postcondition_verified=verified)
                    store.complete(claim, ref)
                else:
                    store.fail(claim)
                    result['coordination_outcome'] = 'OUTCOME_UNCERTAIN'
            else:
                store.fail(claim, uncertain=result.get('failure_category') == 'READ_ONLY_VIOLATION')
            if waited:
                store.cache_event(run, 'WAIT_COMPLETED', task_id=task)
            return result, current, context
        except BaseException:
            try:
                store.fail(claim)
            except WorkError:
                pass  # a replacement generation already owns publication
            raise
