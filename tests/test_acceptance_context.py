"""Offline exact response acceptance and preservation of authoritative context."""
import copy
import json
import pathlib
import sys
import unittest
from unittest.mock import patch
sys.path[:0] = [str(pathlib.Path(__file__).resolve().parents[1] / 'scripts/common'),
                str(pathlib.Path(__file__).resolve().parent)]
import execution
import result_cache
from test_master_b import Fixture
from test_orchestration_routing import Adapter, requirement
from test_knowledge_contract import document


class ExactAcceptanceTests(Fixture):
    def setUp(self):
        super().setUp()
        self.node = self.plan['nodes'][0]
        self.node.update(acceptance_criteria=['exact_output'], exact_output='TOKEN')

    def worker(self, response='TOKEN', complete=True, **extra):
        return execution.FakeBackend(default={'status':'SUCCESS','completion_signal':'agent_reported',
            'response_text':response, 'response_complete':complete, 'acceptance_results':{},
            'summary':response, **extra})

    def test_T1_exact_response_accepted_and_warm_reused(self):
        worker = self.worker()
        first = self.run_work(worker, cache=True)
        self.assertEqual('SUCCESS', first['nodes']['A1']['status'])
        self.assertIs(first['nodes']['A1']['acceptance_results']['exact_output'], True)
        second = self.run_work(worker, cache=True)
        self.assertEqual('CACHE', second['nodes']['A1']['execution_source'])
        self.assertEqual(1,len(worker.calls))
        self.assertNotIn('response_text', first['nodes']['A1'])

    def test_T2_footer_fails_even_if_worker_claims_pass(self):
        worker = self.worker('TOKEN\n\nSkipped all tools. No risks.',
                             acceptance_results={'exact_output':True})
        report = self.run_work(worker)
        result = report['nodes']['A1']
        self.assertEqual('FAILURE',result['status'])
        self.assertEqual('ACCEPTANCE_FAILED',result['failure_category'])
        self.assertIs(result['acceptance_results']['exact_output'],False)
        self.assertEqual(1,len(worker.calls))

    def test_T3_missing_criterion_or_capture_is_unverified(self):
        for criteria, response, complete in [([], 'TOKEN', True),
                (['exact_output'],None,True),(['exact_output'],'TOKEN',False),
                (['exact_output'],'TOKEN','true')]:
            with self.subTest(criteria=criteria,response=response,complete=complete):
                node = {**self.node,'acceptance_criteria':criteria}
                result = {'response_text':response,'response_complete':complete,
                          'acceptance_results':{'exact_output':True} if criteria else {}}
                self.assertEqual('UNVERIFIED',execution._acceptance(node,result)[0])

    def test_T4_no_whitespace_line_or_prefix_trimming(self):
        for response in ['TOKEN\n',' TOKEN','TOKEN ','TOKEN\nfooter','prefix\nTOKEN',
                         'x'*1000+'TOKEN']:
            with self.subTest(response=response[:30]):
                result = {'response_text':response,'response_complete':True,
                          'summary':'TOKEN','acceptance_results':{}}
                self.assertEqual('FAILED',execution._acceptance(self.node,result)[0])

    def test_cli_capture_is_complete_and_summary_is_not_acceptance_evidence(self):
        body = 'footer\n'+'x'*600+'TOKEN\n'
        completed = type('Completed',(),{'returncode':0,'stdout':body,'stderr':''})()
        request = {'prompt':'literal','provider':'codex','model':'gpt-6-astra',
                   'native_variant':'medium','read_only':True,'working_dir':str(self.root),
                   'task_id':'A1','acceptance_criteria':['exact_output']}
        with patch.object(execution.subprocess,'run',return_value=completed):
            result = execution.CliBackend('codex','codex').execute(request)
        self.assertEqual(body,result['response_text'])
        self.assertIs(result['response_complete'],True)
        self.assertEqual('FAILED',execution._acceptance(self.node,result)[0])
        completed.stdout=None
        with patch.object(execution.subprocess,'run',return_value=completed):
            missing=execution.CliBackend('codex','codex').execute(request)
        self.assertEqual('UNVERIFIED',execution._acceptance({**self.node,'exact_output':''},missing)[0])

    def test_all_other_acceptance_conditions_still_required(self):
        self.node['acceptance_criteria'].append('other')
        result = {'response_text':'TOKEN','response_complete':True,'acceptance_results':{}}
        self.assertEqual('UNVERIFIED',execution._acceptance(self.node,result)[0])
        result['acceptance_results']['other'] = False
        self.assertEqual('FAILED',execution._acceptance(self.node,result)[0])
        result['acceptance_results']['other'] = True
        self.assertEqual('PASSED',execution._acceptance(self.node,result)[0])

    def test_T5_T6_exact_prompt_keeps_mandatory_guidance_without_structured_format(self):
        prompt = execution._prompt(self.node,[],read_only=True)
        self.assertNotIn('return a concise structured result',prompt)
        self.assertIn('mandatory system/developer instructions',prompt)
        self.assertIn('Do not suppress mandatory safety guidance',prompt)
        self.assertIn('do not modify, create, or delete any files',prompt)
        self.assertIn('Exact output (JSON string): "TOKEN"',prompt)
        command = execution.CliBackend('codex','codex')._command({'prompt':prompt,
            'provider':'codex','model':'gpt-6-astra','native_variant':'medium','read_only':True})
        self.assertNotIn('--ignore-user-config',command)
        self.assertFalse(any('project_doc_max_bytes' in arg for arg in command))
        ordinary = {**self.node,'acceptance_criteria':['ok']}; ordinary.pop('exact_output')
        self.assertIn('return a concise structured result',execution._prompt(ordinary,[]))

    def test_T7_duplicate_rendering_removed_without_altering_context_identity(self):
        context = execution._build_node_context(self.node,[],self.root)
        before = copy.deepcopy(context)
        prompt = execution._prompt(self.node,[],context_pack=context['pack'],manifest=context['manifest'])
        self.assertEqual(1,prompt.count(self.node['objective_summary']))
        self.assertNotIn('[OBJECTIVE_SUMMARY',prompt)
        self.assertNotIn('[ACCEPTANCE_CRITERIA',prompt)
        self.assertEqual(before,context)
        self.assertEqual(result_cache.context_identity(before['manifest']),
                         result_cache.context_identity(context['manifest']))
        context['pack']['items'].append({'source_kind':'OBJECTIVE_SUMMARY',
            'source_id':'different','content':'distinct required context'})
        self.assertIn('distinct required context',execution._prompt(self.node,[],context_pack=context['pack']))

    def test_T7_required_knowledge_still_delivered(self):
        worker=self.worker()
        report=self.run_work(worker,knowledge_provider=lambda node:document())
        self.assertEqual('SUCCESS',report['nodes']['A1']['status'])
        self.assertTrue(report['nodes']['A1']['master_b_identity']['knowledge_identity'])
        manifest=report['context_manifests']['A1']
        self.assertTrue(any(i['source_kind']=='knowledge_contract' for i in manifest['items']))
        self.assertIn('architecture v1',worker.calls[0]['prompt'])

    def test_required_knowledge_change_still_invalidates_reuse(self):
        current=[document()]
        worker=self.worker()
        self.run_work(worker,cache=True,knowledge_provider=lambda node:current[0])
        current[0]=document(contents={'project.architecture.overview':{'content':'architecture v2'}})
        self.run_work(worker,cache=True,knowledge_provider=lambda node:current[0])
        self.assertEqual(2,len(worker.calls))
        self.assertIn('architecture v2',worker.calls[1]['prompt'])

    def test_T8_failed_or_unverified_cannot_be_reused(self):
        for response,complete in [('TOKEN\nfooter',True),('TOKEN',False)]:
            worker=self.worker(response,complete)
            self.run_work(worker,cache=True)
            second=self.run_work(worker,cache=True)
            self.assertNotEqual('CACHE',second['nodes']['A1'].get('execution_source'))
            self.assertEqual(2,len(worker.calls))
        self.assertEqual(0,self.store().connection.execute('SELECT COUNT(*) FROM accepted_work').fetchone()[0])

    def test_literal_expectation_and_whitespace_change_all_reuse_identities(self):
        worker=self.worker('one  two');self.node['exact_output']='one  two'
        first=self.run_work(worker,cache=True)
        old=copy.deepcopy(self.plan)
        self.node['exact_output']='one two'
        worker=self.worker('one two')
        second=self.run_work(worker,cache=True)
        self.assertNotEqual(execution._plan_fingerprint(old),execution._plan_fingerprint(self.plan))
        self.assertNotEqual(first['nodes']['A1']['master_b_identity'],second['nodes']['A1']['master_b_identity'])
        self.assertNotEqual(first['nodes']['A1']['result_fingerprint'],second['nodes']['A1']['result_fingerprint'])

    def test_T9_model_effort_provenance_unchanged(self):
        self.node['execution_requirement']=requirement()
        worker=Adapter('codex','codex','gpt-6-astra',['medium'])
        worker.execute=lambda request:{'status':'SUCCESS','completion_signal':'agent_reported',
            'response_text':'TOKEN','response_complete':True,'acceptance_results':{}}
        report=self.run_work(worker,backends={'codex':worker})
        result=report['nodes']['A1']
        self.assertEqual(('Astra','medium','gpt-6-astra','medium'),tuple(result[k] for k in
            ('requested_model','requested_effort','resolved_model','resolved_effort')))
        self.assertIsNone(result['actual_model']);self.assertIsNone(result['actual_effort'])

    def test_T10_tool_only_stays_zero_ai(self):
        self.node.pop('exact_output')
        self.node.update(executor='TOOL_ONLY',routing=None,acceptance_criteria=[],
                         tool={'operation':'repository_state','args':{}})
        report=self.run_work(self.worker(),controller_ai_calls=0)
        self.assertEqual(0,report['calls_used'])
        self.assertEqual([],report['attempts'])

    def test_invalid_literal_contract_refuses_before_launch(self):
        for value in [True,123,{'expected':'TOKEN'}]:
            self.node['exact_output']=value
            worker=self.worker()
            with self.assertRaisesRegex(execution.ExecutionError,'EXACT_OUTPUT_CONTRACT_INVALID'):
                self.run_work(worker)
            self.assertEqual([],worker.calls)

    def test_missing_expected_literal_blocks_without_launch(self):
        self.node.pop('exact_output')
        worker=self.worker()
        report=self.run_work(worker)
        self.assertEqual('EXACT_OUTPUT_EXPECTED_MISSING',report['error'])
        self.assertEqual([],worker.calls)

    def test_explicit_empty_and_newline_literals_are_not_normalized(self):
        for value in ['', 'TOKEN\n', ' TOKEN ']:
            self.node['exact_output']=value
            self.assertEqual('SUCCESS',self.run_work(self.worker(value))['nodes']['A1']['status'])


if __name__=='__main__':
    unittest.main()
