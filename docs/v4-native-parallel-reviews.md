# Native V4 bounded parallel reviews

This path changes scheduling and wire size, not semantic acceptance. Every Claim
still needs an atomic LLM verdict. Only SUSPICIOUS/FAIL goes to deep review;
patches invalidate affected descendants; delivery still needs a complete global
consistency sweep and complete dependency coverage.

## Host and secondary

The host identity is supplied by its current runtime. It is never a hardcoded
model. Role generation, evidence collection, repairs, the whole-report global
sweep and final delivery remain host work. Independent analyst roles can run
concurrently in their existing role group; bull/bear can run concurrently after
that group commits. Research manager, trader and risk manager retain their
ordered dependencies. Use full host subagents for whole roles or coarse bundles,
not one spawned agent per Claim.

For atomic/deep work the host can reserve a coarse bundle:

```sh
python -m v4.agent_cli claim-reviews --work-dir RUN --executor HOST_ID --max-items 4 --batch-size 4
```

Read each returned `requests` entry; submit the actual raw JSON object from that
request with `submit-review-request --packet-id ... --request-id ...
--input-hash ... --executor HOST_ID --response FILE`. Do not extract rows and
pretend they came from a single-Claim prompt. The archive records the actual
shared prompt, response and executor; the collector validates each row.

Run the optional secondary concurrently from a separate host process:

```sh
python -m v4.agent_cli dispatch-secondary --work-dir RUN --agy-model MODEL_ID --max-workers 3 --batch-size 4 --max-items 128
```

The total Claim budget bounds this dispatch window; it is not a new maximum
Claim count. CLI defaults: four workers, up to four Claims per wire request,
16 total dispatched Claims. Byte budgets still apply; oversized packed requests
fall back to already-validated single requests without truncation. The library
retains single-packet/single-Claim defaults for existing callers; opt into
`batch_size=4, continuous=True` for the new scheduler. `--single-packet` retains
the initial-packet bound in the CLI.

Reservations are stored per item under the queue lock. Another owner sees only
unreserved work. Default lease duration is one hour, independent of model
request timeout; the AGY dispatcher renews while waiting. A manually working
host must call `renew-reviews` with the returned lease before expiry, or
`release-reviews` to hand it back. After process death, expired work can be
reclaimed. Late replies cannot overwrite a new reservation. Valid completed
receipts are retained, not leased again. Legacy `next`/`submit` remains usable;
use the claim interface for genuinely concurrent workers.

A ready deep review may run while unrelated atomic work is in flight. This
never permits deep before that Claim has a valid non-PASS atomic receipt and
never permits early global/final delivery.

## Dependency completion

After the host's whole-report global sweep returns partial dependency coverage,
`claim-dependencies --work-dir RUN --executor HOST_ID --max-items 12` reserves a
separate immutable chunk. These chunks may complete out of issue order. Their
actual reviewers are recorded separately from the original global reviewer.
Only dependency rows belong in these chunks: global findings and external
resolution proofs remain the primary reviewer's responsibility. Unresolved
semantic dependencies still become findings; omitted rows remain pending.

AGY can handle this stage too. Accepted chunks are replayed in durable acceptance
order, with the actual raw archive checked first. A closed sweep must not be
reopened by a leftover serial completion packet. Global resolutions remain
owned by the primary reviewer; parallel dependency workers cannot impersonate it.

## Failure and restart

Quota exhaustion stops assigning new secondary work. In-flight independent
answers may still complete and are collected; all unfinished reservations are
released for the host. Ordinary per-item errors do not manufacture a verdict.
A quota-disabled secondary stays disabled until explicit `restore-secondary`
after the operator verifies availability. No API/model fallback is selected
behind the host's back. If the disabled secondary left only partial reference
checks, the next host claim records a handoff and requests a complete fresh
review of that unfinished Claim; valid completed receipts are retained. For an
otherwise available reviewer who explicitly relinquishes work, the new host can
use `claim-reviews --takeover-incomplete` after lease release/expiry. This is an
explicit restart of incomplete work, not permission to merge different
reviewers' partial checks. `next` may return an in-flight/ready status rather than
a new packet when another owner already has the work.

A restart reads canonical state and validates per-Claim proof archives. Do not
edit checkpoint/state files to reset progress or budgets. Lease expiry recovers
abandoned work without discarding accepted siblings.

## Offline evidence

The deterministic 12-Claim integration fixture needs 12 standalone requests
(231,054 wire bytes), or 3 bounded four-Claim requests (80,359 bytes). These are
request-count/payload measurements, not a real-model wall-clock prediction.
Regression tests exercise disjoint owners, reload, exact raw batch provenance,
partial responses, expiry/reclaim, ready deep overlap and global completion.
Real AGY elapsed time and output quality still require live acceptance.
