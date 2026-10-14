"""Exact routing through production execution boundaries; fake provider calls only."""
import copy
import json
import os
import pathlib
import sys
import threading
import unittest
import contextlib
import io
from unittest.mock import patch

sys.path[:0] = [str(pathlib.Path(__file__).resolve().parents[1] / 'scripts/common'),
                str(pathlib.Path(__file__).resolve().parent)]
import active_work
import discovery
import execution
import failover
import lifecycle
import orchestrate_cli
from test_master_b import Fixture
from test_runtime_authority_identity import _plan


def requirement(model='Astra', effort='medium', provider='codex', **extra):
    return {'role': 'reviewer', 'requested_provider': provider, 'requested_model': model,
            'requested_effort': effort, 'required_capabilities': ['review'],
            'fallback_policy': {'mode': 'forbidden'}, **extra}


class Adapter(execution.Backend):
    mode = 'SUPPORTED'  # exercises hard machine policy; never launches a real process

    def __init__(self, name, provider, model, efforts, actual=None):
        self.name, self.calls, self.present = name, [], True
        self.catalog = [{'provider': provider, 'model': model, 'efforts': efforts,
                         'capabilities': ['review', 'implementation', 'read_only']}]
        self.actual, self.failure, self.started, self.release = actual or {}, None, None, None

    def available(self):
        return self.present

    def models(self):
        return copy.deepcopy(self.catalog)

    def execute(self, request):
        self.calls.append(request)
        if self.started:
            self.started.set()
        if self.release and not self.release.wait(10):
            raise RuntimeError('owner was never released')
        if self.failure:
            return {'status': 'FAILURE', 'failure_category': self.failure}
        return {'status': 'SUCCESS', 'completion_signal': 'agent_reported',
                'acceptance_results': {'ok': True}, 'summary': 'PRIVATE SCRATCHPAD MUST NOT CROSS',
                **self.actual}


