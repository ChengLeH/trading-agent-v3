"""Generic precision and semantic-context contracts, no model judgments mocked as proof."""
import unittest
from copy import deepcopy
from v3.facts import Fact, FactStore
from .bindings import validate_claim_bindings
from .test_integration import seeded, claim


class PrecisionContracts(unittest.TestCase):
    def test_direct_driver_saves_small_review_siblings_before_budget_block(self):
        from .test_integration import CloudDouble
        r=seeded()
        r.run.claims[0]['text']='large synthesis ' * 10000
        client=CloudDouble()
        outcome=r.execute_phase6(client,client,client,client)
        self.assertEqual(outcome['status'],'blocked:review_context_budget',outcome)
        self.assertTrue(r.receipts)
        self.assertNotIn('a',r.receipts)
        self.assertIn('a',[row['claim_id'] for row in outcome['oversized_items']])

    def test_canonical_renderer_cannot_certify_a_report_without_global_receipt(self):
        from .reporting import render_report
        from .test_integration import review_all, global_verdict
        r=seeded()
        rendered=render_report(r.orch.snapshot(r.run_id))
        self.assertFalse(rendered['sidecar']['verification']['delivery_ready'])
        self.assertTrue(all(c['text'] in rendered['report_markdown'] for c in r.run.claims))
        review_all(r)
        with self.assertRaises(ValueError): r.finalize()
        r.prepare_phase6_global_sweep()
        r.collect_phase6_global_sweep(global_verdict(r), model_identity='host/global')
        final=r.finalize()
        self.assertEqual(final['sidecar']['verification']['status'],'complete')
        self.assertTrue(final['sidecar']['verification']['global_review_complete'])

    def test_arithmetic_precision_is_explicit_not_a_global_absolute_allowance(self):
        from v3.facts import CalcStore
        from .pipeline import calculation_findings
        from types import SimpleNamespace
        store=FactStore()
        for fid,value,period in [('current',123.456,'FY2030'),('prior',100,'FY2029')]:
            store.add(Fact(fid,value,'USD',period,concept='Revenue',entity='TEST',
                           basis='gaap',scope='consolidated',share_basis='not_applicable',
                           source_id='fixture'))
        def check(value, meta=None):
            calcs=CalcStore();calcs.add('growth',value,'growth',['current','prior'],'RATIO',meta)
            return calculation_findings(SimpleNamespace(fact_store=store,calc_store=calcs))
        self.assertTrue(any(r['status']=='FAIL' for r in check(.23460)))
        self.assertFalse(check(.23456))
        self.assertFalse(check(.23,dict(output_rounding=dict(decimal_places=2,rounding='half_up'))))
        self.assertTrue(any(r['status']=='FAIL' for r in check(.24,dict(output_rounding=dict(decimal_places=2,rounding='half_up')))))
        self.assertTrue(any(r['status']=='FAIL' for r in check(.24,dict(tolerance=100))))

    def fixture(self, value=1234560, unit='USD'):
        store = FactStore()
        store.add(Fact('source.amount', value, unit, 'FY2025', concept='Revenue',
                       basis='gaap', scope='consolidated', source_id='fixture'))
        c = claim('arbitrary.claim')
        c.update(refs=['source.amount'],fact_refs=['source.amount'],
                 bindings=[dict(ref_id='source.amount',entity='unknown',concept='Revenue',
                     period='2025-FY',scope='Consolidated',unit=unit,basis='GAAP')],
                 assertion=dict(kind='value',ref_id='source.amount',value=value))
        return store,c

    def test_typed_equivalence_and_explicit_display_preserve_raw_source(self):
        store,c = self.fixture()
        before = deepcopy(store.to_dict())
        c['assertion']['presentation'] = dict(value=1.23, unit='USD million', decimal_places=2, rounding='half_up')
        rows = validate_claim_bindings([c],store,require_bindings=True)
        self.assertFalse([r for r in rows if r.status=='FAIL'], rows)
        self.assertEqual(store.to_dict(),before)
        c['assertion']['presentation']['value'] = 1.24
        self.assertTrue(any(r.status=='FAIL' and r.dimension=='presentation' for r in
                            validate_claim_bindings([c],store,require_bindings=True)))

    def test_near_numbers_wrong_unit_period_and_basis_are_not_rounding(self):
        for dimension,value in [('period','FY2024'),('unit','EUR'),('basis','non_gaap')]:
            store,c=self.fixture()
            c['bindings'][0][dimension]=value
            self.assertTrue(any(r.status=='FAIL' for r in validate_claim_bindings([c],store,require_bindings=True)))
        store,c=self.fixture(value=100)
        c['assertion']['value']=100.00001
        self.assertTrue(any(r.status=='FAIL' and r.dimension=='value' for r in validate_claim_bindings([c],store)))

    def test_unknown_alias_is_semantic_uncertainty_not_string_similarity(self):
        store,c=self.fixture()
        c['bindings'][0]['concept']='Revenue adjusted'
        rows=validate_claim_bindings([c],store,require_bindings=True)
        self.assertTrue(any(r.status=='WARN' and r.dimension=='concept' for r in rows))
        runner=seeded()
        with self.assertRaisesRegex(ValueError,'semantic verification'):
            runner._require_atomic()

    def test_amount_scale_and_identifier_changes_do_not_change_outcome(self):
        for scale in [1,1000,1000000]:
            store,c=self.fixture(value=123.456*scale)
            c['claim_id']='fictional.'+str(scale)
            c['assertion']['presentation']=dict(value=123.46, unit='USD', decimal_places=2,rounding='half_up')
            # Change both source and target scale explicitly, rather than forgive a bad number.
            c['assertion']['presentation']['unit']={1:'USD',1000:'USD thousand',1000000:'USD million'}[scale]
            self.assertFalse([r for r in validate_claim_bindings([c],store) if r.status=='FAIL'])


