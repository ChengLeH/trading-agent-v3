"""V4 canonical objects and graph invariants. Never interpret prose."""
from copy import deepcopy
from dataclasses import dataclass, asdict
import hashlib
import json

DIMENSIONS = ("entity", "concept", "period", "scope", "unit", "basis")
CLAIM_TYPES = {"source_fact", "derived_fact", "semantic_fact", "interpretation",
               "forecast", "trade_assumption"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def id_list(value, name):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
        raise ValueError(f"{name} must be a list of nonempty IDs")
    if len(set(value)) != len(value):
        raise ValueError(f"duplicate IDs in {name}")
    return list(value)


@dataclass
class Claim:
    claim_id: str
    text: str
    claim_type: str
    refs: list
    fact_refs: list
    dependencies: list
    author_role: str
    assertion: dict
    bindings: list
    confidence: float = 0.0
    subsection: str = ""
    purpose: str = ""

    @classmethod
    def from_dict(cls, raw):
        if not isinstance(raw, dict):
            raise ValueError("claim must be an object")
        required = ("claim_id", "text", "claim_type", "refs", "fact_refs",
                    "dependencies", "author_role", "assertion", "bindings")
        if any(k not in raw for k in required):
            raise ValueError(f"claim missing required fields: {set(required) - raw.keys()}")
        if set(raw) - cls.__dataclass_fields__.keys():
            raise ValueError("unknown claim fields")
        for k in ("claim_id", "text", "author_role"):
            if not isinstance(raw[k], str) or not raw[k].strip():
                raise ValueError(f"claim {k} must be nonempty text")
        if raw["claim_type"] not in CLAIM_TYPES:
            raise ValueError("unsupported claim_type")
        for k in ("refs", "fact_refs", "dependencies"):
            id_list(raw[k], k)
        if not set(raw["fact_refs"]) <= set(raw["refs"]):
            raise ValueError("fact_refs must be included in refs")
        if not isinstance(raw["assertion"], dict) or not isinstance(raw["assertion"].get("kind"), str):
            raise ValueError("assertion.kind required")
        if not raw["assertion"]["kind"]:
            raise ValueError("assertion.kind must be nonempty")
        assertion = raw["assertion"]
        if 'catalog_scope' in assertion and assertion['catalog_scope'] not in ('referenced_sources', 'full_catalog'):
            raise ValueError('assertion.catalog_scope must be referenced_sources or full_catalog')
        if assertion["kind"] == "value" and (assertion.get("ref_id") not in raw["refs"] or "value" not in assertion):
            raise ValueError("value assertion requires a declared ref_id and value")
        if assertion["kind"] == "event" and (assertion.get("event_ref") not in raw["refs"] or type(assertion.get("is_aggregate")) is not bool):
            raise ValueError("event assertion requires declared event_ref and boolean is_aggregate")
        if assertion["kind"] == "trade_plan" and (raw["author_role"] != "trader" or not isinstance(assertion.get("trade"), dict)):
            raise ValueError("only trader may author a structured trade_plan assertion")
        if not isinstance(raw["bindings"], list):
            raise ValueError("bindings must be a list")
        for binding in raw["bindings"]:
            if not isinstance(binding, dict):
                raise ValueError("binding must be an object")
            for key in ("ref_id",) + DIMENSIONS:
                if not isinstance(binding.get(key), str) or not binding[key]:
                    raise ValueError(f"binding.{key} required; use explicit unknown when unavailable")
            if binding["ref_id"] not in raw["refs"]:
                raise ValueError("binding ref must be declared in refs")
        if isinstance(raw.get("confidence", 0), bool) or not isinstance(raw.get("confidence", 0), (int, float)):
            raise ValueError("confidence must be numeric")
        if not 0 <= raw.get("confidence", 0) <= 1:
            raise ValueError("confidence outside [0,1]")
        for key in ("subsection", "purpose"):
            if not isinstance(raw.get(key, ""), str):
                raise ValueError(f"{key} must be text")
        digest(raw)  # reject non-JSON/non-finite values before canonical storage
        return cls(**deepcopy(raw))

    def to_dict(self):
        return asdict(self)


def claim_index(claims):
    index = {}
    for c in claims:
        cid = c.get("claim_id") if isinstance(c, dict) else None
        if not isinstance(cid, str) or not cid:
            raise ValueError("every claim requires claim_id")
        if cid in index:
            raise ValueError(f"duplicate claim_id: {cid}")
        index[cid] = c
    for cid, c in index.items():
        deps = id_list(c.get("dependencies", []), "dependencies")
        refs = id_list(c.get("refs", []), "refs")
        if not set(refs).intersection(index) <= set(deps):
            raise ValueError(f"{cid}: claim refs must also be declared dependencies")
        if set(deps) - index.keys():
            raise ValueError(f"{cid}: missing dependency")
    visiting, done = set(), set()

    def visit(cid):
        if cid in visiting:
            raise ValueError("claim dependency cycle")
        if cid in done:
            return
        visiting.add(cid)
        for dep in index[cid].get("dependencies", []):
            visit(dep)
        visiting.remove(cid)
        done.add(cid)

    for cid in index:
        visit(cid)
    return index


def descendants(claims, seeds):
    index = claim_index(claims)
    affected = set(seeds)
    if affected - index.keys():
        raise ValueError("unknown changed claim")
    children = {cid: [] for cid in index}
    for cid, c in index.items():
        for dep in c.get("dependencies", []):
            children[dep].append(cid)
    pending = list(affected)
    while pending:
        for child in children[pending.pop()]:
            if child not in affected:
                affected.add(child)
                pending.append(child)
    return [cid for cid in index if cid in affected]
