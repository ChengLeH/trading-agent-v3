"""Run bounded properties separately from the existing example-based suite."""
import argparse
from pathlib import Path
import time
import unittest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=('ci', 'explore'), default='ci')
    args = parser.parse_args()
    # An absent development dependency must fail, never silently skip the suite.
    from hypothesis import settings
    settings.register_profile('v4-ci', max_examples=40, stateful_step_count=12,
                              derandomize=True, database=None, deadline=None, print_blob=True)
    settings.register_profile('v4-explore', max_examples=200, stateful_step_count=30,
                              derandomize=False, deadline=None, print_blob=True)
    settings.load_profile('v4-' + args.profile)
    root = Path(__file__).resolve().parents[2]
    started = time.perf_counter()
    suite = unittest.defaultTestLoader.discover(str(root / 'v4' / 'property_tests'),
                                               pattern='prop_*.py', top_level_dir=str(root))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(f'profile={args.profile} tests={result.testsRun} '
          f'elapsed_seconds={time.perf_counter() - started:.3f}', flush=True)
    return 0 if result.wasSuccessful() and result.testsRun else 1


if __name__ == '__main__':
    raise SystemExit(main())
