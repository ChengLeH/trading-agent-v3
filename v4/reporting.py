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
    issuers = [{key: row[key] for key in ('ticker','cik','company_name','registry_origin')} for row in snapshot.get('issuer_identity', [])]
    issuers = list({json.dumps(row,sort_keys=True):row for row in issuers}.values())
    if issuers:
        lines.extend(['## 公司身份核對', '', '```json', json.dumps(issuers,ensure_ascii=False,indent=2), '```', ''])
    limitations = [line for row in snapshot.get('ingestion_diagnostics', [])
                   for line in row.get('limitations', [])]
    if limitations:
        lines.extend(['## 資料抽取限制', '', *('- ' + line for line in limitations), ''])
    methods = snapshot.get('indicator_definitions', {})
    if methods:
        lines.extend(['## 技術指標計算口徑', '', '```json',
                      json.dumps(methods,ensure_ascii=False,indent=2), '```', ''])
    report = "\n".join(lines)
    sidecar = dict(format="v4-claim-dag/1", run_id=snapshot["run_id"],
                   sections=sections, claims=claims, facts=snapshot["facts"],
                   calcs=snapshot["calcs"], evidence=snapshot["evidence"],
                   events=snapshot.get("events", {}), trade=snapshot.get("trade", {}),
                   source_declarations=snapshot.get('source_declarations', {}),
                   indicator_definitions=methods, issuer_identity=snapshot.get('issuer_identity', []), ingestion_diagnostics=snapshot.get('ingestion_diagnostics', []),
                   verification=dict(status='unverified', delivery_ready=False, global_review_complete=False),
                   report_hash=digest(report), total_claims=len(claims))
    return dict(report_markdown=report, sidecar=sidecar)