class RoutingTests(Fixture):
    def setUp(self):
        super().setUp()
        self.deepseek = Adapter('opencode', 'deepseek', 'deepseek-flash', ['high'])
        self.astra = Adapter('codex', 'codex', 'gpt-6-astra', ['medium', 'high'])
        self.plan['nodes'][0]['execution_requirement'] = requirement()

    def run_route(self, **kwargs):
        return self.run_work(self.deepseek, backends={'opencode': self.deepseek, 'codex': self.astra}, **kwargs)

    def test_R1_R2_R16_controller_cross_provider_bounded_review(self):
        implementer = copy.deepcopy(self.plan['nodes'][0])
        implementer['task_id'] = 'I'
        implementer['execution_requirement'] = requirement('deepseek-flash', 'high', 'deepseek',
            role='implementer', required_capabilities=['implementation'])
        tool = copy.deepcopy(implementer)
        tool.update(task_id='T', executor='TOOL_ONLY', routing=None, depends_on=['I'],
                    tool={'operation': 'repository_state', 'args': {}})
        tool.pop('execution_requirement')
        reviewer = self.plan['nodes'][0]
        reviewer['depends_on'] = ['I', 'T']
        self.plan['nodes'] = [implementer, tool, reviewer]
        self.plan['order'] = ['I', 'T', 'A1']
        report = self.run_route()
        self.assertEqual('SUCCESS', report['nodes']['A1']['status'], report.get('error'))
        self.assertEqual(['implementer'], [c['role'] for c in self.deepseek.calls])
        self.assertEqual(['reviewer'], [c['role'] for c in self.astra.calls])
        prompt = self.astra.calls[0]['prompt']
        self.assertNotIn('PRIVATE SCRATCHPAD', prompt)
        self.assertIn('Role: reviewer', prompt)
        self.assertIn('result_reference', prompt)
        self.assertEqual(2, report['calls_used'])

    def test_R3_exact_medium_and_cli_parameters(self):
        report = self.run_route()
        self.assertEqual('medium', report['resolutions'][0]['native_variant'])
        req = self.astra.calls[0]
        command = execution.CliBackend('codex', 'codex')._command(req)
        self.assertIn('model_reasoning_effort=medium', command)
        self.assertIn('gpt-6-astra', command)
        req = {**req, 'provider': 'deepseek', 'model': 'deepseek-flash', 'native_variant': 'high'}
        command = execution.CliBackend('opencode', 'opencode')._command(req)
        self.assertIn('deepseek/deepseek-flash#high', command)
        self.assertNotIn('--variant', command)
        self.assertNotIn('--dir', command)

    def test_R4_only_high_is_not_medium(self):
        self.astra.catalog[0]['efforts'] = ['high']
        self.assertEqual('BLOCKED_EFFORT_UNAVAILABLE', self.run_route()['error'])
        self.assertEqual([], self.astra.calls)

    def test_R5_unavailable_exact_model(self):
        self.astra.catalog = []
        self.assertEqual('BLOCKED_MODEL_UNAVAILABLE', self.run_route()['error'])
        self.assertEqual([], self.astra.calls)

    def test_R6_allowed_fallback_truthfully_attributed(self):
        self.astra.catalog = []
        self.plan['nodes'][0]['execution_requirement']['fallback_policy'] = {
            'mode': 'allowed', 'alternatives': [{'provider':'deepseek', 'model':'deepseek-flash', 'effort':'high'}]}
        self.deepseek.actual = {'actual_model': 'deepseek-flash', 'actual_effort': 'high'}
        report = self.run_route()
        result = report['nodes']['A1']
        self.assertEqual('SUCCESS', result['status'])
        self.assertEqual(('Astra', 'medium'), (result['requested_model'], result['requested_effort']))
        self.assertEqual(('deepseek-flash', 'high'), (result['resolved_model'], result['resolved_effort']))
        row = self.store().run_attempts(report['run_id'])[0]
        self.assertEqual(('Astra', 'medium', 'deepseek-flash', 'high'),
            tuple(row[k] for k in ('requested_model','requested_effort','actual_model','actual_effort')))

    def test_R7_actual_effort_unknown_never_fabricated(self):
        result = self.run_route()['nodes']['A1']
        self.assertIsNone(result['actual_effort'])
        self.assertEqual('UNVERIFIED', result['effort_verification'])

    def test_strict_unobservable_actual_effort_is_unverified(self):
        self.plan['nodes'][0]['execution_requirement']['require_actual_effort'] = True
        result = self.run_route()['nodes']['A1']
        self.assertEqual('UNVERIFIED', result['status'])
        self.assertEqual('ACTUAL_EFFORT_UNVERIFIED', result['failure_category'])

    def test_R8_actual_high_is_contract_violation(self):
        self.astra.actual = {'actual_effort': 'high'}
        report = self.run_route(cache=True)
        self.assertEqual('EXECUTION_CONTRACT_VIOLATION', report['nodes']['A1']['failure_category'])
        self.assertEqual('FAILURE', report['nodes']['A1']['status'])
        self.assertEqual('high', self.store().run_attempts(report['run_id'])[0]['actual_effort'])
        self.assertEqual(1, len(self.astra.calls))

    def test_R9_local_only_refuses_remote(self):
        self.assertEqual('local_only', self.run_route(constraints={'local_only':True})['error'])
        self.assertEqual([], self.astra.calls)

    def test_R10_machine_policy_precedes_catalog(self):
        self.plan['nodes'][0]['execution_requirement'] = requirement('qwen', 'medium', 'ollama')
        self.deepseek.catalog = [{'provider':'ollama','model':'qwen','efforts':['medium'], 'capabilities':['review']}]
        self.assertEqual('NOT_CONFIGURED_BY_MACHINE_POLICY', self.run_route()['error'])
        self.assertEqual([], self.deepseek.calls)

    def test_R11_equivalent_readonly_singleflight(self):
        started, release, waiting = threading.Event(), threading.Event(), threading.Event()
        self.astra.started, self.astra.release = started, release
        reports, errors = [], []
        original = active_work.WorkStore.cache_event
        def event(store, run, kind, **kwargs):
            original(store, run, kind, **kwargs)
            if kind == 'WAIT_STARTED': waiting.set()
        def run():
            try: reports.append(self.run_route())
            except BaseException as exc: errors.append(exc)
        with patch.object(active_work.WorkStore, 'cache_event', event):
            owner = threading.Thread(target=run); owner.start()
            self.assertTrue(started.wait(10))
            waiter = threading.Thread(target=run); waiter.start()
            self.assertTrue(waiting.wait(10)); release.set()
            owner.join(10); waiter.join(10)
            self.assertFalse(owner.is_alive() or waiter.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(1, len(self.astra.calls))
        self.assertEqual([0,1], sorted(len(r['attempts']) for r in reports))

    def test_R12_effort_and_actual_verification_requirements_isolate_reuse(self):
        first = self.run_route(cache=True)
        self.plan['nodes'][0]['execution_requirement']['requested_effort'] = 'high'
        second = self.run_route(cache=True)
        self.assertNotEqual(first['nodes']['A1']['master_b_identity'], second['nodes']['A1']['master_b_identity'])
        self.assertEqual(2, len(self.astra.calls))
        self.plan['nodes'][0]['execution_requirement']['require_actual_effort'] = True
        third = self.run_route(cache=True)
        self.assertEqual('UNVERIFIED', third['nodes']['A1']['status'])
        self.assertEqual(3, len(self.astra.calls))

    def test_R12_different_active_efforts_do_not_join(self):
        started, release = threading.Event(), threading.Event()
        self.astra.started, self.astra.release = started, release
        reports, errors = [], []
        def run():
            try: reports.append(self.run_route())
            except BaseException as exc: errors.append(exc)
        owner = threading.Thread(target=run); owner.start()
        try:
            self.assertTrue(started.wait(10))
            original = self.astra.execute
            def execute(request):
                self.astra.release = None
                return original(request)
            self.astra.execute = execute
            high = self.run_route(execution_requirements={'A1':requirement(effort='high')}, wait_seconds=0)
            self.assertEqual('SUCCESS', high['nodes']['A1']['status'])
            self.assertEqual(2, len(self.astra.calls))
        finally:
            release.set(); owner.join(10)
        self.assertEqual([], errors)
        self.assertEqual('SUCCESS', reports[0]['nodes']['A1']['status'])

    def test_fake_cli_never_loads_or_launches_real_backend_registry(self):
        path = self.base / 'requirements.json'
        path.write_text(json.dumps({'A1':requirement()}))
        args = orchestrate_cli.parser().parse_args(['run','--objective','review','--backend','fake',
            '--confirm','--workspace',str(self.root),'--execution-requirements',str(path),
            '--no-store','--no-evidence','--json'])
        with patch.object(orchestrate_cli.orchestrator, 'decompose', return_value=self.plan), \
             patch.object(orchestrate_cli, '_local_health', side_effect=AssertionError('real registry loaded')), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(2, args.func(args))
        self.assertEqual([], self.astra.calls + self.deepseek.calls)

    def test_R13_unavailable_provider_has_no_attempt(self):
        self.astra.present = False
        report = self.run_route()
        self.assertEqual('BLOCKED_PROVIDER_UNAVAILABLE', report['error'])
        self.assertEqual([], self.astra.calls)
        self.assertEqual(0, self.store().status()['attempts'])

    def test_R14_launch_attribution_is_durable_before_return(self):
        def execute(request):
            store = self.store()
            rows = store.connection.execute('SELECT * FROM attempts').fetchall()
            self.assertEqual(1, len(rows))
            row = dict(rows[0])
            self.assertEqual(('LAUNCHED','Astra','medium','gpt-6-astra','medium'),
                tuple(row[k] for k in ('status','requested_model','requested_effort','resolved_model','resolved_effort')))
            return {'status':'SUCCESS','completion_signal':'agent_reported','acceptance_results':{'ok':True}}
        self.astra.execute = execute
        self.assertEqual('SUCCESS', self.run_route()['nodes']['A1']['status'])

    def test_R15_tool_only_zero_calls(self):
        node = self.plan['nodes'][0]
        node.pop('execution_requirement')
        node.update(executor='TOOL_ONLY', routing=None, tool={'operation':'repository_state','args':{}})
        report = self.run_route(controller_ai_calls=0)
        self.assertEqual(0, report['calls_used'])
        self.assertEqual([], self.astra.calls + self.deepseek.calls)

    def test_ignored_adapter_effort_is_blocked_prelaunch(self):
        self.astra.resolve = lambda *args: {'provider':'codex','model':'gpt-6-astra','native_variant':'high'}
        self.assertEqual('BLOCKED_EFFORT_UNAVAILABLE', self.run_route()['error'])
        self.assertEqual([], self.astra.calls)

    def test_implicit_failure_escalation_forbidden_for_exact_request(self):
        self.astra.failure = 'REASONING_INSUFFICIENT'
        report = self.run_route()
        self.assertEqual(1, len(self.astra.calls))
        self.assertEqual([], self.deepseek.calls)
        self.assertEqual('FAILURE', report['nodes']['A1']['status'])

    def test_disallowed_destination_and_capability(self):
        req = self.plan['nodes'][0]['execution_requirement']
        req['allowed_destinations'] = [{'backend':'opencode','provider':'codex'}]
        self.assertEqual('BLOCKED_MODEL_UNAVAILABLE', self.run_route()['error'])
        req.pop('allowed_destinations'); req['required_capabilities'] = ['not-supported']
        self.assertEqual('BLOCKED_CAPABILITY_UNAVAILABLE', self.run_route()['error'])

    def test_invalid_requirement_fails_before_launch(self):
        self.plan['nodes'][0]['execution_requirement']['require_actual_effort'] = 'false'
        with self.assertRaisesRegex(execution.ExecutionError, 'EXECUTION_REQUIREMENT_INVALID'):
            self.run_route()
        self.assertEqual([], self.astra.calls)

    def test_public_requirement_map_and_resume_identity(self):
        self.plan['nodes'][0].pop('execution_requirement')
        report = self.run_route(run_id='exact', execution_requirements={'A1':requirement()})
        self.assertEqual('SUCCESS', report['nodes']['A1']['status'])
        resumed = self.run_route(run_id='exact', resume=True, execution_requirements={'A1':requirement(effort='high')})
        self.assertTrue(resumed.get('error','').startswith('REFUSE_RESUME'))
        self.assertEqual(1, len(self.astra.calls))

    def test_cli_and_direct_programmatic_requirements_reach_same_resolver(self):
        path = self.base / 'requirements.json'
        path.write_text(json.dumps({'A1':requirement()}))
        self.plan['nodes'][0].pop('execution_requirement')
        args = orchestrate_cli.parser().parse_args(['direct', '--objective','review', '--confirm',
            '--allow-dirty','--workspace',str(self.root),'--state-dir',str(self.state),
            '--execution-requirements',str(path),'--no-evidence','--no-store','--json'])
        health = failover.Health(binaries={'codex':True,'opencode':True},
            providers={'codex':{'codex'},'opencode':{'deepseek'}},codex_model='gpt-6-astra')
        with patch.object(orchestrate_cli.orchestrator, 'decompose', return_value=self.plan), \
             patch.object(execution, 'backend_for', return_value=self.deepseek), \
             patch.object(orchestrate_cli, '_local_health', return_value=(health,{'codex':self.astra})), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, args.func(args))
        with patch.object(lifecycle.orchestrator, 'decompose', return_value=self.plan):
            _, report = lifecycle.run_direct('review', backend=self.deepseek,
                repo_root=self.root,state_dir=self.state,confirm=True,allow_dirty=True,
                record_evidence=False,backends={'codex':self.astra},
                execution_requirements={'A1':requirement(effort='high')})
        self.assertEqual('high', report['resolutions'][0]['native_variant'])
        self.assertEqual(['medium','high'], [c['native_variant'] for c in self.astra.calls])

    def test_allowed_fallback_after_launch_including_same_backend(self):
        self.astra.failure = 'RATE_LIMIT'
        self.astra.catalog.append({'provider':'codex', 'model':'alternative',
                                   'efforts':['high'], 'capabilities':['review']})
        self.plan['nodes'][0]['execution_requirement']['fallback_policy'] = {'mode':'allowed',
            'alternatives':[{'provider':'codex','model':'alternative','effort':'high'}]}
        original = self.astra.execute
        def execute(request):
            if request['model'] == 'alternative': self.astra.failure = None
            return original(request)
        self.astra.execute = execute
        report = self.run_route(cache=True)
        self.assertEqual(['gpt-6-astra', 'alternative'], [c['model'] for c in self.astra.calls])
        self.assertEqual('SUCCESS', report['nodes']['A1']['status'])
        self.assertEqual(1, report['nodes']['A1']['fallback_index'])
        self.assertTrue(report['attempts'][1]['is_fallback'])
        second = self.run_route(cache=True)
        self.assertEqual('CACHE', second['nodes']['A1']['execution_source'])
        self.assertEqual(2, len(self.astra.calls))

    def test_forbidden_backend_fallback_after_launch(self):
        self.astra.failure = 'RATE_LIMIT'
        report = self.run_route()
        self.assertEqual('FAILURE', report['nodes']['A1']['status'])
        self.assertEqual(1, len(self.astra.calls))
        self.assertEqual([], self.deepseek.calls)

    def test_strict_actual_proof_does_not_hide_failed_provider_or_prevent_allowed_fallback(self):
        self.astra.failure = 'RATE_LIMIT'
        self.deepseek.actual = {'actual_model':'deepseek-flash','actual_effort':'high'}
        req = self.plan['nodes'][0]['execution_requirement']
        req['require_actual_effort'] = True
        req['fallback_policy'] = {'mode':'allowed', 'alternatives':[
            {'provider':'deepseek','model':'deepseek-flash','effort':'high'}]}
        report = self.run_route()
        self.assertEqual('RATE_LIMIT', report['attempts'][0]['failure_category'])
        self.assertEqual('SUCCESS', report['nodes']['A1']['status'])
        self.assertEqual('high', report['nodes']['A1']['actual_effort'])

    def test_readonly_violation_cannot_be_hidden_by_effort_mismatch(self):
        original = self.astra.execute
        def execute(request):
            (self.root / 'f.txt').write_text('unauthorized mutation')
            return {**original(request), 'actual_effort':'high'}
        self.astra.execute = execute
        report = self.run_route()
        self.assertEqual('READ_ONLY_VIOLATION', report['nodes']['A1']['failure_category'])
        self.assertEqual('OUTCOME_UNCERTAIN', self.store().connection.execute(
            'SELECT state FROM active_work').fetchone()[0])

    def test_exact_health_unknown_or_cooldown_is_not_overridden(self):
        health = failover.Health(binaries={'codex':True}, providers={'codex':{'codex'}},
                                unavailable={'codex':failover.UNKNOWN})
        self.assertEqual('BLOCKED_PROVIDER_UNAVAILABLE', self.run_route(health=health)['error'])
        health.unavailable.clear(); health.tripped.add('codex')
        self.assertEqual('BLOCKED_PROVIDER_UNAVAILABLE', self.run_route(health=health)['error'])

    def test_cli_output_does_not_prove_actual_effort(self):
        completed = type('Completed', (), {'returncode':0, 'stderr':'',
            'stdout':'{"actual_effort":"medium", "actual_model":"gpt-6-astra"}'})()
        self.run_route()
        with patch.object(execution.subprocess, 'run', return_value=completed):
            result = execution.CliBackend('codex', 'codex').execute(self.astra.calls[0])
        self.assertNotIn('actual_effort', result)
        self.assertNotIn('actual_model', result)