class EvidenceRecoveryContracts(unittest.TestCase):
    def manifest(self, root, *, source_id='filing.new', subject=None):
        import json
        from pathlib import Path
        path=Path(root)/'filing.txt'
        path.write_text('Issuer disclosed financing from available cash and committed debt. No forecast is provided.')
        manifest=Path(root)/'extra.json'
        manifest.write_text(json.dumps(dict(subject=subject or {'ticker':'TEST'},as_of='2026-10-16',
            sources=[dict(id=source_id,type='generic_text',path='filing.txt')])) )
        return manifest

    def test_new_evidence_preserves_findings_reopens_reviews_then_patches_and_global(self):
        import tempfile
        from .test_agent_reviews import NativeReviews, response
        from .evidence_repair import append_sources, prepare_evidence_patch
        with tempfile.TemporaryDirectory() as root:
            workflow=NativeReviews().bootstrap(root)
            NativeReviews()._reach_global(workflow)
            with workflow.queue.transaction() as state:
                r=workflow._load(state)
                finding=dict(finding_id='gap.1',claim_ids=['a'],resolution_route='evidence_needed',
                    issue='No financing source supplied',evidence='fixture',suggested_fix='add filing',
                    resolution_reason='missing document',resolution_baseline=r._resolution_baseline())
                r.deferred_global_findings=[finding]
                workflow._save(state,r)
                before_hash=r._hash(); old_claims=deepcopy(r.run.claims); rounds=r.revision_round
            added=append_sources(workflow,self.manifest(root),reason='Main Agent supplied filing')
            self.assertFalse(added['findings_resolved'])
            self.assertNotEqual(added['after_hash'],before_hash)
            with workflow.queue.transaction() as state:
                r=workflow._load(state)
                self.assertEqual(r.run.claims,old_claims)
                self.assertEqual(r.revision_round,rounds)
                self.assertEqual(r.deferred_global_findings[0]['finding_id'],'gap.1')
                self.assertFalse(r.receipts)
                self.assertIn('No forecast is provided.',next(iter(r.run.evidence_store.to_dict().values()))['claim_candidate'])
            with self.assertRaisesRegex(ValueError,'semantic verification'):
                prepare_evidence_patch(workflow,['a'],reason='Reassess financing')
            # Complete mandatory verification on the changed evidence scope.
            for _ in range(10):
                packet=workflow.next()
                if packet.get('kind') not in {'atomic','deep'}:
                    break
                workflow.submit(packet['packet_id'],{cid:response(cid,item) for cid,item in packet['items'].items()},
                    'host/test',packet['input_hash'])
            outcome=prepare_evidence_patch(workflow,['a'],reason='Link new financing evidence; preserve uncertainty')
            self.assertIn('revision_prompts',outcome)
            self.assertIn('committed debt',outcome['revision_prompts'][0][1])
            patch=workflow.next();self.assertEqual(patch.get('kind'),'patch',patch)
            ref=added['canonical_refs'][0]
            revised=dict(claim_id='a',text='Financing source supplied; forecast remains unavailable',
                refs=[ref],bindings=[dict(ref_id=ref,entity='TEST',concept='financing',period='unknown',
                    scope='unknown',unit='unknown',basis='unknown')])
            workflow.submit_patch(patch['packet_id'],[revised],'host/test',patch['input_hash'])
            pending=workflow.next();self.assertEqual(pending['kind'],'atomic')
            self.assertTrue({'a','b','c'}.intersection(pending['items']))
            with workflow.queue.transaction() as state:
                r=workflow._load(state)
                self.assertEqual(r.deferred_global_findings[0]['finding_id'],'gap.1')
                self.assertIsNone(r.global_receipt)

    def test_bad_subject_duplicate_source_and_empty_document_leave_state_unchanged(self):
        import tempfile
        from .test_agent_reviews import NativeReviews
        from .evidence_repair import append_sources
        from .schema import digest
        with tempfile.TemporaryDirectory() as root:
            workflow=NativeReviews().bootstrap(root)
            before=workflow.queue.state_path.read_bytes()
            with self.assertRaisesRegex(ValueError,'subject'):
                append_sources(workflow,self.manifest(root,subject={'ticker':'OTHER'}),reason='test')
            self.assertEqual(workflow.queue.state_path.read_bytes(),before)
            manifest=self.manifest(root)
            append_sources(workflow,manifest,reason='test')
            before=workflow.queue.state_path.read_bytes()
            with self.assertRaisesRegex(ValueError,'distinct new source'):
                append_sources(workflow,manifest,reason='test')
            self.assertEqual(workflow.queue.state_path.read_bytes(),before)


