"""Pure rendering from canonical objects; no prose classification or cleanup."""
import json
from .schema import digest


def render_report(snapshot):
    claims = snapshot["claims"]
    lines = [f"# {snapshot['ticker']} 投資分析（{snapshot.get('as_of', '')}）", ""]
    sections = {}
    by_role = {}
    for c in claims:
        role = c["author_role"]
        by_role.setdefault(role, []).append(c)
        if role not in sections:
            sections[role] = {"claim_ids": [], "refs": []}
    for role, section in sections.items():
        lines.extend([f"## {role}", ""])
        for c in by_role[role]:
            section["claim_ids"].append(c["claim_id"])
            section["refs"].extend(c["refs"])
            if c.get("subsection"):
                lines.extend([f"### {c['subsection']}", ""])
            lines.extend([f"<!-- claim_id: {c['claim_id']} -->", c["text"], ""])
    if snapshot.get("trade"):
        lines.extend(["## 結構化交易計畫", "", "```json",
                      json.dumps(snapshot["trade"], ensure_ascii=False, indent=2), "```", ""])
    report = "\n".join(lines)
    sidecar = dict(format="v4-claim-dag/1", run_id=snapshot["run_id"],
                   sections=sections, claims=claims, facts=snapshot["facts"],
                   calcs=snapshot["calcs"], evidence=snapshot["evidence"],
                   events=snapshot.get("events", {}), trade=snapshot.get("trade", {}),
                   report_hash=digest(report), total_claims=len(claims))
    return dict(report_markdown=report, sidecar=sidecar)
