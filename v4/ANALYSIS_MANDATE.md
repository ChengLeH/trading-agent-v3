# Analysis mandate and data acquisition

The host agent obtains data before V4 ingestion. The V4 manifest does not specify a universal default lookback, and a forecast horizon is not a historical-data window. `scripts/openbb_data.py` accepts unspecified start/end dates; its upstream provider decides their meaning. Its fundamentals helper defaults to four annual periods, which is not a universal V4 policy.

To translate explicit user requests into separate calendar windows, the host may run:

```
python -m v4.agent_cli plan-data --as-of 2026-10-27 --lookback-months 6 --horizon-months 3
```

Copy `analysis_mandate` from the output into the fresh input manifest. The host must acquire the data itself, respecting the provider's inclusive/exclusive boundary convention; this command does not fetch or filter data. A null field means the user did not specify that window, not an instruction to invent one. Future horizon, historical range, data availability, and the minimum needed for each indicator are separate considerations. If a requested limited range cannot support an indicator, preserve the limitation rather than silently widening the request or fabricating an indicator. Shorter data may reduce work but does not guarantee a faster model review.

The mandate is carried through role packets, atomic review, global review, checkpoint, and report/sidecar. Existing checkpoints without a mandate retain their previous behavior. Changing a mandate changes verification context and invalidates dependent receipts; don't edit an active checkpoint to bypass revalidation.

New runs use `decision-comparison/1`: the canonical trade Claim carries a research view and explicit BUY/SHORT alternatives, plus a WAIT rationale and reassessment trigger where applicable. `UNASSESSABLE`, `NOT_ALLOWED`, and unknown horizons are valid; there is no forced trade or invented risk policy. Reference IDs must resolve and must be declared by the trade Claim. Existing per-Claim and global LLM reviews assess whether the comparison and references actually support the decision; structural checks do not certify investment quality.

This change upgrades the semantic instructions to `financial-evidence/12`; existing verification receipts made under older instructions must be revalidated using the normal checkpoint contract-upgrade mechanism. Older checkpoint trades remain readable without fabricated comparison data; a legacy decision is not evidence that the new comparison was performed. Real-model acceptance remains separate from deterministic regression tests.
