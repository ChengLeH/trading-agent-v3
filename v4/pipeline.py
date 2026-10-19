"""V4 orchestration: reuse typed ingestion/calculation, never V3 prose gates."""
from copy import deepcopy
from v3.pipeline import Orchestrator
from v3.sources import SourceError
from v3.facts import FactStore, CalcStore
from v3 import calculations as K
from v3.validation import check_source_identity, check_source_integrity
from .schema import Claim, claim_index, descendants, digest
from .bindings import validate_claim_bindings
from .atomic_phase6 import build_atomic_tasks
from .review_context import calculation_definitions
from datetime import date


class ClaimSourceError(ValueError):
    """Structural grounding failure with exact object routing, never prose matching."""
    def __init__(self, claim):
        self.claim = {key: deepcopy(claim[key]) for key in
                      ("author_role", "claim_id", "claim_type", "refs", "fact_refs", "dependencies")}
        super().__init__(f"grounded claim needs a source reference; author_role={claim['author_role']}; "
                         f"claim_id={claim['claim_id']}; claim_type={claim['claim_type']}")

    def details(self):
        return dict(role_validation=dict(code="grounded_source_required", **deepcopy(self.claim)))


class ClaimReferenceError(ValueError):
    """Exact structured reference diagnostics; never guess replacement IDs."""
    def __init__(self, claim, code, field, invalid_ids, message):
        self.validation = dict(code=code, author_role=claim['author_role'],
            claim_id=claim['claim_id'], field=field, invalid_ids=sorted(invalid_ids))
        super().__init__(f"{message}; author_role={claim['author_role']}; "
                         f"claim_id={claim['claim_id']}; field={field}; invalid_ids={sorted(invalid_ids)}")

    def details(self):
        return dict(role_validation=deepcopy(self.validation))


def source_catalog(run):
    sources = {}
    for kind, store in (("fact", run.fact_store), ("calc", run.calc_store),
                        ("evidence", run.evidence_store)):
        for ref, value in store.to_dict().items():
            if ref in sources:
                raise ValueError(f"ambiguous source ID: {ref}")
            sources[ref] = deepcopy(value)
    for ref, event in getattr(run, "events", {}).items():
        if ref in sources:
            raise ValueError(f"ambiguous event ID: {ref}")
        sources[ref] = deepcopy(event)
    return sources


def validate_trade_claim(claims, trade):
    plans = [c for c in claims if c["assertion"]["kind"] == "trade_plan"]
    has_trader = any(c["author_role"] == "trader" for c in claims)
    if len(plans) != (1 if has_trader else 0):
        trader_ids = [c["claim_id"] for c in claims if c["author_role"] == "trader"]
        plan_ids = [c["claim_id"] for c in plans]
        raise ValueError(f"trader requires exactly one canonical trade_plan Claim; trader_claim_ids={trader_ids}; trade_plan_claim_ids={plan_ids}")
    if plans and plans[0]["assertion"]["trade"] != trade:
        raise ValueError("canonical trade_plan Claim differs from derived trade state")


