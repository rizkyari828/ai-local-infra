"""Offline concurrency and crash checks for the production Master B boundaries."""
from __future__ import annotations
import contextlib
import copy
import io
import os
import pathlib
import tempfile
import threading
import unittest
from unittest.mock import patch
import sys
sys.path[:0] = [str(pathlib.Path(__file__).resolve().parents[1] / 'scripts/common'),
                str(pathlib.Path(__file__).resolve().parent)]
import active_work as aw
import authority
import execution
import knowledge_contract as kc
from test_runtime_authority_identity import _repo, _plan, Stub
from test_knowledge_contract import document, requirement, source


class Worker(Stub):
    def __init__(self, started=None, release=None, failure=None):
        super().__init__()
        self.lock = threading.Lock()
        self.started, self.release, self.failure = started, release, failure

    def execute(self, request):
        with self.lock:
            self.calls.append(request)
        if self.started:
            self.started.set()
        if self.release and not self.release.wait(10):
            raise RuntimeError('test owner was never released')
        if self.failure:
            return {'status': 'FAILURE', 'failure_category': self.failure}
        return {'status': 'SUCCESS', 'completion_signal': 'agent_reported',
                'acceptance_results': {'ok': True}, 'summary': 'accepted answer'}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name)
        self.root = _repo(self.base / 'project')
        self.state = self.base / 'state'
        self.env = patch.dict(os.environ, {'LAI_MACHINE': 'work-mac', 'LAI_STATE_DIR': str(self.state)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.plan = _plan()

    def run_work(self, worker=None, **kwargs):
        if any(n.get('intent') == 'MUTATING' for n in self.plan['nodes']):
            kwargs['constraints'] = {**(kwargs.get('constraints') or {}), 'mutation_scope': 'workspace'}
        return execution.execute_plan(self.plan, backend=worker or Worker(), repo_root=self.root,
            state_dir=self.state, confirm=True, allow_dirty=True, record_evidence=False,
            infra_retries=0, same_tier_retries=0, **kwargs)

    def identity(self, mode='READ_ONLY', **changes):
        node = copy.deepcopy(self.plan['nodes'][0]); node['intent'] = mode
        backend = Worker(); binding = execution.resolve_plan({**self.plan, 'nodes': [node]}, backend)[0]
        result = aw.identity(node, self.root, execution._build_node_context(node, [], self.root), [],
                             authority.bind_constraints({'mutation_scope':'workspace'} if mode == 'MUTATING' else None), binding, backend, None)
        if changes:
            result = {**result, **changes}
            result['equivalence_identity'] = aw.digest(result)
        return result

    def store(self):
        s = aw.WorkStore(state_dir=self.state)
        self.addCleanup(s.close)
        return s


class ConcurrencyTests(Fixture):
    def concurrent(self, count):
        started, release, waiting = threading.Event(), threading.Event(), threading.Event()
        worker = Worker(started, release)
        reports, errors = {}, []
        guard = threading.Lock()
        waiters = 0
        original = aw.WorkStore.cache_event
        def event(store, run, kind, **kwargs):
            nonlocal waiters
            original(store, run, kind, **kwargs)
            if kind == 'WAIT_STARTED':
                with guard:
                    waiters += 1
                    if waiters == count - 1:
                        waiting.set()
        def run(index):
            try:
                reports[index] = self.run_work(worker, run_id=f'run-{index}')
            except BaseException as exc:
                errors.append(exc)
        with patch.object(aw.WorkStore, 'cache_event', event):
            owner = threading.Thread(target=run, args=(0,)); owner.start()
            self.assertTrue(started.wait(10))
            threads = [threading.Thread(target=run, args=(i,)) for i in range(1, count)]
            for thread in threads: thread.start()
            self.assertTrue(waiting.wait(10))
            release.set()
            for thread in [owner] + threads:
                thread.join(10); self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(1, len(worker.calls))
        self.assertEqual(count - 1, waiters)
        self.assertEqual(count, len(reports))
        for i, r in reports.items():
            self.assertEqual('SUCCESS', r['nodes']['A1']['status'], (i, r.get('error'), r['nodes']['A1']))
            self.assertEqual(1 if i == 0 else 0, len(r['attempts']))
            self.assertEqual(1 if i == 0 else 0, len(self.store().run_attempts(r['run_id'])))
        return reports

    def test_C1_two_requests_one_launch(self):
        self.concurrent(2)

    def test_C2_C3_ten_requests_nine_waiters_one_launch(self):
        self.concurrent(10)

    def test_C4_knowledge_changes_while_waiting(self):
        started, release, waited = threading.Event(), threading.Event(), threading.Event()
        worker = Worker(started, release)
        docs = [document(), document()]
        original = aw.WorkStore.cache_event
        reports = {}
        def event(store, run, kind, **kwargs):
            original(store, run, kind, **kwargs)
            if kind == 'WAIT_STARTED': waited.set()
        def run(i):
            reports[i] = self.run_work(worker, run_id=f'r{i}', knowledge_provider=lambda n: docs[i])
        with patch.object(aw.WorkStore, 'cache_event', event):
            a = threading.Thread(target=run, args=(0,));a.start();self.assertTrue(started.wait(10))
            b = threading.Thread(target=run, args=(1,));b.start();self.assertTrue(waited.wait(10))
            docs[1] = document(contents={'project.architecture.overview': {'content': 'changed'}})
            release.set();a.join(10);b.join(10)
            self.assertFalse(a.is_alive() or b.is_alive())
        self.assertEqual(2, len(worker.calls))
        self.assertNotEqual(reports[0]['nodes']['A1']['master_b_identity'],
                            reports[1]['nodes']['A1']['master_b_identity'])
        self.assertNotEqual('CACHE', reports[1]['nodes']['A1'].get('execution_source'))

    def test_C5_dependency_changes_while_waiting(self):
        started, release, waited = threading.Event(), threading.Event(), threading.Event()
        worker, revision, reports = Worker(started, release), [1], {}
        class Tool:
            def execute(self, task):
                return {'exists': True, 'actual': revision[0]}
            def validate_result(self, task, result):
                return True
        dep = {**copy.deepcopy(self.plan['nodes'][0]), 'task_id': 'D', 'executor': 'TOOL_ONLY',
               'tool': {'operation': 'file_exists', 'args': {'path': 'f.txt'}}}
        self.plan['nodes'][0]['depends_on'] = ['D']
        self.plan['nodes'].insert(0, dep);self.plan['order'] = ['D', 'A1']
        original = aw.WorkStore.cache_event
        def event(store, run, kind, **kwargs):
            original(store, run, kind, **kwargs)
            if kind == 'WAIT_STARTED': waited.set()
        def run(i): reports[i] = self.run_work(worker, run_id=f'dep-{i}', tool_executor=Tool())
        with patch.object(aw.WorkStore, 'cache_event', event):
            a=threading.Thread(target=run,args=(0,));a.start();self.assertTrue(started.wait(10))
            b=threading.Thread(target=run,args=(1,));b.start();self.assertTrue(waited.wait(10))
            revision[0]=2;release.set();a.join(10);b.join(10)
            self.assertFalse(a.is_alive() or b.is_alive())
        self.assertEqual(2,len(worker.calls))
        self.assertNotEqual('CACHE',reports[1]['nodes']['A1'].get('execution_source'))
        self.assertNotEqual(reports[0]['nodes']['A1']['master_b_identity'],
                            reports[1]['nodes']['A1']['master_b_identity'])

    def test_timeout_never_launches_duplicate(self):
        store = self.store(); store.acquire(self.identity(), 'owner', 'A1')
        worker = Worker(); report = self.run_work(worker, wait_seconds=0)
        self.assertEqual([], worker.calls)
        self.assertEqual('WAIT_TIMEOUT', report['nodes']['A1']['failure_category'])
        self.assertEqual('WAIT_TIMEOUT',report['error'])
        import orchestrate_cli
        args=orchestrate_cli.parser().parse_args(['direct','--objective','review','--backend','fake',
                                                '--confirm','--json','--workspace',str(self.root)])
        with patch.object(orchestrate_cli,'_local_health',return_value=(None,{'fake':execution.FakeBackend()})), \
             patch.object(orchestrate_cli.orchestrator,'decompose',return_value=self.plan), \
             patch.object(orchestrate_cli.execution,'execute_plan',return_value=report), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(2,orchestrate_cli.cmd_run(args))


    def test_M1_M2_workspace_exclusion_for_equivalent_and_different_mutations(self):
        identity = self.identity('MUTATING');store = self.store()
        _, claim = store.acquire(identity, 'owner', 'A1');store.launch(claim)
        self.plan['intent'] = self.plan['nodes'][0]['intent'] = 'MUTATING'
        for objective in ['review', 'different mutation']:
            self.plan['nodes'][0]['objective_summary'] = objective
            worker = Worker(); r = self.run_work(worker, wait_seconds=0)
            self.assertEqual([], worker.calls);self.assertEqual('BLOCKED',r['nodes']['A1']['status'])

    def test_completion_between_lookup_and_acquire_does_not_launch_again(self):
        identity=self.identity();barrier=threading.Barrier(2);counter=[0];lock=threading.Lock();reports=[]
        def run(i):
            store=aw.WorkStore(state_dir=self.state);first=[True]
            def refresh():
                if first[0]:first[0]=False;barrier.wait(10)
                return identity,None
            def execute(claim,current,context):
                with lock:counter[0]+=1
                store.launch(claim)
                return {'status':'SUCCESS','acceptance':'PASSED'}
            try:reports.append(aw.coordinate(store,refresh,execute,run=f'lookup-{i}',task='A1',reuse=False))
            finally:store.close()
        threads=[threading.Thread(target=run,args=(i,)) for i in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(10);self.assertFalse(thread.is_alive())
        self.assertEqual(2,len(reports));self.assertEqual(1,counter[0])

    def test_real_mutation_requires_explicit_supported_resource_scope(self):
        self.plan['intent']=self.plan['nodes'][0]['intent']='MUTATING'
        for scope in [None,'external']:
            worker=Worker()
            report=execution.execute_plan(self.plan,backend=worker,repo_root=self.root,state_dir=self.state,
                confirm=True,record_evidence=False,constraints={'mutation_scope':scope})
            self.assertEqual([],worker.calls);self.assertEqual('MUTATION_SCOPE_UNPROVEN',report['error'])

    def test_reader_writer_exclusion(self):
        store=self.store();store.acquire(self.identity(), 'reader','A1')
        state,_=store.acquire(self.identity('MUTATING'),'writer','A1')
        self.assertEqual('BUSY',state)

    def test_policy_and_capability_not_equivalent(self):
        base=self.identity();node=copy.deepcopy(self.plan['nodes'][0]);node['routing']['effort']='max'
        binding=execution.resolve_plan({**self.plan,'nodes':[node]},Worker())[0]
        other=aw.identity(node,self.root,execution._build_node_context(node,[],self.root),[],
            authority.bind_constraints({'local_only':True}),binding,Worker(),None)
        self.assertNotEqual(base['equivalence_identity'],other['equivalence_identity'])


class RecoveryTests(Fixture):
    def expired(self, store, claim):
        with store.connection:
            store.connection.execute('UPDATE active_work SET owner_pid=?, lease_until=0 WHERE work_key=?',
                                     (2147483647, claim['work_key']))
        store.recover(now=100)

    def test_C6_M3_prelaunch_failure_allows_new_owner(self):
        store=self.store();identity=self.identity('MUTATING');_,claim=store.acquire(identity,'a','A1')
        store.fail(claim);state,new=store.acquire(identity,'b','A1')
        self.assertEqual('OWNER',state);self.assertGreater(new['generation'],claim['generation'])

    def test_C7_C8_stale_readonly_reclaimed_late_owner_fenced(self):
        store=self.store();identity=self.identity();_,old=store.acquire(identity,'a','A1');store.launch(old)
        self.expired(store,old);state,new=store.acquire(identity,'b','A1')
        self.assertEqual('OWNER',state)
        with self.assertRaisesRegex(aw.WorkError,'STALE_GENERATION'):
            store.persist_result(old,identity,{'status':'SUCCESS','acceptance':'PASSED'})
        self.assertGreater(new['generation'],old['generation'])

    def test_claimed_unlaunched_owner_vanished(self):
        store=self.store();identity=self.identity();_,old=store.acquire(identity,'a','A1')
        self.expired(store,old);self.assertEqual('OWNER',store.acquire(identity,'b','A1')[0])

    def test_M4_M5_launched_mutation_expiry_never_replays(self):
        store=self.store();identity=self.identity('MUTATING');_,old=store.acquire(identity,'a','A1');store.launch(old)
        self.expired(store,old)
        self.assertEqual('OUTCOME_UNCERTAIN',store.claim(old['work_key'])['state'])
        self.assertEqual('BUSY',store.acquire(identity,'b','A1')[0])

    def test_M6_quota_failure_is_uncertain(self):
        self.plan['intent']=self.plan['nodes'][0]['intent']='MUTATING'
        worker=Worker(failure='RATE_LIMIT');first=self.run_work(worker)
        second=self.run_work(worker,wait_seconds=0)
        self.assertEqual(1,len(worker.calls))
        self.assertEqual('OUTCOME_UNCERTAIN',second['nodes']['A1']['failure_category'])
        self.assertEqual(1,len(first['attempts']))

    def test_R7_disappeared_waiter_does_not_own_attempt_or_change_owner(self):
        store=self.store();identity=self.identity();_,owner=store.acquire(identity,'owner','A1')
        store.create_run('waiter',project_id='project',machine_id='work-mac',status='RUNNING')
        store.create_task('waiter','A1',title='review',executor_type='AI')
        store.cache_event('waiter','WAIT_STARTED',task_id='A1',payload={'pid':2147483647})
        store.recover()
        self.assertEqual('CANCELLED',store.get_run('waiter')['status'])
        self.assertEqual([],store.run_attempts('waiter'))
        self.assertEqual(owner,store.claim(identity['equivalence_identity']))

    def test_live_owner_cannot_be_reclaimed_only_on_lease_expiry(self):
        store=self.store();identity=self.identity();_,claim=store.acquire(identity,'a','A1')
        store.recover(now=10**12)
        self.assertEqual('WAIT',store.acquire(identity,'b','A1')[0])

    def test_M7_M8_reconciliation_fences_old_mutation_without_replay(self):
        store=self.store();identity=self.identity('MUTATING');_,old=store.acquire(identity,'a','A1');store.launch(old);store.fail(old)
        result={'status':'SUCCESS','acceptance':'PASSED','summary':'verified'}
        new=store.reconcile(old,identity,result,owner_stopped=lambda:True,postcondition=lambda r:True)
        self.assertIsNotNone(store.accepted(identity,postcondition=lambda r:True))
        with self.assertRaisesRegex(aw.WorkError,'STALE_GENERATION'):store.complete(old,new['result_id'])
        self.assertGreater(new['generation'],old['generation'])

    def test_reconciliation_requires_both_proofs(self):
        store=self.store();i=self.identity('MUTATING');_,c=store.acquire(i,'a','A1');store.launch(c);store.fail(c)
        for stopped,post in [(False,True),(True,False),('false',True),(True,'false'),(1,True),(True,1)]:
            with self.assertRaises(aw.WorkError):store.reconcile(c,i,{'status':'SUCCESS','acceptance':'PASSED'},owner_stopped=lambda:stopped,postcondition=lambda r:post)
            self.assertEqual('OUTCOME_UNCERTAIN',store.claim(c['work_key'])['state'])

    def test_P2_completion_requires_durable_result(self):
        store=self.store();_,c=store.acquire(self.identity(),'a','A1')
        with self.assertRaisesRegex(aw.WorkError,'DURABLE_RESULT_REQUIRED'):store.complete(c,'missing')
        self.assertEqual('CLAIMED',store.claim(c['work_key'])['state'])

    def test_P1_result_persisted_completion_crash_reuses_without_worker(self):
        worker=Worker()
        with patch.object(aw.WorkStore,'complete',side_effect=OSError('publication crash')):
            first=self.run_work(worker,cache=True)
        self.assertEqual(1,len(worker.calls))
        second=self.run_work(worker,cache=True)
        self.assertEqual(1,len(worker.calls));self.assertEqual('CACHE',second['nodes']['A1']['execution_source'])
        row=self.store().claim(second['nodes']['A1']['master_b_identity']['equivalence_identity'])
        self.assertEqual('COMPLETED',row['state'])

    def test_mutation_result_requires_current_postcondition(self):
        store=self.store();i=self.identity('MUTATING');_,c=store.acquire(i,'a','A1')
        with self.assertRaisesRegex(aw.WorkError,'POSTCONDITION_UNVERIFIED'):
            store.persist_result(c,i,{'status':'SUCCESS','acceptance':'PASSED'})
        with self.assertRaisesRegex(aw.WorkError,'POSTCONDITION_UNVERIFIED'):
            store.persist_result(c,i,{'status':'SUCCESS','acceptance':'PASSED'},postcondition_verified=1)


class ReuseTests(Fixture):
    def test_L1_legacy_result_cannot_match_current_version(self):
        worker=Worker();self.run_work(worker,cache=True)
        store=self.store()
        with store.connection:store.connection.execute("UPDATE accepted_work SET version='legacy'")
        self.run_work(worker,cache=True);self.assertEqual(2,len(worker.calls))

    def test_L2_L3_L4_content_scope_and_unselected_changes(self):
        worker=Worker();docs=[document()]
        def run():return self.run_work(worker,cache=True,knowledge_provider=lambda n:docs[0])
        run();run();self.assertEqual(1,len(worker.calls))
        docs[0]['contents']['unselected']={'content':'unrelated'};run();self.assertEqual(1,len(worker.calls))
        docs[0]['contents']['project.architecture.overview']['content']='changed';run();self.assertEqual(2,len(worker.calls))
        docs[0]['requirements'][0]['scope']='api:refunds';run();self.assertEqual(3,len(worker.calls))

    def test_L5_required_revoked_refuses_before_reuse(self):
        worker=Worker();self.run_work(worker,cache=True,knowledge_provider=lambda n:document())
        r=self.run_work(worker,cache=True,knowledge_provider=lambda n:document(sources=[source(lifecycle='REVOKED')]))
        self.assertEqual(1,len(worker.calls));self.assertEqual(kc.REQUIRED_CONTEXT_UNAVAILABLE,r['error'])

    def test_L6_policy_change_blocks_reuse(self):
        worker=Worker();self.run_work(worker,cache=True)
        r=self.run_work(worker,cache=True,constraints={'local_only':True})
        self.assertEqual(1,len(worker.calls));self.assertTrue(r.get('error'))

    def test_resume_revalidates_current_knowledge(self):
        worker=Worker();self.run_work(worker,run_id='resume',knowledge_provider=lambda n:document())
        r=self.run_work(worker,run_id='resume',resume=True,knowledge_provider=lambda n:document(requirements=[requirement(scope='new:scope')]))
        self.assertEqual(1,len(worker.calls));self.assertIn('REFUSE_RESUME',r['error'])

    def test_task_ids_and_titles_are_not_work_identity(self):
        node=copy.deepcopy(self.plan['nodes'][0]);base=self.identity()
        node['task_id']='other';node['title']='UI title'
        other=aw.identity(node,self.root,execution._build_node_context(node,[],self.root),[],
            authority.bind_constraints(None),execution.resolve_plan({**self.plan,'nodes':[node]},Worker())[0],Worker(),None)
        self.assertEqual(base,other)

    def test_same_basename_repo_and_linked_workspace_do_not_join(self):
        base=self.identity();other=_repo(self.base/'other'/'project');node=self.plan['nodes'][0]
        i=aw.identity(node,other,execution._build_node_context(node,[],other),[],
            authority.bind_constraints(None),execution.resolve_plan(self.plan,Worker())[0],Worker(),None)
        self.assertNotEqual(base['equivalence_identity'],i['equivalence_identity'])

    def test_C5_dependency_result_identity_changes_key(self):
        node=self.plan['nodes'][0];ctx=execution._build_node_context(node,[],self.root);b=execution.resolve_plan(self.plan,Worker())[0]
        identities=[aw.identity(node,self.root,ctx,[{'result_fingerprint':x}],authority.bind_constraints(None),b,Worker(),None) for x in ['old','new']]
        self.assertNotEqual(identities[0]['equivalence_identity'],identities[1]['equivalence_identity'])

    def test_actual_model_effort_are_not_inferred(self):
        r=self.run_work()
        row=self.store().run_attempts(r['run_id'])[0]
        self.assertEqual('high',row['requested_effort'])
        self.assertEqual('deepseek-flash',row['resolved_model'])
        self.assertIsNone(row['actual_model']);self.assertIsNone(row['actual_effort'])

    def test_unsafe_knowledge_refuses_without_capture_or_launch(self):
        worker=Worker()
        d=document(contents={'project.architecture.overview': {'content': 'api_key=abcdefghijklmnop'}})
        r=self.run_work(worker,knowledge_provider=lambda n:d)
        self.assertEqual([],worker.calls);self.assertEqual('KNOWLEDGE_DELIVERY_UNSAFE',r['error'])

    def test_dependency_binding_swap_changes_work_identity(self):
        node=self.plan['nodes'][0];context=execution._build_node_context(node,[],self.root)
        binding=execution.resolve_plan(self.plan,Worker())[0]
        sets=[[{'task_id':'left','result_fingerprint':'A'},{'task_id':'right','result_fingerprint':'B'}],
              [{'task_id':'left','result_fingerprint':'B'},{'task_id':'right','result_fingerprint':'A'}]]
        identities=[aw.identity(node,self.root,context,deps,authority.bind_constraints(None),binding,Worker(),None) for deps in sets]
        self.assertNotEqual(identities[0]['equivalence_identity'],identities[1]['equivalence_identity'])

    def test_volatile_observation_never_reuses_completed_result(self):
        self.plan['nodes'][0]['freshness_policy']='RUNTIME_VOLATILE'
        worker=Worker();self.run_work(worker,cache=True);second=self.run_work(worker,cache=True)
        self.assertEqual(2,len(worker.calls));self.assertEqual(0,second['cache']['hits'])
        self.assertEqual(2,self.store().connection.execute('SELECT COUNT(*) FROM accepted_work').fetchone()[0])

    def test_request_task_snapshot_cannot_change_under_claim_identity(self):
        worker=Worker();original=aw.identity
        def change_external_plan(*args,**kwargs):
            result=original(*args,**kwargs)
            self.plan['nodes'][0]['objective_summary']='changed goal'
            return result
        with patch.object(aw,'identity',side_effect=change_external_plan):
            self.run_work(worker,cache=True)
        self.assertIn('Objective: review',worker.calls[0]['prompt'])
        self.assertNotIn('changed goal',worker.calls[0]['prompt'])
        self.run_work(worker,cache=True)
        self.assertEqual(2,len(worker.calls))
        self.assertIn('Objective: changed goal',worker.calls[1]['prompt'])

    def test_validated_knowledge_snapshot_matches_delivered_worker_content(self):
        doc=document();worker=Worker();original=aw.identity;calls=[0]
        def change_after_validation(*args,**kwargs):
            result=original(*args,**kwargs);calls[0]+=1
            if calls[0]==2:
                doc['contents']['project.architecture.overview']['content']='changed after snapshot'
            return result
        with patch.object(aw,'identity',side_effect=change_after_validation):
            self.run_work(worker,cache=True,knowledge_provider=lambda n:doc)
        self.assertIn('architecture v1',worker.calls[0]['prompt'])
        self.assertNotIn('changed after snapshot',worker.calls[0]['prompt'])
        self.run_work(worker,cache=True,knowledge_provider=lambda n:doc)
        self.assertEqual(2,len(worker.calls))
        self.assertIn('changed after snapshot',worker.calls[1]['prompt'])

    def test_knowledge_provider_unavailable_cannot_silently_drop_requirements(self):
        worker=Worker();r=self.run_work(worker,knowledge_provider=lambda n:None)
        self.assertEqual([],worker.calls);self.assertEqual(kc.REQUIRED_CONTEXT_UNAVAILABLE,r['error'])

    def test_actual_attribution_cannot_capture_secret_like_text(self):
        secret='api_key=abcdefghijklmnopqrst'
        class UnsafeMetadata(Worker):
            def execute(self,request):
                result=super().execute(request);result['actual_model']=secret;return result
        report=self.run_work(UnsafeMetadata())
        rows=self.store().run_attempts(report['run_id'])
        self.assertIsNone(rows[0]['actual_model'])
        data=self.store().connection.execute('SELECT result FROM accepted_work').fetchone()[0]
        self.assertNotIn(secret,data)

    def test_cancellation_stops_independent_later_nodes(self):
        second=copy.deepcopy(self.plan['nodes'][0]);second['task_id']='B';second['objective_summary']='independent work'
        self.plan['nodes'].append(second);self.plan['order'].append('B')
        class Interrupt(Worker):
            def execute(self,request):
                self.calls.append(request);raise KeyboardInterrupt
        worker=Interrupt();r=self.run_work(worker)
        self.assertEqual(1,len(worker.calls));self.assertEqual('CANCELLED',r['nodes']['A1']['status'])
        self.assertEqual('NOT_RUN',r['nodes']['B']['status'])
        self.assertEqual('CANCELLED',r['attempts'][0]['status'])

    def test_wait_must_remain_bounded(self):
        for bound in [float('nan'),float('inf'),-1]:
            worker=Worker();r=self.run_work(worker,wait_seconds=bound)
            self.assertEqual([],worker.calls);self.assertEqual('WAIT_BOUND_INVALID',r['error'])

    def test_verified_mutation_reuse_rechecks_current_postcondition(self):
        self.plan['intent']=self.plan['nodes'][0]['intent']='MUTATING'
        worker=Worker();verified=[True]
        check=lambda n,r,p:verified[0]
        self.run_work(worker,cache=True,postcondition_verifier=check)
        reused=self.run_work(worker,cache=True,postcondition_verifier=check)
        self.assertEqual(1,len(worker.calls));self.assertEqual('CACHE',reused['nodes']['A1']['execution_source'])
        verified[0]=False
        blocked=self.run_work(worker,cache=True,postcondition_verifier=check)
        self.assertEqual(1,len(worker.calls))
        self.assertEqual('MUTATION_RECONCILIATION_REQUIRED',blocked['error'])

    def test_known_mutation_never_replays_without_current_verifier(self):
        self.plan['intent']=self.plan['nodes'][0]['intent']='MUTATING'
        worker=Worker();self.run_work(worker,postcondition_verifier=lambda n,r,p:True)
        missing=self.run_work(worker)
        self.assertEqual(1,len(worker.calls));self.assertEqual('MUTATION_RECONCILIATION_REQUIRED',missing['error'])
        verified=self.run_work(worker,force_refresh=True,postcondition_verifier=lambda n,r,p:True)
        self.assertEqual(1,len(worker.calls));self.assertEqual('CACHE',verified['nodes']['A1']['execution_source'])

    def test_mutation_reuse_does_not_bypass_other_workspace_writer(self):
        self.plan['intent']=self.plan['nodes'][0]['intent']='MUTATING'
        worker=Worker();self.run_work(worker,cache=True,postcondition_verifier=lambda n,r,p:True)
        store=self.store();other=self.identity('MUTATING',task_identity='different')
        store.acquire(other,'other','A1')
        blocked=self.run_work(worker,cache=True,postcondition_verifier=lambda n,r,p:True,wait_seconds=0)
        self.assertEqual(1,len(worker.calls));self.assertEqual('BLOCKED',blocked['nodes']['A1']['status'])

    def test_force_refresh_waiter_adopts_new_generation_not_old_cached_result(self):
        self.run_work(Worker(),cache=True)
        started,release,waited=threading.Event(),threading.Event(),threading.Event()
        worker=Worker(started,release);reports={};original=aw.WorkStore.cache_event
        def event(store,run,kind,**kwargs):
            original(store,run,kind,**kwargs)
            if kind=='WAIT_STARTED':waited.set()
        def run(i):reports[i]=self.run_work(worker,run_id=f'force-{i}',cache=True,force_refresh=True)
        with patch.object(aw.WorkStore,'cache_event',event):
            a=threading.Thread(target=run,args=(0,));a.start();self.assertTrue(started.wait(10))
            b=threading.Thread(target=run,args=(1,));b.start();self.assertTrue(waited.wait(10))
            release.set();a.join(10);b.join(10);self.assertFalse(a.is_alive() or b.is_alive())
        self.assertEqual(1,len(worker.calls))
        self.assertEqual('force-0',reports[1]['nodes']['A1']['reused_from_run_id'])

    def test_reported_effort_mismatch_is_not_reused(self):
        class DifferentEffort(Worker):
            def execute(self, request):
                result=super().execute(request);result['actual_effort']='low';return result
        worker=DifferentEffort();self.run_work(worker,cache=True);self.run_work(worker,cache=True)
        self.assertEqual(2,len(worker.calls))
        row=self.store().connection.execute('SELECT actual_effort FROM attempts LIMIT 1').fetchone()
        self.assertEqual('low',row[0])

    def test_external_mutation_is_refused_without_resource_scope(self):
        self.plan['intent']=self.plan['nodes'][0]['intent']='MUTATING'
        self.plan['nodes'][0]['task_contract']={'targets':['../external-resource']}
        worker=Worker();r=self.run_work(worker)
        self.assertEqual([],worker.calls);self.assertEqual('UNSCOPED_EXTERNAL_MUTATION',r['error'])

    def test_unverified_mutation_is_not_adopted_on_resume(self):
        self.plan['intent']=self.plan['nodes'][0]['intent']='MUTATING'
        worker=Worker();first=self.run_work(worker,run_id='mutation')
        self.assertEqual('OUTCOME_UNCERTAIN',first['nodes']['A1']['coordination_outcome'])
        resumed=self.run_work(worker,run_id='mutation',resume=True)
        self.assertEqual(1,len(worker.calls));self.assertIn('UNCERTAIN_MUTATION',resumed['error'])

    def test_dogfood_quota_interruption_does_not_fabricate_completion(self):
        dep={**copy.deepcopy(self.plan['nodes'][0]),'task_id':'D','executor':'TOOL_ONLY',
             'tool':{'operation':'file_exists','args':{'path':'f.txt'}}}
        self.plan['nodes'][0]['depends_on']=['D'];self.plan['nodes'].insert(0,dep);self.plan['order']=['D','A1']
        worker=Worker(failure='RATE_LIMIT');first=self.run_work(worker,run_id='quota')
        durable,validation=execution._load_resume_state(self.state/'orchestration'/'quota')
        self.assertEqual('SUCCESS',durable['nodes']['D']['status'])
        self.assertIsNotNone(durable['nodes']['D']['result_fingerprint'])
        self.assertEqual('FAILURE',first['nodes']['A1']['status']);self.assertEqual(1,len(first['attempts']))
        second=self.run_work(Worker(),run_id='quota',resume=True)
        self.assertEqual('SUCCESS',second['nodes']['A1']['status'])
        rows=self.store().run_attempts('quota');self.assertEqual(2,len(rows))
        self.assertEqual({'FAILED','SUCCESS'},{r['status'] for r in rows})

if __name__=='__main__':unittest.main()
