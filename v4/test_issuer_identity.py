import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from v3.pipeline import RunState
from v3.sources import SourceRecord
from .pipeline import OrchestratorV4
from .issuer_identity import verify_issuer,IssuerIdentityError


class IssuerIdentityContracts(unittest.TestCase):
    def run_state(self,cik=123):
        return RunState(run_id='issuer',ticker='TEST',manifest={'subject':{'ticker':'TEST','cik':cik}},
          records=[SourceRecord('source','openbb_fundamentals','TEST',cik,{'income':[{'fiscal_year':2025,'total_revenue':10}]},'hash')])

    def registry(self):
        return {'0':{'ticker':'TEST','title':'Test Corporation','cik_str':123}}

    def test_wrong_cik_fails_even_if_all_self_declared_tickers_match(self):
        run=self.run_state(456)
        with patch('v4.issuer_identity.download_registry',return_value=json.dumps(self.registry()).encode()):
            findings=verify_issuer(run)
        self.assertEqual(findings[0]['status'],'FAIL')
        self.assertIn('SEC issuer mismatch',findings[0]['reason'])

    def test_verified_identity_survives_checkpoint_and_source_change_rechecks(self):
        from .checkpoint import load_checkpoint,save_checkpoint
        from .test_integration import seeded
        runner=seeded(); run=self.run_state();runner.run.manifest=run.manifest;runner.run.records=run.records
        runner.run.manifest['as_of']='2026-10-20'
        with tempfile.TemporaryDirectory() as d, patch('v4.issuer_identity.download_registry',return_value=json.dumps(self.registry()).encode()) as fetch:
            self.assertEqual(verify_issuer(runner.run)[0]['status'],'PASS')
            p=Path(d)/'checkpoint.json';save_checkpoint(runner,p)
            restored=load_checkpoint(p)
            self.assertEqual(verify_issuer(restored.run)[0]['status'],'PASS')
            self.assertEqual(fetch.call_count,1)
            restored.run.manifest['subject']['cik']=456
            self.assertEqual(verify_issuer(restored.run)[0]['status'],'FAIL')

    def test_offline_unavailable_is_explicit_failure_not_false_identity_pass(self):
        with patch('v4.issuer_identity.download_registry',side_effect=OSError('offline')):
            finding=verify_issuer(self.run_state())[0]
        self.assertEqual(finding['status'],'FAIL')
        self.assertIn('registry',finding['next_action'])

    def test_local_snapshot_update_and_removal_are_not_hidden_by_cached_proof(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'sec.json';p.write_text(json.dumps(self.registry()));run=self.run_state()
            run.manifest['issuer_registry_path']=str(p)
            self.assertEqual(verify_issuer(run)[0]['status'],'PASS')
            data=self.registry();data['0']['cik_str']=456;p.write_text(json.dumps(data))
            self.assertEqual(verify_issuer(run)[0]['status'],'FAIL')
            p.unlink();self.assertEqual(verify_issuer(run)[0]['status'],'FAIL')

    def test_declared_cik_is_verified_for_market_only_and_empty_sources(self):
        for empty in [True,False]:
            run=self.run_state(456)
            if empty: run.records=[]
            else: run.records[0].type='openbb_ohlcv'
            with patch('v4.issuer_identity.download_registry',return_value=json.dumps(self.registry()).encode()):
                self.assertEqual(verify_issuer(run)[0]['status'],'FAIL')

    def test_network_registry_cache_expires_at_day_boundary(self):
        from .issuer_identity import download_registry
        from unittest.mock import MagicMock
        download_registry.cache_clear()
        response=MagicMock();response.__enter__.return_value=response
        response.geturl.return_value='https://www.sec.gov/files/company_tickers.json'
        response.read.return_value=json.dumps(self.registry()).encode()
        with patch('v4.issuer_identity.urllib.request.urlopen',return_value=response) as fetch:
            download_registry('2026-10-20');download_registry('2026-10-20')
            download_registry('2026-10-21')
            self.assertEqual(fetch.call_count,2)
        download_registry.cache_clear()

    def test_native_fresh_manifest_resolves_offline_registry_and_preserves_proof(self):
        from .agent_workflow import NativeWorkflow
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/'sec.json').write_text(json.dumps(self.registry()))
            payload={'_metadata':{'ticker':'TEST'},'fundamentals':{'income':[
                {'fiscal_year':2025,'total_revenue':10}], 'cash_flow':[
                {'fiscal_year':2025,'operating_cash_flow':5,'capex':-1,'share_repurchases':-2}]}}
            (root/'fund.json').write_text(json.dumps(payload))
            manifest={'subject':{'ticker':'TEST','cik':123},'as_of':'2026-10-20',
                'issuer_registry_path':'sec.json','sources':[{'id':'financials','type':'openbb_fundamentals','path':'fund.json'}]}
            (root/'manifest.json').write_text(json.dumps(manifest))
            workflow=NativeWorkflow.from_manifest(root/'manifest.json',root/'work')
            packet=workflow.next()
            self.assertEqual(packet['kind'],'roles')
            with workflow.queue.transaction() as state:
                restored=workflow._load(state)
                self.assertEqual(restored.run.fact_store.get('fact.FreeCashFlow.FY2025').value,4)
                self.assertEqual(restored.run.records[0].meta['issuer_identity']['company_name'],'Test Corporation')
                self.assertTrue(restored.run.records[0].meta['cash_flow_extraction']['coverage'])
            bad=self.registry();bad['0']['cik_str']=456
            (root/'sec.json').write_text(json.dumps(bad))
            with self.assertRaises(IssuerIdentityError):NativeWorkflow(workflow.directory).next()

    def test_legacy_checkpoint_can_use_explicit_registry_environment_without_relabelling_origin(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'sec.json';p.write_text(json.dumps(self.registry()));run=self.run_state()
            with patch.dict(os.environ,{'V4_SEC_ISSUER_REGISTRY':str(p)}):
                result=verify_issuer(run)
                self.assertEqual(result[0]['registry_origin'],'operator_supplied_SEC_snapshot')
            with patch.dict(os.environ,{'V4_SEC_ISSUER_REGISTRY':''}), patch('v4.issuer_identity.download_registry',side_effect=OSError('offline')):
                self.assertEqual(verify_issuer(run)[0]['status'],'FAIL')

    def test_no_cik_is_not_certified_by_implicit_lookup(self):
        run=self.run_state(None)
        with patch('v4.issuer_identity.download_registry') as fetch:
            self.assertEqual(verify_issuer(run),[])
            fetch.assert_not_called()