def calculation_findings(run):
    """Recompute typed operations using V3's pure finance kernel."""
    facts, calcs = run.fact_store.to_dict(), run.calc_store.to_dict()
    out, active, done = [], set(), set()

    def check(cid):
        if cid in active:
            raise ValueError("calculation dependency cycle")
        if cid in done:
            return
        active.add(cid)
        c = calcs[cid]
        vals = []
        for ref in c["input_refs"]:
            if ref in facts:
                vals.append(facts[ref]["value"])
            elif ref in calcs:
                check(ref)
                vals.append(calcs[ref]["value"])
            else:
                raise ValueError(f"missing calculation input: {ref}")
        meta, kind = c.get("meta", {}), c["calc_type"]
        operations = {
            "fcf": lambda: K.calc_fcf(*vals),
            "growth": lambda: K.calc_growth(*vals),
            "return_cc": lambda: K.close_to_close_return(*vals),
            "ev": lambda: K.calc_enterprise_value(*vals),
            "volume_ratio": lambda: K.calc_volume_ratio(*vals),
            "derived_pct": lambda: K.calc_distance_pct(vals[1], vals[0]),
            "cagr": lambda: K.calc_cagr(*vals, meta["years"]),
            "margin": lambda: K.calc_margin(*vals, meta.get("num_basis", "unknown"),
                                             meta.get("den_basis", "unknown")),
            "per_share": lambda: K.calc_per_share(*vals, meta["total_unit"], meta["shares_unit"]),
            "weighted_cost": lambda: K.calc_weighted_entry_cost(meta["tranches"]),
            "risk_reward": lambda: K.calc_risk_reward_ratio(*vals, direction=meta["direction"]),
            "position_risk": lambda: K.calc_position_risk_pct(*vals, direction=meta["direction"]),
        }
        if kind in operations:
            got = operations[kind]()
            tolerance = meta.get("tolerance", 1e-4)
            if not isinstance(tolerance, (int, float)) or not 0 <= tolerance <= 1e-4:
                raise ValueError("invalid arithmetic tolerance")
            if abs(got - c["value"]) > tolerance:
                raise ValueError(f"{cid}: stored value differs from kernel result {got}")
        else:
            # Unsupported operations retain a visible limitation; never
            # claim deterministic proof. Semantic review receives the inputs.
            out.append(dict(status="WARN", object_id=cid, check_id="calculation",
                            reason=f"kernel recheck unavailable for operation {kind}"))
        active.remove(cid)
        done.add(cid)

    for cid in calcs:
        try:
            check(cid)
        except (ValueError, TypeError, KeyError, ZeroDivisionError) as exc:
            out.append(dict(status="FAIL", object_id=cid, check_id="calculation", reason=str(exc)))
            active.clear()
    return out


