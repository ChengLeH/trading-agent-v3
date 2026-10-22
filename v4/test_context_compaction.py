"""Wire context stays self-contained while completion excludes unrelated evidence."""
import copy
import json
import os
import subprocess
import sys
import unittest
from .test_integration import seeded, review_all, global_verdict


def payload(prompt):
    return json.loads(prompt.split('<context-data>\n')[1].split('\n</context-data>')[0])


class ContextCompaction(unittest.TestCase):
    def test_completion_scopes_claims_but_keeps_undeclared_parent_catalog(self):
        r = seeded(); r.global_dependency_batch_size = 1; review_all(r)
        _, initial_prompt = r.prepare_phase6_global_sweep()
        initial = payload(initial_prompt)
        response = global_verdict(r)
        response['dependency_reviews'] = [row for row in response['dependency_reviews'] if row['claim_id'] != 'c']
        r.collect_phase6_global_sweep(response, model_identity='test/global')
        _, prompt = r.prepare_phase6_global_sweep()
        completion = payload(prompt)
        self.assertEqual(completion['target_claim_ids'], ['c'])
        self.assertEqual([c['claim_id'] for c in completion['state']['claims']], ['a', 'b', 'c'])
        catalog = {c['claim_id']: c for c in completion['claim_catalog']}
        self.assertEqual(set(catalog), {c['claim_id'] for c in r.run.claims})
        self.assertEqual(catalog['independent']['text'], '任意論述')
        self.assertIn('assertion', catalog['independent'])
        self.assertEqual(len(initial['state']['claims']), len(r.run.claims))
        r.collect_phase6_global_sweep(global_verdict(r) | {'dependency_reviews': [row for row in global_verdict(r)['dependency_reviews'] if row['claim_id'] == 'c']}, model_identity='test/global')
        _, final_prompt = r.prepare_phase6_global_sweep()
        self.assertEqual(payload(final_prompt)['review_scope'], 'global_consistency')
        self.assertEqual(len(payload(final_prompt)['state']['claims']), len(r.run.claims))

    def test_scoped_sources_follow_all_calc_and_event_ancestors(self):
        from .review_context import scoped_review_state
        state = dict(claims=[dict(claim_id='c', dependencies=['p'], refs=['outer'], fact_refs=[], bindings=[]),
                             dict(claim_id='p', dependencies=[], refs=['event'], fact_refs=[], bindings=[])],
                     facts={'fact': {'value': 3}, 'unused': {'value': 9}},
                     calcs={'outer': dict(calc_type='growth', input_refs=['inner']),
                            'inner': dict(calc_type='margin', input_refs=['fact'])},
                     events={'event': dict(source_ref='article', aggregate_ref='aggregate')},
                     evidence={'article': {'text': 'body'}, 'aggregate': {'text': 'aggregate body'}, 'unrelated': {'text': 'X' * 10000}},
                     source_lineage={'fact': {'derived_from': ['origin'], 'evidence_refs': ['article']},
                                     'origin': {'publisher': 'Original source'}, 'unused': {}}, trade={'direction': 'WAIT'})
        before = copy.deepcopy(state)
        scoped = scoped_review_state(state, ['c'])
        self.assertEqual(set(scoped['calcs']), {'inner', 'outer'})
        self.assertEqual(set(scoped['facts']), {'fact'})
        self.assertEqual(set(scoped['evidence']), {'article', 'aggregate'})
        self.assertEqual(set(scoped['calculation_definitions']), {'growth', 'margin'})
        self.assertEqual(set(scoped['source_lineage']), {'fact', 'origin'})
        self.assertEqual(state, before)
        self.assertLess(len(json.dumps(scoped)), len(json.dumps(state)))

    def test_direct_claim_reference_without_dependency_preserves_source_lineage(self):
        from .review_context import scoped_review_state
        state = dict(
            claims=[dict(claim_id='child', dependencies=[], refs=['parent'], fact_refs=[], bindings=[]),
                    dict(claim_id='parent', dependencies=[], refs=['ev'], fact_refs=[], bindings=[]),
                    dict(claim_id='unrelated', dependencies=[], refs=['unused'], fact_refs=[], bindings=[])],
            facts={}, calcs={}, events={},
            evidence={'ev': dict(text='primary evidence', source_id='source'),
                      'unused': dict(text='other evidence', source_id='other')},
            source_lineage={'source': dict(publisher='Original publisher'),
                            'other': dict(publisher='Other publisher')},
            trade={'direction': 'WAIT'})
        before = copy.deepcopy(state)
        scoped = scoped_review_state(state, ['child'])
        self.assertEqual([c['claim_id'] for c in scoped['claims']], ['child', 'parent'])
        self.assertEqual(set(scoped['evidence']), {'ev'})
        self.assertEqual(set(scoped['source_lineage']), {'source'})
        self.assertEqual(state, before)

    def test_binding_ref_only_reaches_fact_and_evidence_lineage(self):
        from .review_context import scoped_review_state
        state = dict(claims=[dict(claim_id='a', dependencies=[], refs=[], fact_refs=[],
            bindings=[dict(ref_id='binding.fact'), dict(ref_id='binding.evidence')])],
            facts={'binding.fact': dict(value=2, source_id='report-source'),
                   'unrelated.fact': dict(value=3)},
            calcs={}, events={},
            evidence={'binding.evidence': dict(text='source', source_id='evidence-origin'),
                      'unrelated.evidence': dict(text='noise')},
            source_lineage={'report-source': dict(publisher='Issuer'),
                            'evidence-origin': dict(publisher='Original'),
                            'unrelated.origin': dict(publisher='Third')},
            trade={'direction': 'WAIT'})
        result = scoped_review_state(state, ['a'])
        self.assertEqual(set(result['facts']), {'binding.fact'})
        self.assertEqual(set(result['evidence']), {'binding.evidence'})
        self.assertEqual(set(result['source_lineage']), {'report-source', 'evidence-origin'})

    def test_history_compacts_deltas_and_preserves_pending_rechecks(self):
        from .review_context import compact_review_history
        before = dict(claim_id='a', text='old', refs=['source'], bindings=[{'value': 'X' * 5000}])
        after = before | {'text': 'new'}
        finding = dict(finding_id='open', claim_ids=['a'], issue='still needs recheck')
        history = [dict(claim_changes=[dict(claim_id='a', before=before, after=after)], invalidated_global_findings=[finding])]
        compact = compact_review_history(history, claim_ids={'a'})
        self.assertEqual(compact, [dict(claim_changes=[dict(claim_id='a', before={'text': 'old'}, after={'text': 'new'})], findings_to_recheck=[finding])])
        self.assertEqual(compact_review_history(history, claim_ids={'other'}), [])

    def test_context_is_identical_across_process_hash_seeds(self):
        code = """import json
from v4.review_context import scoped_review_state
state = dict(claims=[dict(claim_id='c',dependencies=[],refs=['b','a'],fact_refs=[],bindings=[])],facts={'a':{'value':1},'b':{'value':2}},calcs={},events={},evidence={})
print(json.dumps(scoped_review_state(state,['c']), ensure_ascii=False, separators=(',',':')))
"""
        outputs = [subprocess.check_output([sys.executable, '-c', code], env=os.environ | {'PYTHONHASHSEED': seed, 'PYTHONDONTWRITEBYTECODE': '1'}) for seed in ('1', '937')]
        self.assertEqual(outputs[0], outputs[1])
