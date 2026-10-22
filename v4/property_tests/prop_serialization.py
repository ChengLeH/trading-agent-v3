"""Exercise real atomic request construction, including unknown operations."""
import unittest
from unittest.mock import patch

from hypothesis import given, strategies as st

from v4.atomic_phase6 import build_atomic_tasks, build_atomic_prompt
from v4.schema import digest
from v4.test_atomic_phase6 import make_run
from v4.test_contracts import claim


# Both categories are mandatory, not probabilistically absent from a sample.
KINDS = ('growth', 'fcf', 'margin', 'return_cc', 'volume_ratio',
         'future_operation_a', 'future_operation_b')


def request(order):
    c = claim('candidate')
    c['refs'] = ['calc.' + kind for kind in KINDS]
    run = make_run([c])
    for kind in order:
        run.calc_store.add('calc.' + kind, 1, kind, ['fact.dram_supply.2029'], 'RATIO')
    task, = build_atomic_tasks(run)
    definitions = task.context['calculation_definitions']
    expected = sorted(KINDS[:5]) + sorted(KINDS[5:])
    if list(definitions) != expected:
        raise AssertionError(f'Noncanonical operation definitions: {list(definitions)}')
    prompt = build_atomic_prompt(task, verification_epoch='fixed-property-epoch')
    return prompt.encode('utf-8'), digest(task.to_dict()), digest(prompt)


class AtomicSerializationProperties(unittest.TestCase):
    @given(st.lists(st.text(alphabet='abcXYZ012_-', min_size=1, max_size=20), max_size=120))
    def test_unrelated_diagnostic_counts_and_names_preserve_wire_and_hash(self, names):
        run = make_run([claim('candidate')])
        results = []
        for rows in ([], [dict(entity='UNRELATED', ref_id='unbound.' + name,
                               check_id=name, status='WARN', reason='original text') for name in names]):
            with patch('v4.atomic_phase6.ingestion_diagnostics', return_value=[
                    dict(section='coverage', findings=rows)]):
                task, = build_atomic_tasks(run)
            results.append((build_atomic_prompt(task, verification_epoch='fixed'), digest(task.to_dict())))
        self.assertEqual(results[0], results[1])

    @given(st.permutations(KINDS))
    def test_calc_insertion_order_preserves_wire_request_and_hash(self, order):
        self.assertEqual(request(order), request(KINDS))

    @given(st.integers(min_value=1, max_value=1000),
           st.integers(min_value=1001, max_value=2000))
    def test_ordered_calculation_inputs_are_not_canonicalized_as_a_set(self, low, high):
        from v3.facts import Fact
        run = make_run([claim('candidate')])
        for name, value in [('low', low), ('high', high)]:
            run.fact_store.add(Fact(name, value, 'USD', 'FY2025',
                                    concept='metric', source_id='fixture'))
        run.claims[0]['refs'] = ['ratio']
        run.calc_store.add('ratio', low / high, 'margin', ['low', 'high'], 'RATIO')
        first, = build_atomic_tasks(run)
        run.calc_store._calcs['ratio']['input_refs'] = ['high', 'low']
        second, = build_atomic_tasks(run)
        self.assertNotEqual(digest(first.to_dict()), digest(second.to_dict()))
        self.assertNotEqual(build_atomic_prompt(first, verification_epoch='fixed'),
                            build_atomic_prompt(second, verification_epoch='fixed'))