class SemanticContextContracts(unittest.TestCase):
    def test_financial_obligations_reach_roles_atomic_deep_patch_and_global(self):
        from .runner import ROLE_CONTRACT
        from .financial_review import FINANCIAL_REVIEW_CONTRACT, FINANCIAL_REVIEW_VERSION
        from .atomic_phase6 import build_atomic_prompt, build_reference_supplement_prompt, build_reference_verdict_repair_prompt
        from .test_integration import review_all, verdict
        r=seeded(); tasks=r._tasks(); task=tasks['a']
        self.assertIn(FINANCIAL_REVIEW_CONTRACT,ROLE_CONTRACT)
        self.assertIn(FINANCIAL_REVIEW_CONTRACT,build_atomic_prompt(task))
        self.assertEqual(task.context['financial_review_contract'],FINANCIAL_REVIEW_VERSION)
        self.assertIn(FINANCIAL_REVIEW_CONTRACT,build_reference_supplement_prompt(tasks['b'],
            dict(reference_checks=[],verdict='PASS',findings=[]),['a']))
        self.assertIn(FINANCIAL_REVIEW_CONTRACT,build_reference_verdict_repair_prompt(tasks['b'],[],validation_error='fixture'))
        review_all(r)
        r.receipts['a']['result']=verdict('a','FAIL')
        self.assertIn(FINANCIAL_REVIEW_CONTRACT,r._deep_prompt(task))
        repair=r.prepare_phase6_revision_v4([dict(claim_id='a',issue='fixture',evidence='fixture',suggested_fix='fix')])
        self.assertIn(FINANCIAL_REVIEW_CONTRACT,repair['revision_prompts'][0][1])
        r.pending_patch=None;r.receipts['a']['result']=verdict('a')
        global_prompt=r.prepare_phase6_global_sweep()[1]
        self.assertIn(FINANCIAL_REVIEW_CONTRACT,global_prompt)

    def test_contract_upgrade_invalidates_atomic_and_global_receipts(self):
        from unittest.mock import patch
        from .test_integration import review_all
        r=seeded();review_all(r)
        old=r._hash();r.global_receipt=dict(input_hash=old)
        with patch('v4.runner.FINANCIAL_REVIEW_VERSION','future-contract'), \
             patch('v4.atomic_phase6.FINANCIAL_REVIEW_VERSION','future-contract'):
            self.assertNotEqual(old,r._hash())
            r._tasks()
            self.assertFalse(r.receipts)
            self.assertIsNone(r.global_receipt)