class CatalogTests(unittest.TestCase):
    def test_legacy_plan_and_delivered_identity_are_unchanged_without_exact_requirement(self):
        plan = _plan()
        # Frozen Master B fixture signatures: adding an optional requirement must not
        # invalidate ordinary persisted plans or rewrite their delivered identity.
        self.assertEqual('f812024a549599fc8d47e535559c474e', execution._plan_fingerprint(plan))
        self.assertEqual('cb3c169aaaf873e26e3c81d26b6c2f4f',
                         execution.delivered_input_fingerprint(plan['nodes'][0], [], None))

    def test_codex_exact_account_catalog_and_malformed_config(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'CODEX_HOME':tmp}):
            home = pathlib.Path(tmp)
            (home / 'models_cache.json').write_text(json.dumps({'models':[{'slug':'gpt-6-astra',
                'visibility':'list','supported_reasoning_levels':[{'effort':'medium'}]}]}))
            self.assertEqual(['medium'], discovery.backend_models('codex')[0]['efforts'])
            (home / 'config.toml').write_text('model_provider="other"')
            self.assertEqual([], discovery.backend_models('codex'))
            (home / 'config.toml').write_text('[invalid')
            self.assertEqual([], discovery.backend_models('codex'))

    def test_opencode_reuses_declared_destinations_not_worker_discovery(self):
        rows = discovery.backend_models('opencode')
        row = next(r for r in rows if r['provider']=='deepseek' and r['model']=='deepseek-flash')
        self.assertEqual(['high','max'], row['efforts'])
        self.assertNotIn('medium', row['efforts'])


if __name__ == '__main__':
    unittest.main()
