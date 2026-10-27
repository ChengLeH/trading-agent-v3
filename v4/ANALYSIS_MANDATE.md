# Analysis mandate and data acquisition

The host agent obtains data before V4 ingestion. The V4 manifest does not specify a universal default lookback, and a forecast horizon is not a historical-data window. `scripts/openbb_data.py` accepts unspecified start/end dates; its upstream provider decides their meaning. Its fundamentals helper defaults to four annual periods, which is not a universal V4 policy.

To translate explicit user requests into separate calendar windows, the host may run:

```
python -m v4.agent_cli plan-data --as-of 2026-10-27 --lookback-months 6 --horizon-months 3
```

Copy `analysis_mandate` from the output into the fresh input manifest. The host must acquire the data itself, respecting the provider's inclusive/exclusive boundary convention; this command does not fetch or filter data. A null field means the user did not specify that window, not an instruction to invent one. Future horizon, historical range, data availability, and the minimum needed for each indicator are separate considerations. If a requested limited range cannot support an indicator, preserve the limitation rather than silently widening the request or fabricating an indicator. Shorter data may reduce work but does not guarantee a faster model review.

The mandate is carried through role packets, atomic review, global review, checkpoint, and report/sidecar. Existing checkpoints without a mandate retain their previous behavior. Changing a mandate changes verification context and invalidates dependent receipts; don't edit an active checkpoint to bypass revalidation.