class FreshFinancialIntegration(unittest.TestCase):
    def test_native_source_declaration_repairs_identity_without_rewriting_raw_source(self):
        import tempfile
        from .test_agent_reviews import NativeReviews
        from .evidence_repair import declare_financial_source
        from v3.sources import SourceRecord
        from .financial_statements import augment_financial_statements, financial_statement_findings
        with tempfile.TemporaryDirectory() as root:
            w=NativeReviews().bootstrap(root)
            payload=dict(income=[],balance=[dict(fiscal_year=2030,parent_equity=70,
                consolidated_equity=80,nonredeemable_nci=10,redeemable_nci=0)],
                financial_statement_metadata=dict(unit='USD',basis='consolidated',entity='Example Inc.',redeemable_nci_inclusion='separate'))
            with w.queue.transaction() as state:
                r=w._load(state)
                rec=SourceRecord('filing','openbb_fundamentals','TEST',None,payload,'original-hash')
                r.run.records.append(rec)
                for concept,value in [('Assets',100),('Liabilities',20)]:
                    r.run.fact_store.add(Fact(concept,value,'USD','FY2030',concept=concept,basis='gaap',
                        source_id='filing',source_hash='original-hash'))
                augment_financial_statements(r.run,dict(facts=0,limitations=[]))
                w._save(state,r);before=state['input_hash']
            harness=NativeReviews()
            packet=harness._reach_global(w)
            w.submit(packet['packet_id'],{next(iter(packet['items'])):harness._global_response(w)},
                'host/global',packet['input_hash'])
            self.assertEqual(w.next()['status'],'complete')
            result=declare_financial_source(w,'filing',dict(basis='gaap'),reason='operator verified accounting framework')
            self.assertNotEqual(result['after_hash'],before)
            with w.queue.transaction() as state:
                r=w._load(state)
                self.assertEqual(r.run.records[0].payload,payload)
                self.assertEqual(r.run.records[0].source_hash,'original-hash')
                rows=financial_statement_findings(r.run)
                self.assertTrue(any(x['check_id']=='balance_sheet_identity' and x['status']=='PASS' for x in rows),rows)
                self.assertFalse(r.receipts)
                self.assertIsNone(r.global_receipt)
                self.assertIn('source_declarations',r._tasks()['a'].context)
            self.assertEqual(w.next()['kind'],'atomic')
            with self.assertRaises(ValueError):
                declare_financial_source(w,'filing',dict(basis='consolidated'),reason='bad declaration')

    def test_new_fields_survive_real_adapter_and_reach_validation_and_packets(self):
        import json
        import tempfile
        from pathlib import Path
        from .pipeline import OrchestratorV4
        with tempfile.TemporaryDirectory() as root:
            source=Path(root)/'financial.json'
            source.write_text(json.dumps(dict(fundamentals=dict(income=[],balance=[dict(fiscal_year=2030,
                total_assets=120,total_liabilities=60,parent_equity=50,nonredeemable_nci=8,redeemable_nci=2)],
                financial_statement_metadata=dict(unit='USD',basis='gaap',entity='TEST',redeemable_nci_inclusion='separate')))))
            orch=OrchestratorV4()
            rid=orch.start_run({'ticker':'TEST'},dict(subject={'ticker':'TEST'},as_of='2031-01-01',
                sources=[dict(id='statement',type='openbb_fundamentals',path=str(source))]))
            orch.ingest(rid);orch.build_facts(rid)
            findings,blocked=orch.validate_stage(rid,'ingest')
            self.assertFalse(blocked,findings)
            identity=[f for f in findings if f.get('check_id')=='balance_sheet_identity']
            self.assertEqual(len(identity),1);self.assertEqual(identity[0]['status'],'PASS')
            packet=orch.get_packet(rid,'fundamentals_analyst')
            self.assertTrue(any(d.get('section')=='financial_statements' for d in packet['ingestion_diagnostics']))
            self.assertIn('ParentEquity',{f['concept'] for f in packet['facts'].values()})
            # Financial disagreement remains a deterministic blocker, even without claims.
            raw=json.loads(source.read_text());raw['fundamentals']['balance'][0]['total_assets']=121
            source.write_text(json.dumps(raw))
            other=OrchestratorV4();rid2=other.start_run({'ticker':'TEST'},orch.runs[rid].manifest)
            other.ingest(rid2);other.build_facts(rid2)
            rows,blocked=other.validate_stage(rid2,'ingest')
            self.assertTrue(blocked)
            self.assertTrue(any(f.get('check_id')=='balance_sheet_identity' and f['status']=='FAIL' for f in rows))

    def test_cli_recovery_entry_reports_action_without_starting_models(self):
        import tempfile
        from .test_agent_reviews import NativeReviews
        from .test_agent_cli import run
        with tempfile.TemporaryDirectory() as root:
            workflow=NativeReviews().bootstrap(root)
            manifest=EvidenceRecoveryContracts().manifest(root)
            rc,payload=run(['add-evidence','--work-dir',str(workflow.directory), '--manifest',str(manifest),
                '--reason','Main Agent supplies source'])
            self.assertEqual(rc,0,payload)
            self.assertEqual(payload['status'],'evidence_added')
            rc,payload=run(['restore-secondary','--work-dir',str(workflow.directory),
                '--identity','antigravity/fake-reviewer','--reason','operator checked'])
            self.assertEqual(rc,0,payload)
            self.assertEqual(payload['status'],'secondary_not_disabled')


