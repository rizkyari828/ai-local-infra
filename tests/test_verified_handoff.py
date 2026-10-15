"""Offline validation and real controller handoff; no provider inference."""
import copy
import json
import pathlib
import sys
import types
import threading
import unittest
from unittest.mock import patch

sys.path[:0] = [str(pathlib.Path(__file__).resolve().parents[1] / 'scripts/common'),
                str(pathlib.Path(__file__).resolve().parent)]
import deterministic
import execution
import result_cache
from test_master_b import Fixture
from test_orchestration_routing import Adapter, requirement


def output(source='ABCD', footer=''):
    return ('BEGIN_VERIFIABLE_RESULT\nSOURCE: ' + source + '\nREVERSED: ' + source[::-1]
            + '\nLENGTH: ' + str(len(source)) + '\nEND_VERIFIABLE_RESULT' + footer)


REVIEW = 'BEGIN_REVIEW_RESULT\nREVIEW_VERDICT: PASS\nEND_REVIEW_RESULT'


class CaptureAdapter(Adapter):
    def __init__(self, name, provider, model, effort, body):
        super().__init__(name, provider, model, [effort])
        self.body, self.complete, self.extra = body, True, {}

    def execute(self, request):
        self.calls.append(copy.deepcopy(request))
        if self.started:
            self.started.set()
        if self.release and not self.release.wait(10):
            raise RuntimeError('owner not released')
        return {'status': 'SUCCESS', 'completion_signal': 'agent_reported',
                'summary': 'PRIVATE SCRATCHPAD MUST NOT CROSS', 'scratchpad': 'HIDDEN_REASONING',
                'response_text': self.body, 'response_complete': self.complete,
                'acceptance_results': {'structured_result': True, 'review_result': True}, **self.extra}


