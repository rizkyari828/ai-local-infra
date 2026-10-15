"""Master D Phase A: real project files, frozen selector, no providers."""
import copy
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path[:0] = [str(pathlib.Path(__file__).resolve().parents[1] / 'scripts/common'),
                str(pathlib.Path(__file__).resolve().parent)]
import authority
import knowledge_contract as kc
import knowledge_runtime as kr
from test_runtime_authority_identity import _repo


def source(key='a', **changes):
    return dict(key=key, owner='project', kind='architecture', locator='references/'+key+'.md',
                authority='AUTHORITATIVE', review='REVIEWED', lifecycle='ACTIVE', **changes)


def requirement(key='a', **changes):
    return dict(key=key, required=True, min_authority='AUTHORITATIVE') | changes


class CoreFixture(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base=pathlib.Path(self.tmp.name); self.root=_repo(self.base/'project')
        (self.root/'references').mkdir()
        self.registry=self.root/kr.REGISTRY; self.registry.parent.mkdir()
        self.doc={'contract_version':kc.CONTRACT_VERSION,
                  'repositories':{'project':authority.repository_identity(self.root)['id']},
                  'sources':[source()], 'requirements':[requirement()], 'contents':{}}
        self.write('a','architecture facts')
        self.save()

    def write(self,key,text):
        (self.root/'references'/ (key+'.md')).write_text(text)

    def save(self): self.registry.write_text(json.dumps(self.doc))
    def resolve(self,**node):return kr.resolve(self.root,node)
    def refuses(self,code='REQUIRED_CONTEXT_UNAVAILABLE',**node):
        with self.assertRaisesRegex(kr.KnowledgeRuntimeError,code):self.resolve(**node)


class CoreTests(CoreFixture):
    def test_K1_K14_K32_deterministic_exact_delivery_and_contract_identity(self):
        first=self.resolve(); second=self.resolve()
        self.assertEqual(first,second)
        self.assertEqual(kc.select(first['document']),first['selection'])
        self.assertEqual('architecture facts',first['bundle']['items'][0]['content'])
        self.assertEqual(first['selection']['delivered_identity'],first['bundle']['delivered_identity'])
        self.assertEqual('READY',first['bundle']['outcome'])

    def test_K2_missing_optional_is_omitted(self):
        self.doc['requirements'][0]['required']=False;self.save()
        (self.root/'references/a.md').unlink()
        bundle=self.resolve()['bundle']
        self.assertEqual([],bundle['items']);self.assertEqual('missing_content',bundle['omissions'][0]['reason'])
        self.assertEqual('MISSING_FILE',bundle['source_disposition'][0]['reason'])

    def test_K3_missing_required_file_and_missing_registry(self):
        (self.root/'references/a.md').unlink();self.refuses()
        self.registry.unlink();self.refuses(knowledge_requirements=[requirement()])
        self.assertIsNone(self.resolve())

    def test_K4_K5_revocation_supersession_and_unreviewed_fail_closed(self):
        for change in [{'lifecycle':'REVOKED'}, {'lifecycle':'SUPERSEDED','superseded_by':'b'}, {'review':'UNREVIEWED'}]:
            self.doc['sources']=[source() | change];self.save();self.refuses()

    def test_K6_complete_dependency_closure(self):
        self.doc['sources']=[source(depends_on=['b']),source('b',depends_on=['c']),source('c')]
        self.doc['requirements']=[requirement(k) for k in ['a','b','c']]
        self.write('b','dependent facts');self.write('c','leaf facts');self.save()
        b=self.resolve()['bundle'];self.assertEqual(['a','b','c'],[i['key'] for i in b['items']])
        self.assertEqual(2,len(b['provenance'][0]['dependency_fingerprints']))
        (self.root/'references/c.md').unlink();self.refuses()

    def test_K7_cycles_rejected(self):
        self.doc['sources']=[source(depends_on=['b']),source('b',depends_on=['a'])]
        self.doc['requirements']=[requirement(),requirement('b')];self.write('b','facts');self.save();self.refuses()

    def test_dependency_existence_does_not_invent_selection_obligation(self):
        self.doc['sources']=[source(depends_on=['b']),source('b')];self.write('b','facts');self.save();self.refuses()

    def test_K8_foreign_owner_and_unknown_permission_are_invalid(self):
        self.doc['sources'][0]['owner']='machine_local';self.save();self.refuses('KNOWLEDGE_OWNER_INVALID')
        self.doc['sources']=[source() | {'permissions':{'allow':True}}];self.save();self.refuses('KNOWLEDGE_METADATA_INVALID')

    def test_K9_same_basename_repositories_have_separate_source_identity(self):
        first=self.resolve()['bundle']
        other=_repo(self.base/'B'/'project');(other/'references').mkdir();(other/'references/a.md').write_text('architecture facts')
        registry=other/kr.REGISTRY;registry.parent.mkdir();registry.write_text(self.registry.read_text())
        with self.assertRaisesRegex(kr.KnowledgeRuntimeError,'KNOWLEDGE_REPOSITORY_MISMATCH'):kr.resolve(other)
        doc=copy.deepcopy(self.doc);doc['repositories']['project']=authority.repository_identity(other)['id'];registry.write_text(json.dumps(doc))
        self.assertNotEqual(first['items'][0]['source_identity'],kr.resolve(other)['bundle']['items'][0]['source_identity'])

    def test_K10_traversal_and_absolute_locators_refused(self):
        for locator in ['../secret','/tmp/secret',r'references\a.md']:
            self.doc['sources'][0]['locator']=locator;self.save()
            with self.assertRaises(kr.KnowledgeRuntimeError):self.resolve()

    def test_K10_symlink_file_directory_and_registry_refused(self):
        file=self.root/'references/a.md';file.unlink();file.symlink_to(self.root/'f.txt');self.refuses()
        file.unlink();(self.root/'references').rmdir();(self.root/'references').symlink_to(self.base)
        self.refuses()
        self.registry.unlink();self.registry.symlink_to(self.root/'f.txt');self.refuses('KNOWLEDGE_PATH_UNSAFE')

    def test_forbidden_secret_and_session_paths_are_not_read(self):
        for locator in ['.env','.aws/credentials','.claude/sessions/trace.json','references/private.key']:
            self.doc['sources'][0]['locator']=locator;self.save();self.refuses()

    def test_K11_K12_sensitive_source_is_withheld_not_weakly_redacted(self):
        self.write('a','public fact\napi_key=sk-'+'a'*40)
        self.refuses()
        self.doc['requirements'][0]['required']=False;self.save();b=self.resolve()['bundle']
        self.assertEqual([],b['items']);self.assertNotIn('sk-',kr.stable(b))
        self.assertEqual('REDACTION_SUFFICIENCY_UNPROVEN',b['source_disposition'][0]['reason'])

    def test_K13_bytes_chars_source_size_and_source_count_are_bounded(self):
        self.refuses('KNOWLEDGE_BUDGET_INSUFFICIENT',knowledge_limits={'max_bundle_chars':10})
        self.write('a','é'*512)
        self.refuses('KNOWLEDGE_BUDGET_INSUFFICIENT',knowledge_limits={'max_bundle_bytes':1000})
        self.refuses(knowledge_limits={'max_source_bytes':10})
        self.doc['sources'].append(source('b'));self.doc['requirements'].append(requirement('b'));self.write('b','b');self.save()
        self.refuses('KNOWLEDGE_BUDGET_INSUFFICIENT',knowledge_limits={'max_sources':1})

    def test_optional_budget_omission_preserves_required_dependency(self):
        self.doc['sources']=[source(depends_on=['b']),source('b'),source('c')]
        self.doc['requirements']=[requirement(),requirement('b',required=False),requirement('c',required=False)]
        self.write('b','dependency');self.write('c','unneeded');self.save()
        b=self.resolve(knowledge_limits={'max_sources':2})['bundle']
        self.assertEqual(['a','b'],[i['key'] for i in b['items']])
        self.assertEqual({'key':'c','reason':'CONTEXT_BOUND'},b['source_disposition'][-1])
        self.refuses('KNOWLEDGE_BUDGET_INSUFFICIENT',knowledge_limits={'max_sources':1})

    def test_K15_K18_relevant_change_only_and_line_ending_normalization(self):
        first=self.resolve()['bundle'];self.write('unregistered','unrelated')
        self.assertEqual(first,self.resolve()['bundle'])
        self.write('a','changed facts');self.assertNotEqual(first['delivered_identity'],self.resolve()['bundle']['delivered_identity'])
        self.write('a','line\r\n');one=self.resolve()['bundle'];self.write('a','line\n');self.assertEqual(one,self.resolve()['bundle'])

    def test_requirement_scope_identity_and_reason_parity(self):
        first=self.resolve()['bundle'];self.doc['requirements'][0]['reason']='different explanation';self.save()
        self.assertEqual(first['bundle_fingerprint'],self.resolve()['bundle']['bundle_fingerprint'])
        self.doc['requirements'][0]['scope']='api:other';self.save()
        self.assertNotEqual(first['delivered_identity'],self.resolve()['bundle']['delivered_identity'])

    def test_requirement_and_source_applicability_are_distinct(self):
        self.doc['requirements'][0]['applicability']={'languages':['rust']};self.save()
        self.assertEqual([],self.resolve(knowledge_context={'language':'python'})['bundle']['items'])
        self.doc['requirements'][0].pop('applicability');self.doc['sources'][0]['applicability']={'languages':['rust']};self.save()
        self.refuses(knowledge_context={'language':'python'})

    def test_duplicate_keys_source_identities_and_malformed_metadata(self):
        self.doc['sources'].append(source());self.save();self.refuses('KNOWLEDGE_METADATA_INVALID')
        self.doc['sources']=[source(),source('b') | {'locator':'references/a.md'}];self.save();self.refuses('KNOWLEDGE_SOURCE_AMBIGUOUS')
        self.registry.write_text('{"contract_version":1,"contract_version":2}')
        self.refuses('KNOWLEDGE_DUPLICATE_JSON_KEY')
        for malformed in ['[]','{bad','{"sources":[null]}']:
            self.registry.write_text(malformed)
            with self.assertRaises(kr.KnowledgeRuntimeError):self.resolve()

    def test_inline_contents_cannot_override_registered_file(self):
        self.doc['contents']={'a':{'content':'invented'}};self.save();self.refuses('KNOWLEDGE_METADATA_INVALID')

    def test_evidence_has_no_source_body_or_local_absolute_path(self):
        snap=self.resolve();evidence=kr.evidence(snap)
        self.assertNotIn('architecture facts',kr.stable(evidence));self.assertNotIn(str(self.root),kr.stable(evidence))
        self.assertEqual(snap['bundle']['bundle_fingerprint'],evidence['bundle_fingerprint'])

    def test_task_requirements_cannot_weaken_registry_and_bounds_cannot_expand(self):
        self.refuses('KNOWLEDGE_REQUIREMENT_CONFLICT',knowledge_requirements=[requirement(required=False)])
        for limits in [{'max_sources':False},{'max_sources':100000},{'unknown':1}]:
            self.refuses('KNOWLEDGE_LIMIT_INVALID',knowledge_limits=limits)

    def test_missing_registry_does_not_hide_invalid_task_metadata(self):
        self.registry.unlink()
        self.refuses('KNOWLEDGE_REQUIREMENT_INVALID',knowledge_requirements={})
        self.refuses('KNOWLEDGE_REQUIREMENT_INVALID',knowledge_requirements=[requirement(),requirement()])

    def test_prose_parity_survives_persisted_evidence(self):
        first=kr.evidence(self.resolve())
        self.doc['requirements'][0]['reason']='another agent description';self.save()
        self.assertEqual(first,kr.evidence(self.resolve()))

    def test_revoked_and_inapplicable_sources_are_not_opened(self):
        self.doc['sources'][0]['lifecycle']='REVOKED';self.doc['requirements'][0]['required']=False;self.save()
        original=kr._read;seen=[]
        def read(root,locator,bound):seen.append(locator);return original(root,locator,bound)
        with patch.object(kr,'_read',read):self.resolve()
        self.assertEqual([kr.REGISTRY],seen)


if __name__=='__main__':unittest.main()