class RecoveryBoundaryProofs(unittest.TestCase):
    def test_empty_document_and_adapter_error_leave_canonical_state_unchanged(self):
        import tempfile
        from pathlib import Path
        from .test_agent_reviews import NativeReviews
        from .evidence_repair import append_sources
        with tempfile.TemporaryDirectory() as root:
            workflow=NativeReviews().bootstrap(root)
            manifest=EvidenceRecoveryContracts().manifest(root)
            (Path(root)/'filing.txt').write_text('   ')
            before=workflow.queue.state_path.read_bytes()
            with self.assertRaisesRegex(ValueError,'ingestion failed'):
                append_sources(workflow,manifest,reason='empty document must not become evidence')
            self.assertEqual(workflow.queue.state_path.read_bytes(),before)

    def test_restore_really_redispatches_and_accepts_valid_reviews(self):
        import tempfile
        from .test_agent_dispatch import SecondaryDispatch,FakeSecondary
        from .agent_dispatch import dispatch_secondary_reviews,restore_secondary_availability
        with tempfile.TemporaryDirectory() as root:
            workflow=SecondaryDispatch().bootstrap(root)
            with workflow.queue.transaction() as state:
                state['secondary_availability']={FakeSecondary.identity:dict(status='unavailable',reason_code='quota_exhausted')}
            self.assertTrue(restore_secondary_availability(workflow,FakeSecondary.identity,reason='operator verified'))
            client=FakeSecondary()
            result=dispatch_secondary_reviews(workflow,client,max_workers=2)
            self.assertEqual(result['status'],'secondary_complete',result)
            self.assertEqual(len(client.calls),8)
            self.assertEqual(set(result['accepted_claim_ids']),set(client.calls))


class PersistedContractUpgrade(unittest.TestCase):
    def test_old_native_global_receipt_is_not_replayed_under_new_financial_contract(self):
        import tempfile
        from unittest.mock import patch
        from .test_agent_reviews import NativeReviews
        from .agent_reviews import NativeReviewWorkflow
        from .test_integration import global_verdict
        with tempfile.TemporaryDirectory() as root:
            with patch('v4.runner.FINANCIAL_REVIEW_VERSION','legacy-contract'), \
                 patch('v4.atomic_phase6.FINANCIAL_REVIEW_VERSION','legacy-contract'), \
                 patch('v4.agent_workflow.FINANCIAL_REVIEW_VERSION','legacy-contract'):
                workflow=NativeReviews().bootstrap(root)
                packet=NativeReviews()._reach_global(workflow)
                with workflow.queue.transaction() as state:
                    r=workflow._load(state); result=global_verdict(r)
                workflow.submit(packet['packet_id'],{next(iter(packet['items'])):result},'host/test',packet['input_hash'])
            # Every old atomic task hash changed, so native replay must schedule
            # current reviews before accepting any old global judgment.
            resumed=NativeReviewWorkflow(workflow.directory).next()
            self.assertEqual(resumed.get('kind'),'atomic',resumed)
            self.assertTrue(resumed['items'])