class VerifiedHandoffTests(Fixture):
    def setUp(self):
        super().setUp()
        implementer = self.plan['nodes'][0]
        implementer.update(task_id='I', acceptance_criteria=['structured_result'],
            structured_result={'kind': 'reverse_text_v1', 'source': 'ABCD', 'allow_explanation': True},
            execution_requirement=requirement('deepseek-flash', 'high', 'deepseek', role='implementer',
                                               required_capabilities=['implementation']))
        reviewer = copy.deepcopy(implementer)
        reviewer.pop('structured_result')
        reviewer.update(task_id='R', depends_on=['I'], objective_summary='Independently review the verified transformation',
            acceptance_criteria=['review_result'], review_result={'source_task_id': 'I', 'allow_explanation': True},
            execution_requirement=requirement())
        self.plan['nodes'] = [implementer, reviewer]
        self.plan['order'] = ['I', 'R']
        self.implementer, self.reviewer = implementer, reviewer
        self.deepseek = CaptureAdapter('opencode', 'deepseek', 'deepseek-flash', 'high', output())
        self.astra = CaptureAdapter('codex', 'codex', 'gpt-6-astra', 'medium', REVIEW)

    def workflow(self, **kwargs):
        return self.run_work(self.deepseek, backends={'opencode': self.deepseek, 'codex': self.astra}, **kwargs)

    def test_H1_H7_H8_H15_H16_controller_validated_cross_provider_handoff(self):
        report = self.workflow()
        self.assertEqual(['SUCCESS', 'SUCCESS'], [report['nodes'][k]['status'] for k in ['I', 'R']], report)
        self.assertEqual(2, report['calls_used'])
        self.assertEqual(['implementer', 'reviewer'], [a['role'] for a in report['attempts']])
        self.assertEqual([1, 1], [len(self.deepseek.calls), len(self.astra.calls)])
        proof = report['nodes']['I']['verified_result']
        self.assertEqual(output(), proof['captured_output'])
        self.assertEqual({'SOURCE': 'ABCD', 'REVERSED': 'DCBA', 'LENGTH': '4'}, proof['verification']['expected'])
        self.assertTrue(deterministic.valid_verified_result(proof))
        request = self.astra.calls[0]
        self.assertEqual(proof, request['dependency_summaries'][0]['verified_result'])
        prompt = request['prompt']
        self.assertIn('captured_output', prompt)
        self.assertIn('DETERMINISTIC_FACTS_MATCH', prompt)
        self.assertIn(execution.DATA_AUTHORITY_HEADER, prompt)
        self.assertGreater(prompt.index('captured_output'), prompt.index(execution.DATA_AUTHORITY_HEADER))
        self.assertNotIn('PRIVATE SCRATCHPAD', prompt)
        self.assertNotIn('HIDDEN_REASONING', prompt)
        review_proof = report['nodes']['R']['verified_result']['reviewed_source']
        self.assertEqual(report['nodes']['I']['result_fingerprint'], review_proof['result_fingerprint'])
        self.assertEqual(proof['evidence_fingerprint'], review_proof['evidence_fingerprint'])
        for key, requested, resolved, effort in [('I', 'deepseek-flash', 'deepseek-flash', 'high'),
                                                 ('R', 'Astra', 'gpt-6-astra', 'medium')]:
            node = report['nodes'][key]
            self.assertEqual((requested, resolved, effort, effort), tuple(node[f] for f in
                ['requested_model', 'resolved_model', 'requested_effort', 'resolved_effort']))
            self.assertIsNone(node['actual_model']); self.assertIsNone(node['actual_effort'])
        self.assertEqual((0, 0), (report['accounting']['retries'], report['accounting']['fallbacks']))
        self.assertEqual(2, len(self.store().run_attempts(report['run_id'])))

    def test_H2_H9_H14_wrong_fact_blocks_reviewer_despite_positive_claim(self):
        self.deepseek.body = output().replace('REVERSED: DCBA', 'REVERSED: ABCD')
        report = self.workflow()
        self.assertEqual('ACCEPTANCE_FAILED', report['nodes']['I']['failure_category'])
        self.assertEqual('BLOCKED', report['nodes']['R']['status'])
        self.assertEqual([], self.astra.calls)
        self.assertEqual(1, len(report['attempts']))

    def test_H3_permitted_explanation_remains_in_complete_evidence(self):
        self.deepseek.body = output(footer='\n\nNo files accessed. No changes.')
        report = self.workflow()
        self.assertEqual('SUCCESS', report['nodes']['R']['status'])
        self.assertEqual(self.deepseek.body, report['nodes']['I']['verified_result']['captured_output'])

    def test_H4_exact_output_still_rejects_footer(self):
        node = {'acceptance_criteria': ['exact_output'], 'exact_output': output()}
        result = {'response_text': output(footer='\nfooter'), 'response_complete': True}
        self.assertEqual('FAILED', execution._acceptance(node, result)[0])

    def test_H5_missing_or_incomplete_capture_unverified_and_blocks_reviewer(self):
        self.deepseek.complete = False
        report = self.workflow()
        self.assertEqual('UNVERIFIED', report['nodes']['I']['status'])
        self.assertEqual([], self.astra.calls)
        self.assertEqual(1, len(report['attempts']))
        self.assertEqual('UNVERIFIED', deterministic.verify_response('structured_result',
            self.implementer['structured_result'], 'no block supplied', True)['status'])

    def test_H6_malformed_ambiguous_or_unexpected_fields_fail(self):
        bad = [output() + '\n' + output(), output().replace('LENGTH: 4', 'LENGTH: 04'),
               output().replace('LENGTH: 4', 'LENGTH: 4\nLENGTH: 4'),
               output().replace('LENGTH: 4\n', ''), output().replace('LENGTH: 4', 'LENGTH: 4\nEXTRA: x'),
               output().replace('SOURCE: ABCD', 'SOURCE: OTHER'),
               output().replace('LENGTH: 4', 'length: 4'), output().replace('END_VERIFIABLE_RESULT', ''),
               output(footer='\nSOURCE: OTHER')]
        for body in bad:
            with self.subTest(body=body):
                self.assertEqual('FAILED', deterministic.verify_response('structured_result',
                    self.implementer['structured_result'], body, True)['status'])

    def test_H10_changed_capture_prevents_stale_reviewer_cache_adoption(self):
        self.implementer['freshness_policy'] = result_cache.FRESHNESS_RUNTIME_VOLATILE
        first = self.workflow(cache=True)
        self.deepseek.body = output(footer='\nDifferent permitted explanation.')
        second = self.workflow(cache=True)
        self.assertEqual(2, len(self.astra.calls))
        self.assertNotEqual(first['nodes']['I']['result_fingerprint'], second['nodes']['I']['result_fingerprint'])
        self.assertNotEqual(first['nodes']['R']['master_b_identity'], second['nodes']['R']['master_b_identity'])

    def test_H11_resume_preserves_provenance_without_launch(self):
        first = self.workflow(run_id='durable-handoff', cache=True)
        second = self.workflow(run_id='durable-handoff', cache=True, resume=True)
        self.assertNotIn('error', second)
        self.assertEqual(first['nodes']['I']['verified_result'], second['nodes']['I']['verified_result'])
        self.assertEqual(first['nodes']['R']['verified_result'], second['nodes']['R']['verified_result'])
        self.assertEqual((1, 1), (len(self.deepseek.calls), len(self.astra.calls)))

    def test_H11_publication_crash_recovers_evidence_without_relaunch(self):
        import active_work
        with patch.object(active_work.WorkStore, 'complete', side_effect=OSError('publication crash')):
            first = self.workflow(cache=True)
        self.assertEqual('BLOCKED', first['nodes']['I']['status'])
        self.assertEqual([], self.astra.calls)
        second = self.workflow(cache=True)
        self.assertEqual('SUCCESS', second['nodes']['R']['status'])
        self.assertEqual(1, len(self.deepseek.calls))
        self.assertEqual(1, len(self.astra.calls))
        self.assertEqual(output(), second['nodes']['I']['verified_result']['captured_output'])

    def test_H11_corrupt_resume_evidence_does_not_manufacture_acceptance(self):
        first = self.workflow(run_id='corrupt-handoff')
        state_path = self.state / 'orchestration' / 'runs' / first['run_id'] / 'state.json'
        if not state_path.exists():
            state_path = next(self.state.rglob('state.json'))
        state = json.loads(state_path.read_text())
        state['nodes']['I'].pop('verified_result')
        state_path.write_text(json.dumps(state))
        report = self.workflow(run_id='corrupt-handoff', resume=True)
        self.assertIn('REFUSE_RESUME', report.get('error', ''))
        self.assertEqual((1, 1), (len(self.deepseek.calls), len(self.astra.calls)))

    def test_H12_sensitive_or_oversized_capture_fails_before_publication(self):
        for footer in ['\napi_key=sk-' + 'a'*40, '\n' + 'x'*2048]:
            self.deepseek.body = output(footer=footer)
            report = self.workflow()
            self.assertEqual('FAILURE', report['nodes']['I']['status'])
            self.assertNotIn('verified_result', report['nodes']['I'])
        self.assertEqual([], self.astra.calls)
        self.assertEqual(0, self.store().connection.execute('SELECT COUNT(*) FROM accepted_work').fetchone()[0])

    def test_H13_reviewer_verdict_separate_from_execution_and_attribution(self):
        self.astra.body = REVIEW.replace('PASS', 'FAIL') + '\nI disagree.'
        report = self.workflow()
        node = report['nodes']['R']
        self.assertEqual('ACCEPTANCE_FAILED', node['failure_category'])
        self.assertEqual('SUCCESS', node['execution_status'])
        self.assertEqual('agent_reported', node['completion_signal'])
        self.assertEqual('FAIL', node['result_verification']['fields']['REVIEW_VERDICT'])
        self.assertIsNone(node['actual_effort'])
        self.assertEqual(2, len(report['attempts']))

    def test_reviewer_missing_ambiguous_and_invalid_verdicts_never_pass(self):
        spec = self.reviewer['review_result']
        self.assertEqual('UNVERIFIED', deterministic.verify_response('review_result', spec, 'PASS', True)['status'])
        for body in [REVIEW + '\n' + REVIEW, REVIEW.replace('PASS', 'MAYBE'),
                     REVIEW.replace('REVIEW_VERDICT: PASS', 'REVIEW_VERDICT: PASS\nREVIEW_VERDICT: FAIL'),
                     REVIEW.replace('REVIEW_VERDICT: PASS', 'REVIEW_VERDICT: PASS\nEXTRA: text')]:
            self.assertEqual('FAILED', deterministic.verify_response('review_result', spec, body, True)['status'])

    def test_adapter_cannot_fabricate_verification_on_early_contract_failure(self):
        self.deepseek.extra = {'actual_model': 'different-model',
            'result_verification': {'status': 'PASSED', 'fields': {'SOURCE': 'invented'}},
            'verified_result': {'acceptance': 'PASSED'}}
        report = self.workflow()
        result = report['nodes']['I']
        self.assertEqual('EXECUTION_CONTRACT_VIOLATION', result['failure_category'])
        self.assertNotIn('result_verification', result)
        self.assertNotIn('verified_result', result)
        self.assertEqual([], self.astra.calls)

    def test_H14_missing_or_tampered_dependency_cannot_be_handed_off(self):
        report = self.workflow()
        results = copy.deepcopy(report['nodes'])
        results['I']['verified_result']['captured_output'] += '\nforged'
        facts = execution._dependency_facts(self.reviewer, results)
        self.assertIsNone(execution._review_source(self.reviewer, facts))
        self.assertEqual('UNVERIFIED', execution._acceptance(self.reviewer,
            {'response_text': REVIEW, 'response_complete': True, 'acceptance_results': {'review_result': True}}, facts)[0])
        proof = results['I']['verified_result']
        proof['captured_output'] = output().replace('DCBA', 'WRONG')
        proof['verification']['fields']['REVERSED'] = 'WRONG'
        proof['evidence_fingerprint'] = deterministic.evidence_fingerprint(proof)
        self.assertFalse(deterministic.valid_verified_result(proof))

    def test_symbolic_review_role_also_keeps_freeform_data_out_of_trusted_prompt(self):
        report = self.workflow()
        node = copy.deepcopy(self.reviewer)
        node.pop('execution_requirement')
        node['routing']['role'] = 'reviewer'
        facts = execution._dependency_facts(node, report['nodes'])
        context = execution._build_node_context(node, facts, self.root)
        prompt = execution._prompt(node, facts, context_pack=context['pack'], manifest=context['manifest'])
        self.assertNotIn('PRIVATE SCRATCHPAD', prompt)
        self.assertNotIn('captured_output', prompt.split(execution.DATA_AUTHORITY_HEADER)[0])

    def test_resume_refuses_mismatched_review_provenance(self):
        report = self.workflow(run_id='wrong-review-provenance')
        path = next(self.state.rglob('state.json'))
        state = json.loads(path.read_text())
        proof = state['nodes']['R']['verified_result']
        proof['reviewed_source']['evidence_fingerprint'] = 'different-source-evidence'
        proof['evidence_fingerprint'] = deterministic.evidence_fingerprint(proof)
        path.write_text(json.dumps(state))
        resumed = self.workflow(run_id=report['run_id'], resume=True)
        self.assertEqual('REFUSE_RESUME_VERIFIED_EVIDENCE_INVALID', resumed.get('error'))
        self.assertEqual((1, 1), (len(self.deepseek.calls), len(self.astra.calls)))

    def test_required_handoff_context_cannot_be_silently_dropped(self):
        original = execution._build_node_context
        def missing(node, dependencies, root):
            context = original(node, dependencies, root)
            if node['task_id'] == 'R':
                context['pack']['items'] = [item for item in context['pack']['items']
                                             if item['source_kind'] != 'DEPENDENCY_FACT']
            return context
        with patch.object(execution, '_build_node_context', side_effect=missing):
            report = self.workflow()
        self.assertEqual('VERIFIED_DEPENDENCY_MISSING', report['nodes']['R']['failure_category'])
        self.assertEqual([], self.astra.calls)
        self.assertEqual(1, len(report['attempts']))

    def test_changed_source_acceptance_cannot_reuse_accepted_review(self):
        self.workflow(cache=True)
        self.implementer['acceptance_criteria'].append('additional_required_check')
        self.deepseek.extra = {'acceptance_results': {'additional_required_check': False}}
        report = self.workflow(cache=True)
        self.assertEqual('FAILURE', report['nodes']['I']['status'])
        self.assertEqual('BLOCKED', report['nodes']['R']['status'])
        self.assertEqual(1, len(self.astra.calls))

    def test_declared_explanation_policy_and_other_conditions_remain_required(self):
        self.implementer['structured_result']['allow_explanation'] = False
        self.deepseek.body = output(footer='\nfooter')
        self.assertEqual('FAILURE', self.workflow()['nodes']['I']['status'])
        self.deepseek.body = output()
        self.implementer['acceptance_criteria'].append('other')
        report = self.workflow()
        self.assertEqual('UNVERIFIED', report['nodes']['I']['status'])
        self.assertEqual([], self.astra.calls)

    def test_invalid_contract_rejected_before_launch(self):
        self.implementer['structured_result']['expected'] = {'REVERSED': 'WRONG'}
        with self.assertRaisesRegex(execution.ExecutionError, 'RESULT_CONTRACT_INVALID'):
            self.workflow()
        self.assertEqual([], self.deepseek.calls)

    def test_warm_reuse_keeps_complete_evidence(self):
        first = self.workflow(cache=True)
        second = self.workflow(cache=True)
        self.assertEqual(0, second['calls_used'])
        self.assertEqual(first['nodes']['I']['verified_result'], second['nodes']['I']['verified_result'])
        self.assertEqual(first['nodes']['R']['verified_result'], second['nodes']['R']['verified_result'])

    def test_corrupt_accepted_cache_evidence_cannot_be_adopted(self):
        self.workflow(cache=True)
        store = self.store()
        rows = store.connection.execute('SELECT rowid,result FROM accepted_work').fetchall()
        for row in rows:
            result = json.loads(row['result'])
            if result['task_id'] == 'I':
                result['verified_result']['verification']['fields']['LENGTH'] = '999'
                store.connection.execute('UPDATE accepted_work SET result=? WHERE rowid=?',
                                         (json.dumps(result), row['rowid']))
        store.connection.commit()
        report = self.workflow(cache=True)
        self.assertEqual(2, len(self.deepseek.calls))
        self.assertEqual('4', report['nodes']['I']['verified_result']['verification']['fields']['LENGTH'])

    def test_H17_verified_workflow_single_flight_preserves_two_claims(self):
        import active_work
        self.deepseek.started, self.deepseek.release = threading.Event(), threading.Event()
        waited = threading.Event()
        original = active_work.WorkStore.cache_event
        def event(store, run, kind, **kwargs):
            original(store, run, kind, **kwargs)
            if kind == 'WAIT_STARTED':
                waited.set()
        reports, errors = [], []
        def work():
            try:
                reports.append(self.workflow(cache=True))
            except BaseException as exc:
                errors.append(exc)
        with patch.object(active_work.WorkStore, 'cache_event', event):
            owner = threading.Thread(target=work); owner.start()
            self.assertTrue(self.deepseek.started.wait(10))
            waiter = threading.Thread(target=work); waiter.start()
            self.assertTrue(waited.wait(10))
            self.deepseek.release.set()
            for thread in [owner, waiter]:
                thread.join(15)
                self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(2, len(reports))
        self.assertEqual(1, len(self.deepseek.calls))
        self.assertEqual(1, len(self.astra.calls))
        self.assertTrue(all(report['nodes']['R']['status'] == 'SUCCESS' for report in reports))

    def test_H17_unpublished_uncertain_mutation_cannot_authorize_review(self):
        self.implementer['intent'] = 'MUTATING'
        self.plan['intent'] = 'MIXED'
        report = self.workflow()
        self.assertEqual('OUTCOME_UNCERTAIN', report['nodes']['I']['coordination_outcome'])
        self.assertNotIn('durable_result_id', report['nodes']['I'])
        self.assertEqual('BLOCKED', report['nodes']['R']['status'])
        self.assertEqual([], self.astra.calls)
        self.assertEqual(1, len(report['attempts']))

    def test_actual_cli_adapters_dispatch_with_stubbed_processes(self):
        opencode = execution.CliBackend('opencode', 'stub-opencode')
        codex = execution.CliBackend('codex', 'stub-codex')
        for backend, catalog in [(opencode, self.deepseek.catalog), (codex, self.astra.catalog)]:
            backend.available = lambda: True
            backend.models = lambda catalog=catalog: copy.deepcopy(catalog)
        commands = []
        original = execution.subprocess.run
        def run(command, **kwargs):
            if command[0] not in ('stub-opencode', 'stub-codex'):
                return original(command, **kwargs)
            commands.append(command)
            text = REVIEW
            if command[0] == 'stub-opencode':
                text = '\n'.join(json.dumps(event) for event in [
                    {'type': 'reasoning', 'part': {'text': 'HIDDEN_REASONING'}},
                    {'type': 'text', 'part': {'type': 'text', 'id': 'p', 'messageID': 'm', 'text': output()}},
                    {'type': 'step_finish', 'part': {'messageID': 'm', 'reason': 'stop'}}])
            return types.SimpleNamespace(returncode=0, stdout=text, stderr='')
        with patch.object(execution.subprocess, 'run', side_effect=run):
            report = self.run_work(opencode, backends={'opencode': opencode, 'codex': codex})
        self.assertEqual(['SUCCESS', 'SUCCESS'], [report['nodes'][k]['status'] for k in ['I', 'R']], report)
        self.assertEqual(2, len(commands))
        self.assertIn('deepseek/deepseek-flash#high', commands[0])
        self.assertIn('model_reasoning_effort=medium', commands[1])
        self.assertNotIn('HIDDEN_REASONING', commands[1][-1])

    def test_opencode_incomplete_or_multiple_message_capture_is_unverified(self):
        backend = execution.CliBackend('opencode', 'stub')
        for raw in ['not JSON', json.dumps({'type': 'text', 'part': {'text': output()}}),
                    json.dumps({'type': 'error', 'error': 'failure'})]:
            self.assertEqual((None, False), backend._structured_capture(raw))

    def test_H18_tool_only_remains_zero_ai(self):
        node = self.implementer
        for key in ['structured_result', 'execution_requirement']:
            node.pop(key)
        node.update(executor='TOOL_ONLY', routing=None, acceptance_criteria=[],
                    tool={'operation': 'repository_state', 'args': {}})
        self.plan.update(nodes=[node], order=['I'])
        report = self.workflow()
        self.assertEqual(0, report['calls_used'])
        self.assertEqual([], report['attempts'])


if __name__ == '__main__':
    unittest.main()
