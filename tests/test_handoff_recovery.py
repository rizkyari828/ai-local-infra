"""Persisted-evidence regressions through production cache and resume boundaries."""
import json
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path[:0] = [str(pathlib.Path(__file__).resolve().parents[1] / 'scripts/common'),
                str(pathlib.Path(__file__).resolve().parent)]
import active_work
import test_verified_handoff as handoff


class AdoptionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.case = handoff.VerifiedHandoffTests('test_warm_reuse_keeps_complete_evidence')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.reviewer['acceptance_criteria'] += ['tests_passed', 'scope_checked']
        self.case.astra.extra = {'acceptance_results': {'tests_passed': True, 'scope_checked': {'passed': True}}}

    def corrupt_cache(self, task, change):
        store = self.case.store()
        for row in store.connection.execute('SELECT rowid,result FROM accepted_work').fetchall():
            result = json.loads(row['result'])
            if result['task_id'] == task:
                change(result)
                store.connection.execute('UPDATE accepted_work SET result=? WHERE rowid=?',
                                         (json.dumps(result), row['rowid']))
        store.connection.commit()

    def corrupt_resume(self, task, change):
        path = next(self.case.state.rglob('state.json'))
        state = json.loads(path.read_text())
        change(state['nodes'][task])
        path.write_text(json.dumps(state))

    def assert_refused(self, report, task, acceptance, *, historical=None):
        node = report['nodes'][task]
        self.assertEqual('BLOCKED', node['status'])
        self.assertEqual(acceptance, node['acceptance'])
        self.assertEqual([], report['attempts'])
        self.assertEqual('FAILED', self.case.store().get_run(report['run_id'])['status'])
        tasks = {t['task_id']: t for t in self.case.store().run_tasks(report['run_id'])}
        self.assertEqual('BLOCKED', tasks[task]['status'])
        self.assertIn(task, report['final']['unresolved_blockers'])
        self.assertGreater(report['final']['blocked'], 0)
        events = self.case.store().connection.execute(
            'SELECT event_type,payload FROM run_events WHERE run_id=? ORDER BY rowid',
            (report['run_id'],)).fetchall()
        self.assertEqual('RUN_FAILED', events[-1]['event_type'])
        rejected = [e for e in events if e['event_type'] == 'CACHE_REJECTED']
        self.assertTrue(rejected)
        if historical is not None:
            self.assertEqual(historical, self.case.store().run_attempts(report['run_id']))
        self.assertEqual((1, 1), (len(self.case.deepseek.calls), len(self.case.astra.calls)))

    def test_R1_R6_valid_all_criteria_cache_adoption_and_identity_unchanged(self):
        first = self.case.workflow(cache=True)
        second = self.case.workflow(cache=True)
        self.assertEqual(0, second['calls_used'])
        self.assertEqual([], second['attempts'])
        self.assertEqual('SUCCESS', second['nodes']['R']['status'])
        self.assertEqual(first['nodes']['R']['master_b_identity'], second['nodes']['R']['master_b_identity'])
        self.assertEqual(first['nodes']['R']['verified_result'], second['nodes']['R']['verified_result'])

    def test_verified_store_adoption_requires_authoritative_criteria_check(self):
        first = self.case.workflow(cache=True)
        identity = first['nodes']['R']['master_b_identity']
        with self.assertRaisesRegex(active_work.WorkError, 'PERSISTED_ACCEPTANCE_UNVERIFIED'):
            self.case.store().accepted(identity)

    def test_R2_R4_false_secondary_criterion_refuses_cache_without_repair_or_launch(self):
        self.case.workflow(cache=True)
        self.corrupt_cache('R', lambda r: r['acceptance_results'].update(tests_passed=False))
        report = self.case.workflow(cache=True)
        self.assertEqual('PERSISTED_ACCEPTANCE_FAILED', report.get('error'))
        self.assert_refused(report, 'R', 'FAILED')

    def test_R3_R10_failed_criterion_refuses_resume_preserving_attempt_history(self):
        first = self.case.workflow(run_id='resume-false')
        attempts = self.case.store().run_attempts(first['run_id'])
        self.corrupt_resume('R', lambda r: r['acceptance_results'].update(tests_passed=False))
        report = self.case.workflow(run_id=first['run_id'], resume=True)
        self.assertEqual('REFUSE_RESUME_ACCEPTANCE_FAILED', report.get('error'))
        self.assert_refused(report, 'R', 'FAILED', historical=attempts)
        self.assertEqual('SUCCESS', report['nodes']['I']['status'])
        self.assertEqual(1, report['final']['succeeded'])

    def test_R5_missing_required_criterion_stays_unverified_in_cache(self):
        self.case.workflow(cache=True)
        self.corrupt_cache('R', lambda r: r['acceptance_results'].pop('scope_checked'))
        report = self.case.workflow(cache=True)
        self.assertEqual('PERSISTED_ACCEPTANCE_UNVERIFIED', report.get('error'))
        self.assert_refused(report, 'R', 'UNVERIFIED')

    def test_R5_missing_required_criterion_stays_unverified_on_resume(self):
        first = self.case.workflow(run_id='resume-missing-criterion')
        self.corrupt_resume('R', lambda r: r['acceptance_results'].pop('scope_checked'))
        report = self.case.workflow(run_id=first['run_id'], resume=True)
        self.assert_refused(report, 'R', 'UNVERIFIED')

    def test_R6_each_typed_criterion_is_revalidated(self):
        self.case.workflow(cache=True)
        self.corrupt_cache('R', lambda r: r['acceptance_results'].update(scope_checked={'passed': False}))
        self.assert_refused(self.case.workflow(cache=True), 'R', 'FAILED')

    def test_R6_truthy_string_is_unverified(self):
        self.case.workflow(cache=True)
        self.corrupt_cache('R', lambda r: r['acceptance_results'].update(tests_passed='true'))
        self.assert_refused(self.case.workflow(cache=True), 'R', 'UNVERIFIED')

    def test_R7_R8_R9_R15_missing_reviewer_proof_refuses_and_finalizes_failed(self):
        first = self.case.workflow(run_id='resume-missing-review-proof')
        attempts = self.case.store().run_attempts(first['run_id'])
        self.corrupt_resume('R', lambda r: r.pop('verified_result'))
        report = self.case.workflow(run_id=first['run_id'], resume=True)
        self.assertEqual('REFUSE_RESUME_VERIFIED_EVIDENCE_INVALID', report.get('error'))
        self.assert_refused(report, 'R', 'UNVERIFIED', historical=attempts)
        self.assertEqual({'I': 'SUCCESS', 'R': 'BLOCKED'}, {k: v['status'] for k, v in report['nodes'].items()})
        self.assertEqual(1, report['final']['succeeded'])
        self.assertEqual(1, report['final']['blocked'])
        persisted = json.loads(next(self.case.state.rglob('state.json')).read_text())
        self.assertEqual('BLOCKED', persisted['nodes']['R']['status'])

    def test_R11_invalid_source_cache_cannot_launch_or_reuse_reviewer(self):
        self.case.workflow(cache=True)
        self.corrupt_cache('I', lambda r: r['acceptance_results'].update(structured_result=False))
        report = self.case.workflow(cache=True)
        self.assert_refused(report, 'I', 'FAILED')
        self.assertEqual('BLOCKED', report['nodes']['R']['status'])
        self.assertEqual(2, report['final']['blocked'])

    def test_R11_resume_invalidates_source_and_descendants(self):
        first = self.case.workflow(run_id='resume-failed-source')
        attempts = self.case.store().run_attempts(first['run_id'])
        self.corrupt_resume('I', lambda r: r['acceptance_results'].update(structured_result=False))
        report = self.case.workflow(run_id=first['run_id'], resume=True)
        self.assert_refused(report, 'I', 'FAILED', historical=attempts)
        self.assertEqual(0, report['final']['succeeded'])
        self.assertEqual(2, report['final']['blocked'])

    def test_corrupt_fingerprint_refuses_cache_no_replacement_worker(self):
        self.case.workflow(cache=True)
        self.corrupt_cache('R', lambda r: r['verified_result'].update(evidence_fingerprint='mismatch'))
        self.assert_refused(self.case.workflow(cache=True), 'R', 'UNVERIFIED')

    def test_R13_R17_successful_resume_preserves_attribution_and_attempts(self):
        first = self.case.workflow(run_id='valid-resume')
        attempts = self.case.store().run_attempts(first['run_id'])
        report = self.case.workflow(run_id=first['run_id'], resume=True)
        self.assertNotIn('error', report)
        self.assertEqual(2, report['final']['succeeded'])
        self.assertEqual('SUCCEEDED', self.case.store().get_run(first['run_id'])['status'])
        self.assertEqual(attempts, self.case.store().run_attempts(first['run_id']))
        self.assertEqual([], report['attempts'])
        for task in ['I', 'R']:
            for key in ['requested_model', 'requested_effort', 'resolved_model', 'resolved_effort', 'actual_model', 'actual_effort']:
                self.assertEqual(first['nodes'][task][key], report['nodes'][task][key])

    def test_adoption_race_revalidates_inside_claim_acquisition(self):
        self.case.workflow(cache=True)
        self.corrupt_cache('R', lambda r: r['acceptance_results'].update(tests_passed=False))
        original = active_work.WorkStore.accepted
        calls = []
        def skip_first(store, identity, **kwargs):
            if 'review_result' in identity['execution_compatibility'].get('result_contract', {}):
                calls.append(kwargs.get('acceptance_check'))
                if len(calls) == 1:
                    return None
            return original(store, identity, **kwargs)
        with patch.object(active_work.WorkStore, 'accepted', skip_first):
            report = self.case.workflow(cache=True)
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(all(callable(check) for check in calls))
        self.assert_refused(report, 'R', 'FAILED')

    def test_R14_publication_crash_keeps_valid_adoption_possible(self):
        with patch.object(active_work.WorkStore, 'complete', side_effect=OSError('publication crash')):
            self.case.workflow(cache=True)
        self.assertEqual([], self.case.astra.calls)
        recovered = self.case.workflow(cache=True)
        self.assertEqual('SUCCESS', recovered['nodes']['R']['status'])
        self.assertEqual((1, 1), (len(self.case.deepseek.calls), len(self.case.astra.calls)))

    def test_exact_output_cached_evidence_remains_strict(self):
        node = self.case.implementer
        node.pop('structured_result')
        node.update(acceptance_criteria=['exact_output'], exact_output='TOKEN')
        self.case.plan.update(nodes=[node], order=['I'])
        self.case.deepseek.body = 'TOKEN'
        self.case.workflow(cache=True)
        self.assertEqual(0, self.case.workflow(cache=True)['calls_used'])
        self.corrupt_cache('I', lambda r: r.update(exact_output_fingerprint='0' * 64))
        report = self.case.workflow(cache=True)
        self.assertEqual('PERSISTED_ACCEPTANCE_FAILED', report.get('error'))
        self.assertEqual([], report['attempts'])
        self.assertEqual(1, len(self.case.deepseek.calls))

    def test_refusal_only_invalidates_affected_descendants(self):
        import copy
        unrelated = copy.deepcopy(self.case.implementer)
        unrelated['task_id'] = 'U'
        self.case.plan['nodes'].insert(1, unrelated)
        self.case.plan['order'].insert(1, 'U')
        first = self.case.workflow(run_id='scoped-refusal')
        self.corrupt_resume('I', lambda r: r['acceptance_results'].update(structured_result=False))
        report = self.case.workflow(run_id=first['run_id'], resume=True)
        self.assertEqual({'I': 'BLOCKED', 'U': 'SUCCESS', 'R': 'BLOCKED'},
                         {k: v['status'] for k, v in report['nodes'].items()})
        self.assertEqual(1, report['final']['succeeded'])
        self.assertEqual([], report['attempts'])


if __name__ == '__main__':
    unittest.main()
