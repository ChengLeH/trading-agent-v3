# V4 opt-in property tests

These test deterministic contracts, not the quality of LLM or investment judgments.
No model, network provider, credentials, or production service is called by the tests.
Use an isolated development environment and install `requirements-dev.txt`;
the large production/data-provider requirements file is not needed here.

```sh
python -m pip install -r requirements-dev.txt
python -m v4.property_tests --profile ci
python -m v4.property_tests --profile explore
```

An absent development dependency fails this explicit command. The existing
`python -m unittest discover -s v4 -t .` suite does not require Hypothesis;
property files deliberately use `prop_*.py` rather than `test_*.py`.

## Contracts

- **Serialization:** build real atomic tasks and prompts using both known and
  unknown operations. Check the definition ordering, prompt bytes, task digest,
  and prompt digest. Ordered calculation operands remain ordered. This is
  complementary to the existing subprocess/hash-seed regression, not a
  replacement: an in-process generator cannot change its interpreter's hash seed.
- **Debt:** missing keys, explicit nulls, zero amounts, positive amounts,
  completeness declarations, and overlap declarations have separate inputs.
  Only a complete declared composition yields a derived total. Available
  components are retained while each absent component/declaration is named in
  diagnostics. An explicitly reported total can stand on its own without
  inventing absent components. The oracle is the declared debt truth table,
  not the legacy builder's `or 0` behavior.
- **Lifecycle:** compare real persisted receipts with an independent model
  keyed by Claim content, the transitive premise cone, and epoch. Fixed sources
  and contracts do not change during these sequences. Unaffected receipts
  survive; changed premises and fresh epochs invalidate prior proof. Run real
  queue, checkpoint, and raw-archive IO, including stale input hashes, legal
  clarity-note objects, dependency-only repairs, and global revision/reload/patch.
  There are three mutable Claims plus eight fixed minimal role Claims, because
  the real native workflow requires all nine roles; no completion guard is mocked.
  Repair actions run only after all atomic reviews are valid, as required by
  the real repair API. This models sequential restarts, not concurrent crashes.

## Budgets and failure handling

`ci` uses 40 generated cases and at most 12 state-machine steps, deterministic
generation, no example database, and no per-example wall-clock deadline.
`explore` uses 200 cases and 30 steps, variable generation and the Hypothesis
example database. Both print replay blobs on failure. These bounds constrain
search, not the time spent shrinking a failure; the CI step also has a 12-minute
timeout inside a 15-minute job so artifact upload has remaining time.

The workflow runs the bounded profile on pull requests and exploratory tests
on a separate nightly event. Nightly failure is visible but is not a PR-event
check and does not change merge protection. Logs and minimized example database
entries are uploaded as candidates; confirm them and add a fixed regression
before treating them as a proven product defect. Do not automatically edit
production code or waive a contract from a randomized failure.

GitHub scheduled workflows require the workflow on the default branch. Adding
this file only on `v4-atomic-claims` does not activate a nightly schedule; this
change does not modify or merge `main`. Local exploration is available immediately.

## Initial measurement

Before these changes, CPython 3.15.0 on the development Mac ran the existing
545 V4 tests in 139.076 seconds (545 executed, zero errors/failures/skips).
Final isolated verification on the same interpreter ran the unchanged 545-test
suite successfully in 75.133 seconds, the five CI properties in 25.157 seconds,
and the five exploratory properties in 211.481 seconds. The two suite timings
show environment variance; they are not a speedup claim. An earlier overlapping
regression/exploration run had three existing short transport-timeout failures;
the isolated rerun had none. Do not benchmark or verify these timing-sensitive
transport fixtures concurrently with exploration. GitHub/Linux CI was not run
locally; these measurements are not CI performance guarantees.

Sensitivity check: substituting the historical unsorted operation-definition
builder was rejected by the new production-request property. No production
source was changed for that check. Mutants collapsing absent debt into zero,
retaining an epoch during fresh verification, and ignoring active-packet input
hashes were also rejected. The existing cross-process regression stays in the
example suite. Preserve fixed failures as normal regressions or explicit
Hypothesis examples; a seed alone is not a durable cross-version fixture.