class OrchestratorV4(Orchestrator):
    def start_run(self, subject, manifest):
        rid = super().start_run(subject, manifest)
        self.runs[rid].events = {}
        return rid

    def register_trade(self, run_id, trade):
        """Rebuild only resources created by the previous trade registration."""
        run = self.runs[run_id]
        shadow = deepcopy(run)
        old_facts, old_calcs = getattr(run, "trade_generated", ([], []))
        shadow.fact_store, shadow.calc_store = FactStore(), CalcStore()
        for fid, f in run.fact_store.to_dict().items():
            if fid not in old_facts:
                from v3.facts import Fact
                shadow.fact_store.add(Fact(**f))
        for cid, c in run.calc_store.to_dict().items():
            if cid not in old_calcs:
                shadow.calc_store.add(cid, c["value"], c["calc_type"], c["input_refs"], c["unit"], c["meta"])
        if trade.get("direction") not in {"WAIT", "BUY", "SHORT"}:
            raise ValueError("unsupported structured trade direction")
        if trade["direction"] != "WAIT":
            for key in ("entry", "stop", "target", "position_pct"):
                if type(trade.get(key)) not in (float, int):
                    raise ValueError(f"trade.{key} must be numeric")
            if not 0 < trade["position_pct"] <= 100:
                raise ValueError("trade.position_pct outside (0,100]")
        digest(trade)
        before_facts, before_calcs = set(shadow.fact_store.to_dict()), set(shadow.calc_store.to_dict())
        self.runs[run_id] = shadow
        try:
            created = super().register_trade(run_id, trade)
        except Exception:
            self.runs[run_id] = run
            raise
        shadow.trade_generated = (sorted(set(shadow.fact_store.to_dict()) - before_facts),
                                  sorted(set(shadow.calc_store.to_dict()) - before_calcs))
        # Preserve the RunState identity held by callers.
        run.fact_store, run.calc_store, run.trade = shadow.fact_store, shadow.calc_store, deepcopy(trade)
        run.trade_generated = shadow.trade_generated
        self.runs[run_id] = run
        return created

    def _sync_views(self, run):
        # No separately stored prose/claim copies can outlive a patch.
        roles = dict.fromkeys([*run.analyses, *(c["author_role"] for c in run.claims)])
        grouped = {role: [] for role in roles}
        for c in run.claims:
            grouped[c["author_role"]].append(c)
        run.analyses = {role: {"claim_ids": [c["claim_id"] for c in claims], "claims": deepcopy(claims)}
                        for role, claims in grouped.items()}
        run.packets.clear()

    def submit_batch(self, run_id, claims_by_role):
        run = self.runs[run_id]
        incoming = []
        for role, claims in claims_by_role.items():
            if role in run.analyses:
                raise ValueError(f"role already submitted: {role}")
            if not isinstance(claims, list) or not claims:
                raise ValueError(f"role {role} must return at least one Claim object")
            for raw in claims:
                c = Claim.from_dict(raw).to_dict()
                if c["author_role"] != role:
                    raise ValueError("author_role differs from submitting role")
                incoming.append(c)
        candidate = run.claims + incoming
        index = claim_index(candidate)  # transactional validation, including cross-role dependencies
        validate_trade_claim(candidate, run.trade)
        sources = source_catalog(run)
        fact_ids = set(run.fact_store.to_dict())
        if set(sources).intersection(index):
            raise ValueError("claim/source ID collision")
        for c in incoming:
            unknown = set(c["refs"]) - sources.keys() - index.keys()
            if unknown:
                raise ClaimReferenceError(c, "unknown_reference_ids", "refs", unknown, "unknown refs")
            if set(c["fact_refs"]) - fact_ids:
                raise ClaimReferenceError(c, "noncanonical_fact_ids", "fact_refs",
                                          set(c["fact_refs"]) - fact_ids, "fact_refs must refer to canonical facts")
            expected = set(c["refs"]).intersection(sources)
            bound = [b["ref_id"] for b in c["bindings"]]
            bound_sources = set(bound).intersection(sources)
            if bound_sources != expected or len(bound) != len(set(bound)):
                missing = sorted(expected - bound_sources)
                duplicates = sorted({ref for ref in bound if bound.count(ref) > 1})
                raise ClaimReferenceError(c, "source_binding_mismatch", "bindings", missing + duplicates,
                    f"every source ref requires exactly one typed binding; "
                    f"missing_source_bindings={missing}; duplicate_bindings={duplicates}")
            # Claim refs already resolve through claim_index and must be DAG
            # dependencies. Optional bindings preserve the model's attribution;
            # they are not canonical source records or a reason to skip NLI.
            # A model-authored label cannot establish the evidence obligation.
            # All admitted objects remain unverified until the mandatory LLM
            # review checks their substance, including zero-source assertions.
        run.claims = deepcopy(candidate)
        for role in claims_by_role:
            run.analyses[role] = {}
        self._sync_views(run)

    def submit_analysis(self, run_id, role, analysis_text, claims):
        self.submit_batch(run_id, {role: claims})
        return len(claims)

    def get_packet(self, run_id, role):
        run = self.runs[run_id]
        # Structured objects replace long prose handoffs. No text clipping,
        # keyword routing, source guessing, or hidden summaries.
        calcs = run.calc_store.to_dict()
        source_index = {}
        for kind, records in (("fact", run.fact_store.to_dict()), ("calc", calcs),
                              ("evidence", run.evidence_store.to_dict()), ("event", run.events)):
            for ref, record in records.items():
                source_index[ref] = kind
        packet = dict(source_index=source_index, role=role, ticker=run.ticker, as_of=run.manifest["as_of"],
                      facts=run.fact_store.to_dict(), calcs=calcs,
                      calculation_definitions=calculation_definitions(calcs),
                      evidence=run.evidence_store.to_dict(), events=deepcopy(run.events),
                      claims=deepcopy(run.claims), trade=deepcopy(run.trade))
        run.packets[role] = deepcopy(packet)
        return packet

    def validate_stage(self, run_id, stage):
        run = self.runs[run_id]
        claim_index(run.claims)
        validate_trade_claim(run.claims, run.trade)
        out = []
        for fn, args in ((check_source_integrity, (run.records,)),
                         (check_source_identity, (run.manifest, [r for r in run.records if not isinstance(r, SourceError)]))):
            out.extend(f.to_dict() for f in fn(*args))
        sources = source_catalog(run)
        for task in build_atomic_tasks(run):
            for ref in task.unresolved_refs:
                out.append(dict(status="FAIL", claim_id=task.claim_id, check_id="reference_exists",
                                ref_id=ref, reason="referenced source no longer exists"))
        out.extend(f.to_dict() for f in validate_claim_bindings(
            run.claims, run.fact_store, require_bindings=True, sources=sources))
        out.extend(calculation_findings(run))
        # Fact identity includes entity. Different entities/bases/scopes are
        # legitimate distinct facts, unlike V3's broader collision grouping.
        seen = {}
        for fid, f in run.fact_store.to_dict().items():
            key = tuple(f.get(k) for k in ("entity", "concept", "period", "unit", "basis", "scope", "share_basis"))
            if key in seen and seen[key][1] != f["value"]:
                out.append(dict(status="FAIL", object_id=fid, check_id="fact_identity",
                                reason="same typed identity has conflicting values", refs=[seen[key][0], fid]))
            seen[key] = (fid, f["value"])
        for c in run.claims:
            Claim.from_dict(c)
            a = c["assertion"]
            if a["kind"] == "event":
                event = run.events.get(a.get("event_ref"))
                if event is None or a.get("is_aggregate") != event.get("is_aggregate"):
                    out.append(dict(status="FAIL", claim_id=c["claim_id"], check_id="event_binding",
                                    reason="structured event binding mismatch"))
        as_of = date.fromisoformat(run.manifest["as_of"])
        for ref, source in sources.items():
            # Only explicit observation/publication metadata, never names or
            # prose, determines whether a source existed at analysis time.
            observed = source.get("effective_date") or source.get("published_or_filing_time")
            if observed:
                try:
                    if date.fromisoformat(observed[:10]) > as_of:
                        out.append(dict(status="FAIL", object_id=ref, check_id="as_of",
                                        reason="source observation is after analysis date"))
                except (ValueError, TypeError):
                    out.append(dict(status="FAIL", object_id=ref, check_id="date_schema",
                                    reason="invalid explicit source observation date"))
        member_totals = {}
        for e in run.events.values():
            aggregate_ref = e.get("aggregate_ref")
            if aggregate_ref:
                member_totals[aggregate_ref] = member_totals.get(aggregate_ref, 0) + e["value"]
        for aggregate_id, aggregate in run.events.items():
            if not aggregate["is_aggregate"]:
                continue
            if member_totals.get(aggregate_id, 0) > aggregate["value"]:
                out.append(dict(status="FAIL", object_id=aggregate_id, check_id="event_total",
                                reason="explicit member events exceed aggregate cap"))
        run.findings = out
        return out, any(f["status"] == "FAIL" for f in out)

    def register_events(self, run_id, events):
        """Add/update/remove a complete typed event catalog transactionally.

        Event membership is explicit via aggregate_ref, never inferred from
        similar dates, company names, or narrative phrases.
        """
        from .schema import DIMENSIONS
        candidate = {}
        sources = source_catalog(self.runs[run_id])
        for event in events:
            if not isinstance(event, dict):
                raise ValueError("event must be an object")
            for key in ("event_id", "date", "source_ref") + DIMENSIONS:
                if not isinstance(event.get(key), str) or not event[key]:
                    raise ValueError(f"event.{key} required")
            date.fromisoformat(event["date"])
            if type(event.get("is_aggregate")) is not bool:
                raise ValueError("event.is_aggregate must be boolean")
            if type(event.get("value")) not in (int, float) or event["value"] < 0:
                raise ValueError("event.value must be nonnegative numeric quantity")
            if event["source_ref"] not in sources or event["source_ref"] in self.runs[run_id].events:
                raise ValueError("event must cite an existing fact/evidence/calc source")
            eid = event["event_id"]
            if eid in candidate or eid in sources and eid not in self.runs[run_id].events:
                raise ValueError("duplicate or ambiguous event_id")
            candidate[eid] = deepcopy(event)
        for eid, event in candidate.items():
            aggregate_ref = event.get("aggregate_ref")
            if aggregate_ref:
                aggregate = candidate.get(aggregate_ref)
                if event["is_aggregate"] or not aggregate or not aggregate["is_aggregate"]:
                    raise ValueError("invalid aggregate_ref")
                for dim in ("entity", "concept", "scope", "unit", "basis"):
                    if aggregate[dim] != event[dim]:
                        raise ValueError("event member and aggregate dimensions differ")
        digest(candidate)
        self.runs[run_id].events = candidate

    def gate(self, run_id, stage):
        _, blocked = self.validate_stage(run_id, stage)
        self.runs[run_id].stage = f"blocked:{stage}" if blocked else stage
        return blocked

    def snapshot(self, run_id):
        snap = super().snapshot(run_id)
        snap["events"] = self.runs[run_id].events
        snap["manifest"] = self.runs[run_id].manifest
        snap["trade_generated"] = getattr(self.runs[run_id], "trade_generated", ([], []))
        return deepcopy(snap)

    def validate_final(self, run_id):
        from .reporting import render_report
        findings, blocked = self.validate_stage(run_id, "final")
        return findings, blocked, render_report(self.snapshot(run_id))

    def patch(self, run_id, patches, allowed_ids):
        """Validate the entire candidate before publishing any changed state."""
        run = self.runs[run_id]
        original = deepcopy(run.claims)
        original_index = claim_index(original)
        index = deepcopy(original_index)
        if not isinstance(patches, list) or not patches:
            raise ValueError("nonempty claim patches required")
        changed = []
        for p in patches:
            if not isinstance(p, dict):
                raise ValueError("patch must be an object")
            cid = p.get("claim_id")
            if cid not in allowed_ids or cid not in index or cid in changed:
                raise ValueError("unauthorized, missing, or duplicate patch claim_id")
            candidate = {**index[cid], **deepcopy(p)}
            if candidate["author_role"] != index[cid]["author_role"]:
                raise ValueError("patch cannot change author_role")
            index[cid] = Claim.from_dict(candidate).to_dict()
            changed.append(cid)
        changed = [cid for cid in changed if index[cid] != original_index[cid]]
        if not changed:
            return []
        candidate = list(index.values())
        affected = set(descendants(original, changed)) | set(descendants(candidate, changed))
        # Reuse exactly the same source/schema contracts as initial ingestion.
        shadow = deepcopy(run)
        trade_claims = [c for c in candidate if c["assertion"]["kind"] == "trade_plan"]
        if len(trade_claims) != (1 if any(c["author_role"] == "trader" for c in candidate) else 0):
            raise ValueError("patch must preserve the canonical trade_plan Claim")
        shadow.claims, shadow.analyses = [], {}
        self.runs["__patch_candidate__"] = shadow
        try:
            if trade_claims:
                self.register_trade("__patch_candidate__", trade_claims[0]["assertion"]["trade"])
            grouped = {}
            for c in candidate:
                grouped.setdefault(c["author_role"], []).append(c)
            self.submit_batch("__patch_candidate__", grouped)
        finally:
            del self.runs["__patch_candidate__"]
        run.claims = candidate
        run.trade, run.fact_store, run.calc_store = shadow.trade, shadow.fact_store, shadow.calc_store
        run.trade_generated = getattr(shadow, "trade_generated", ([], []))
        self._sync_views(run)
        return [cid for cid in index if cid in affected]
