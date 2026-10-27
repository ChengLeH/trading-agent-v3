"""Structured, non-transforming market-data basis and event annotations."""
from copy import deepcopy
from datetime import date, timedelta
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest

from v3.pipeline import RunState
from v3.sources import SourceRecord
from .pipeline import OrchestratorV4


BARS = [
    {"date": "2026-10-15", "open": 100, "high": 105, "low": 99,
     "close": 100, "volume": 10},
    {"date": "2026-10-16", "open": 50, "high": 53, "low": 49,
     "close": 50, "volume": 11},
]


class MarketBasisAnnotations(unittest.TestCase):
    def build(self, *, source=None, meta=None, payload_metadata=None, bars=None):
        orch, run_id = OrchestratorV4(), "market-basis-fixture"
        source = deepcopy(source or {"id": "market"})
        source["id"] = "market"
        manifest = {
            "subject": {"ticker": "TEST"},
            "as_of": "2026-10-16",
            "sources": [source],
        }
        record_payload = {"bars": deepcopy(BARS if bars is None else bars)}
        record_payload.update(deepcopy(payload_metadata or {}))
        record = SourceRecord(
            "market", "openbb_ohlcv", "TEST", None, record_payload,
            "market-hash", meta=deepcopy(meta or {}),
        )
        orch.runs[run_id] = RunState(
            run_id=run_id, ticker="TEST", manifest=manifest, records=[record],
        )
        orch.runs[run_id].events = {}
        orch.build_facts(run_id)
        return orch, run_id, record

    def test_consistent_field_basis_is_normalized_and_source_wins(self):
        declared = {
            "open": "raw",
            "high": "raw_ohlcv",
            "low": "raw_close",
            "close": "raw",
        }
        orch, run_id, record = self.build(
            source={"id": "market", "price_basis_by_field": declared},
            meta={"price_basis_by_field": {field: "record_value" for field in declared}},
            payload_metadata={"price_basis_by_field": {field: "payload_value" for field in declared}},
        )
        run = orch.runs[run_id]
        diagnostic = record.meta["market_data_extraction"]
        self.assertEqual(diagnostic["price_basis_by_field"], {
            "open": "raw_close", "high": "raw_close",
            "low": "raw_close", "close": "raw_close",
        })
        self.assertEqual(diagnostic["price_basis_by_field_source"], "manifest")
        self.assertEqual(diagnostic["ohlc_basis_consistency"], "declared_consistent")
        self.assertFalse(any(row.get("check_id") == "price_fields_basis_unverified"
                             for row in diagnostic["findings"]))
        self.assertEqual(run.fact_store.get("fact.Close.latest").basis, "raw_price")
        self.assertEqual(run.calc_store.get("calc.ReturnD.latest")["meta"]["price_basis"],
                         "raw_close")
        self.assertEqual(run.calc_store.get("calc.ReturnD.latest")["value"], -0.5)
        self.assertEqual(record.payload["bars"], BARS)

    def test_mixed_and_missing_field_basis_warn_without_global_fallback_or_price_change(self):
        orch, run_id, record = self.build(
            source={"id": "market", "price_basis": "global_adjusted"},
            meta={"price_basis_by_field": {"open": "raw", "close": "record_close"}},
            payload_metadata={"price_basis_by_field": {
                "open": "payload_open", "high": "payload_high",
                "low": "payload_low", "close": "payload_close",
            }},
        )
        run = orch.runs[run_id]
        diagnostic = record.meta["market_data_extraction"]
        self.assertEqual(diagnostic["price_basis_by_field"], {
            "open": "raw_close", "high": "unknown",
            "low": "unknown", "close": "record_close",
        })
        self.assertEqual(diagnostic["price_basis_by_field_source"], "source_metadata")
        self.assertEqual(diagnostic["ohlc_basis_consistency"], "unverified")
        warning = next(row for row in diagnostic["findings"]
                       if row.get("check_id") == "price_fields_basis_unverified")
        self.assertEqual(warning["status"], "WARN")
        self.assertIn("LLM", warning["reason"])
        self.assertEqual(run.fact_store.get("fact.OHLCV.D.2026-10-16.high").basis, "unknown")
        self.assertEqual(run.fact_store.get("fact.Close.latest").basis, "price:record_close")
        self.assertEqual(run.calc_store.get("calc.ReturnD.latest")["value"], -0.5)
        self.assertEqual(record.payload["bars"], BARS)

    def test_atr_uses_high_low_close_basis_when_open_is_unknown(self):
        bars = []
        for offset in range(252):
            close = 100 + offset
            bars.append({
                "date": (date(2026, 10, 16) - timedelta(days=251 - offset)).isoformat(),
                "open": close - 1,
                "high": close + 2,
                "low": close - 2,
                "close": close,
                "volume": 10 + offset,
            })
        orch, run_id, record = self.build(
            bars=bars,
            source={"id": "market", "price_basis_by_field": {
                "high": "range_basis", "low": "range_basis", "close": "range_basis",
            }},
        )
        run = orch.runs[run_id]
        diagnostic = record.meta["market_data_extraction"]
        self.assertEqual(diagnostic["price_basis_by_field"]["open"], "unknown")
        self.assertEqual(diagnostic["ohlc_basis_consistency"], "unverified")
        self.assertEqual(run.fact_store.get("fact.ATR14.latest").basis,
                         "price:range_basis")
        self.assertEqual(run.fact_store.get("fact.High52w.latest").basis,
                         "price:range_basis")
        self.assertEqual(run.fact_store.get("fact.Low52w.latest").basis,
                         "price:range_basis")
        self.assertEqual(run.fact_store.get("fact.High52w.latest").value, 353)
        self.assertEqual(run.fact_store.get("fact.Low52w.latest").value, 98)

    def test_return_calc_drops_stale_basis_for_missing_or_mixed_source_close(self):
        from v3.facts import CalcStore, Fact, FactStore
        from .market_data import annotate_market_facts, normalize_market_record

        cases = (
            ("missing", {"open": "raw", "high": "raw", "low": "raw"},
             {"open": "raw", "high": "raw", "low": "raw", "close": "raw"}),
            ("mixed", {"open": "basis_a", "high": "basis_a",
                       "low": "basis_a", "close": "basis_a"},
             {"open": "basis_b", "high": "basis_b",
              "low": "basis_b", "close": "basis_b"}),
        )
        for case, first_basis, second_basis in cases:
            with self.subTest(case=case):
                diagnostics = []
                for source_id, field_basis in (("market-a", first_basis),
                                               ("market-b", second_basis)):
                    record = SourceRecord(
                        source_id, "openbb_ohlcv", "TEST", None,
                        {"bars": deepcopy(BARS)}, f"{source_id}-hash",
                    )
                    _, diagnostic = normalize_market_record(
                        record,
                        {"as_of": "2026-10-16", "sources": [{
                            "id": source_id, "price_basis_by_field": field_basis,
                        }]},
                    )
                    diagnostics.append(diagnostic)

                facts = FactStore()
                facts.add(Fact("fact.Close.prev", 100, "USD", "2026-10-15",
                               concept="Close", source_id="market-a"))
                facts.add(Fact("fact.Close.latest", 50, "USD", "2026-10-16",
                               concept="Close", source_id="market-b"))
                calcs = CalcStore()
                calcs.add("calc.ReturnD.latest", -0.5, "return_cc",
                          ["fact.Close.prev", "fact.Close.latest"], "RATIO",
                          meta={"price_basis": "stale_global"})
                annotate_market_facts(
                    SimpleNamespace(fact_store=facts, calc_store=calcs), diagnostics)
                self.assertNotIn("price_basis", calcs.get("calc.ReturnD.latest")["meta"])

    def test_event_timing_covers_bar_window_and_as_of_only_paths(self):
        from .market_data import normalize_market_record

        bars = deepcopy(BARS)
        bars[0]['date'], bars[1]['date'] = '2035-04-12', '2035-04-14'
        events = [dict(kind='opaque_source_event', effective_date=day)
                  for day in ('2035-04-11', '2035-04-13', '2035-04-15')]
        for source_bars, source_events, expected in (
            (bars, events, ['before_bar_window', 'within_bar_window', 'after_bar_window']),
            ([], [dict(effective_date='2035-04-15'), dict(effective_date='2035-04-17')],
                 ['on_or_before_as_of', 'future_as_of'])):
            with self.subTest(expected=expected):
                payload = dict(bars=deepcopy(source_bars), corporate_actions=deepcopy(source_events))
                original = deepcopy(payload)
                record = SourceRecord('market', 'openbb_ohlcv', 'TEST', None, payload, 'hash')
                normalized, diagnostic = normalize_market_record(record, dict(as_of='2035-04-16'))
                timing = diagnostic['corporate_action_timing']
                self.assertEqual([row['timing_classification'] for row in timing], expected)
                self.assertTrue(all(row['adjustment_status'] == 'unverified' for row in timing))
                self.assertEqual(diagnostic['corporate_actions'], source_events)
                self.assertEqual(record.payload, original)
                self.assertEqual([b['close'] for b in normalized['bars']],
                                 [b['close'] for b in source_bars])

    def test_future_corporate_action_is_raw_context_and_does_not_change_returns(self):
        event = {
            "kind": "vendor_specific_event",
            "action_id": "event-203",
            "effective_date": "2026-10-20",
            "source_ref": "filing:page-4",
            "adjustment_factor": 0.8,
            "vendor_extension": {"opaque": ["keep", 7]},
        }
        baseline, baseline_id, _ = self.build()
        orch, run_id, record = self.build(
            source={"id": "market", "corporate_actions": [event]},
            meta={"corporate_actions": [{"action_id": "metadata-loser"}]},
            payload_metadata={"corporate_actions": [{"action_id": "payload-loser"}]},
        )
        run = orch.runs[run_id]
        diagnostic = record.meta["market_data_extraction"]
        self.assertEqual(diagnostic["corporate_actions"], [event])
        self.assertEqual(diagnostic["corporate_actions_source"], "manifest")
        self.assertEqual(diagnostic["corporate_actions_completeness"], "unverified")
        self.assertEqual(diagnostic["corporate_action_timing"], [{
            "index": 0,
            "action_id": "event-203",
            "effective_date": "2026-10-20",
            "timing_classification": "future_as_of",
            "adjustment_status": "unverified",
        }])
        self.assertEqual(run.fact_store.get("fact.Close.latest").value,
                         baseline.runs[baseline_id].fact_store.get("fact.Close.latest").value)
        self.assertEqual(run.calc_store.get("calc.ReturnD.latest")["value"],
                         baseline.runs[baseline_id].calc_store.get("calc.ReturnD.latest")["value"])
        self.assertEqual(run.calc_store.get("calc.ReturnD.latest")["value"], -0.5)
        self.assertEqual(record.payload["corporate_actions"], [{"action_id": "payload-loser"}])

    def test_invalid_event_date_stays_unknown_and_warns(self):
        event = {"kind": "other", "action_id": "unknown-1",
                 "effective_date": "2026-10-20-not-a-date", "source_ref": "source:opaque"}
        _, _, record = self.build(payload_metadata={
            "corporate_actions": [event],
            "price_basis_by_field": {
                "open": "raw", "high": "raw_ohlcv",
                "low": "raw_close", "close": "raw",
            },
        })
        diagnostic = record.meta["market_data_extraction"]
        self.assertEqual(diagnostic["corporate_actions"], [event])
        self.assertEqual(diagnostic["corporate_actions_source"], "payload")
        self.assertEqual(diagnostic["price_basis_by_field_source"], "payload")
        self.assertEqual(diagnostic["corporate_action_timing"][0]["timing_classification"],
                         "unknown")
        self.assertEqual(diagnostic["corporate_action_timing"][0]["adjustment_status"],
                         "unverified")
        self.assertTrue(any(row.get("status") == "WARN"
                            and row.get("check_id") == "corporate_action_timing_unverified"
                            for row in diagnostic["findings"]))

    def test_absent_optional_metadata_preserves_existing_diagnostic_and_global_basis(self):
        _, _, record = self.build()
        diagnostic = record.meta["market_data_extraction"]
        self.assertTrue(any(f['check_id'] == 'market_52_week_history_incomplete'
                            for f in diagnostic['findings']))
        self.assertEqual({k: v for k, v in diagnostic.items() if k not in {'findings', 'limitations'}}, {
            "section": "market_data",
            "source_id": "market",
            "input_rows": 2,
            "normalized_rows": 2,
            "selected_price_field": "close",
            "price_basis": "raw_close",
            "price_basis_source": "default_assumption",
            "adjusted_close_present": False,
            "declared_price_basis": None,
        })

    def test_optional_market_metadata_survives_checkpoint_reload(self):
        from .checkpoint import load_checkpoint, save_checkpoint
        from .test_integration import seeded

        runner = seeded()
        event = {"kind": "opaque", "action_id": "retained",
                 "effective_date": "2026-10-20", "extra": {"keep": True}}
        runner.run.records = [SourceRecord(
            "market", "openbb_ohlcv", "TEST", None,
            {"bars": deepcopy(BARS), "corporate_actions": [event]},
            "market-hash", meta={"price_basis_by_field": {
                "open": "raw", "high": "raw", "low": "raw", "close": "raw",
            }},
        )]
        runner.orch.build_facts(runner.run_id)
        expected = deepcopy(runner.run.records[0].meta["market_data_extraction"])
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "checkpoint.json"
            save_checkpoint(runner, path)
            restored = load_checkpoint(path)
        self.assertEqual(restored.run.records[0].meta["market_data_extraction"], expected)
        self.assertEqual(restored.run.records[0].meta["market_data_extraction"]["corporate_actions"],
                         [event])


if __name__ == "__main__":
    unittest.main()