class ReviewRecoveryRegressions(unittest.TestCase):
    def test_restore_uses_new_durable_generation_and_keeps_old_quota_session(self):
        import tempfile
        import json
        from pathlib import Path
        from .test_cloud import cli_stub
        from .cloud import AntigravityCloudClient
        from .test_agent_dispatch import SecondaryDispatch
        from .agent_dispatch import dispatch_secondary_reviews,restore_secondary_availability
        from .test_integration import verdict
        with tempfile.TemporaryDirectory() as root:
            marker=Path(root)/'quota-reset'
            body='''
init()
sys.stdin.readline()
emit(dict(event="step_update", step_update=dict(step_type="user_input", state="DONE")))
if not Path(%r).exists():
    emit(dict(event="result", result=dict(status="ERROR", error="Individual quota reached")))
else:
    result(%r)
''' % (str(marker),json.dumps(verdict('a')))
            with cli_stub(body) as executable:
                workflow=SecondaryDispatch().bootstrap(root)
                client=AntigravityCloudClient('model',executable=executable,session_root=Path(root)/'sessions',
                    timeout=5,request_attempts=1)
                first=dispatch_secondary_reviews(workflow,client,max_workers=1,max_items=1)
                self.assertEqual(first['status'],'fallback_to_main')
                old_sessions=list((Path(root)/'sessions').glob('*/session.json'))
                self.assertEqual(len(old_sessions),1)
                old_content=old_sessions[0].read_bytes()
                marker.touch()
                restore_secondary_availability(workflow,client.identity,reason='operator confirmed quota reset')
                second=dispatch_secondary_reviews(workflow,client,max_workers=1,max_items=1)
                self.assertEqual(second['status'],'secondary_complete',second)
                self.assertEqual(second['accepted_claim_ids'],['a'])
                self.assertEqual(old_sessions[0].read_bytes(),old_content)

    def test_crash_after_quota_step_never_resumes_paid_continuation(self):
        import tempfile
        from unittest.mock import patch
        from .session_state import SessionState
        from .agy_session import load_agent_profile,run_session,AntigravityError
        with tempfile.TemporaryDirectory() as root:
            store=SessionState(root,'model','prompt',load_agent_profile()[1])
            with store.lock():
                store.save(active_attempt_id='attempt',request_sent=True,request_written=True,turn_acknowledged=True,
                    conversation_id='11111111-1111-4111-8111-111111111111')
                store.event(dict(event='step_update',step_update=dict(step_type='error_message',error_code='insufficient_quota')))
            with patch('v4.agy_session.subprocess.Popen') as spawn, self.assertRaises(AntigravityError) as caught:
                run_session('/unused/agy','model','prompt',5,session_dir=root,resume=True)
            self.assertEqual(caught.exception.code,'quota_exhausted')
            spawn.assert_not_called()


class RoleAndPendingUpgrade(unittest.TestCase):
    def old_contract(self):
        from contextlib import ExitStack
        from unittest.mock import patch
        stack=ExitStack()
        for module in ('runner','atomic_phase6','agent_workflow','checkpoint'):
            stack.enter_context(patch('v4.'+module+'.FINANCIAL_REVIEW_VERSION','legacy-contract'))
        return stack

    def test_partial_role_packet_retires_but_preserves_candidates_and_progress(self):
        import tempfile
        from .test_agent_roles import NativeRoles
        from .test_journal import author_response
        from .agent_workflow import NativeWorkflow
        with tempfile.TemporaryDirectory() as root:
            with self.old_contract():
                _,workflow=NativeRoles().bootstrap(root);old=workflow.next()
                role='market_analyst';answer=author_response(old['items'][role]['prompt'])
                workflow.submit(old['packet_id'],{role:answer},'host/test',old['input_hash'])
            new=NativeWorkflow(workflow.directory).next()
            self.assertEqual(new['kind'],'roles')
            self.assertNotEqual(new['packet_id'],old['packet_id'])
            self.assertIn(role,new['items'])
            with workflow.queue.transaction() as state:
                self.assertEqual(state['contract_upgrades'][-1]['role_candidates'][role]['claims'][0]['text'],answer['claims'][0]['text'])
                self.assertEqual(state['packets'][old['packet_id']]['received'][role]['response'],answer)
                self.assertTrue(state['packets'][old['packet_id']]['retired'])

    def test_pending_checkpoint_keeps_findings_and_rounds_but_requires_current_reviews(self):
        import tempfile
        from pathlib import Path
        from .test_integration import review_all
        from .checkpoint import save_checkpoint,load_checkpoint
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'pending.json'
            with self.old_contract():
                r=seeded();review_all(r);r.revision_round=2
                r.prepare_phase6_revision_v4([dict(claim_id='a',issue='unresolved premise',evidence='fixture',suggested_fix='correct')])
                save_checkpoint(r,path)
            resumed=load_checkpoint(path)
            self.assertEqual(resumed.revision_round,2)
            self.assertIsNone(resumed.pending_patch)
            self.assertTrue(any(f['issue']=='unresolved premise' for f in resumed.deferred_global_findings))
            self.assertIn('retired_pending_patch',resumed.contract_migrations[-1])
            self.assertFalse(resumed.receipts)

    def test_opaque_units_are_unresolved_and_provider_event_ids_cannot_mask_quota(self):
        from .agy_protocol import classify_provider_failure
        from .agy_session import AntigravityError
        store,c=PrecisionContracts().fixture()
        c['bindings'][0]['unit']='US dollars'
        rows=validate_claim_bindings([c],store,require_bindings=True)
        self.assertTrue(any(r.dimension=='unit' and r.status=='WARN' for r in rows))
        self.assertFalse(any(r.dimension=='unit' and r.status=='FAIL' for r in rows))
        for reference in ['.','--',':','abc-123','bad\nref']:
            code,details=classify_provider_failure(dict(error_code='insufficient_quota',event_id=reference))
            self.assertEqual(AntigravityError(code,**details).code,'quota_exhausted')


