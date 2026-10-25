# Scoped original-role reconsideration and review wire budgets

Canonical Claim objects, source stores, DAGs and verification task hashes remain
unchanged by transport projection. Every Claim still receives semantic review;
non-PASS Claims still require deep review and delivery still requires Global.

## Review transport

Fresh runners use a 128 KiB atomic item budget and a 128 KiB deep item budget.
Saved run budgets and explicit overrides retain their values; updating the code
does not silently raise an existing run's limits. The packet budget is separate.
An oversized request reports item, packet and effective item limits separately.

Wire projection only interns exact structured values in a self-contained request:
repeated Claim binding dimensions and repeated task-context fields. Local catalog
IDs are deterministic. Instructions explicitly require resolving each reference
for its own Claim. Unique values and full source contents are preserved. A
projection is kept only if its bytes plus decoding instructions are smaller.
The decoder is also used by budget/diagnostics validation; a pooled context
cannot conceal incomplete related diagnostics. No cross-session model memory,
prose classification, evidence truncation or default PASS is assumed.

An input that remains too large is still blocked with measured diagnostics.
Do not assume any fixed byte limit can fit every future evidence set. Inspect
its breakdown before an explicit `configure-reviews` change. Batch packing can
pool evidence across related requests, while independent requests remain
self-contained.

## Original-role reconsideration

A finding should use `claim_patch` when correcting or withdrawing a local claim
is sufficient. `role_redeliberation` is for a conclusion that requires its
original author role to reconsider; the reviewer must state why patching alone
is insufficient. The program never infers that route from finding prose.

Once required reviews are collected, Native `next` can issue a packet with
`kind=role_redeliberation`. Its item ID is the original author role. Its prompt
contains the target Claims, their evidence and the exact findings. Respond with
one full Claim for every listed target, preserving IDs and author roles:

```json
{"bull":{"claims":[{"claim_id":"original.id","text":"...","claim_type":"interpretation","refs":[],"fact_refs":[],"dependencies":[],"author_role":"bull","assertion":{"kind":"interpretation"},"bindings":[]}],"reason":"Why the original role revised or maintained this conclusion."}}
```

The resident main Agent may perform this role; no model/provider is hardcoded.
AGY dispatch remains restricted to its review work and fallback is unchanged.

Submit through the regular `submit` command below the automatic revision cap.
At the cap, an operator-only packet is retained rather than spending more
model calls automatically. Explicit submission is available through:

```sh
python -m v4.agent_cli submit-redeliberation --work-dir RUN_DIR \
  --packet-id PACKET_ID --input-hash INPUT_HASH --executor ACTUAL_IDENTITY \
  --responses ORIGINAL_ROLE_RESPONSE.json
```

The response is archived under the original role task ID with its scope, epoch,
finding IDs and accepted task hashes. Candidate schema, author, scope, DAG and
source validation happen before acceptance. Changed Claims pass through the
existing surgical-patch transaction and descendant revalidation. An unchanged
answer also requires fresh reviews and consumes a reconsideration round; it
never self-resolves the finding. Unrelated receipts remain valid.

The external finding stays open until Global verifies the matching post-finding
original-role response against current Claims and premises. A queue-transaction
journal exposes received proof without independently publishing checkpoints.
Exact redelivery is idempotent only while the accepted state/history/epoch and
raw archive still match. Stale or conflicting responses are rejected.

Existing runs need no hand edits or new initialization. Their pending work is
read through the normal `next`/submission path; accepted evidence is not erased
merely because the wire encoding changed. This change does not claim that an
existing GLW-sized packet necessarily fits the new fresh-run default.