class GlobalRecoveryUpgrade(unittest.TestCase):
    def test_evidence_addition_clears_only_stale_contract_block_and_reaches_new_global(self):
        import tempfile
        from .test_agent_reviews import NativeReviews,response
        from .evidence_repair import append_sources
        with tempfile.TemporaryDirectory() as root:
            workflow=NativeReviews().bootstrap(root);packet=NativeReviews()._reach_global(workflow)
            result=workflow.submit(packet['packet_id'],{next(iter(packet['items'])):dict(findings='invalid')},'host/test',packet['input_hash'])
            self.assertEqual(result['status'],'blocked:global_contract')
            append_sources(workflow,EvidenceRecoveryContracts().manifest(root),reason='new source changes review input')
            for _ in range(8):
                fresh=workflow.next()
                if fresh.get('kind') not in {'atomic','deep'}:break
                workflow.submit(fresh['packet_id'],{cid:response(cid,item) for cid,item in fresh['items'].items()},'host/test',fresh['input_hash'])
            self.assertEqual(fresh.get('kind'),'global',fresh.get('reason'))
            self.assertNotEqual(fresh['packet_id'],packet['packet_id'])

    def test_old_partial_global_progress_is_audited_then_restarted_under_new_contract(self):
        import tempfile
        from .test_agent_reviews import NativeReviews
        from .test_integration import global_verdict
        from .checkpoint import load_checkpoint
        with tempfile.TemporaryDirectory() as root:
            with RoleAndPendingUpgrade().old_contract():
                workflow=NativeReviews().bootstrap(root);packet=NativeReviews()._reach_global(workflow)
                with workflow.queue.transaction() as state:
                    r=workflow._load(state);result=global_verdict(r)
                result['dependency_reviews']=result['dependency_reviews'][:1]
                workflow.submit(packet['packet_id'],{next(iter(packet['items'])):result},'host/test',packet['input_hash'])
            resumed=load_checkpoint(workflow.checkpoint_path)
            self.assertIsNone(resumed.global_review_progress)
            self.assertTrue(any('retired_global_progress' in row for row in resumed.contract_migrations))


class MandatoryOpaqueBindingReview(unittest.TestCase):
    def test_same_unresolved_claim_is_dispatched_and_cannot_reuse_sibling_receipts(self):
        from .test_integration import review_all
        r=seeded()
        store,c=PrecisionContracts().fixture()
        r.run.fact_store=store;review_all(r)
        c['bindings'][0]['concept']='Unfamiliar revenue description'
        r.run.claims.append(c)
        findings,blocked=r.orch.validate_stage(r.run_id,'phase6')
        self.assertFalse(blocked,findings)
        self.assertTrue(any(f.get('claim_id')==c['claim_id'] and f.get('status')=='WARN' for f in findings))
        packet=r.prepare_phase6_atomic()
        self.assertEqual(set(dict(packet['atomic_prompts'])),{c['claim_id']})
        self.assertEqual(set(r._tasks())-set(r.receipts),{c['claim_id']})
        with self.assertRaisesRegex(ValueError,'semantic verification'):
            r.prepare_phase6_global_sweep()
