"""Pure rendering from canonical objects; no prose classification or cleanup."""
import html
import json
from copy import deepcopy
from urllib.parse import urlsplit
from .schema import digest
from .review_diagnostics import diagnostics_index, REVIEW_CONTEXT_FORMAT


_MARKDOWN_ESCAPES = str.maketrans({char: "\\" + char for char in r"\`*_{}[]()#+-.!|"})


def _citation_field(value):
    """Normalize source metadata to one printable text line."""
    if not isinstance(value, str):
        return ""
    printable = "".join(ch for ch in value if ch.isprintable() or ch.isspace())
    return " ".join(printable.split())


def _citation_markdown(value):
    return html.escape(_citation_field(value), quote=True).translate(_MARKDOWN_ESCAPES)


def _http_url(value):
    if (not isinstance(value, str) or any(ch.isspace() or not ch.isprintable() for ch in value)
            or any(ch in value for ch in "<>\\")):
        return None
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        return None
    return value if (parsed.scheme.casefold() in {"http", "https"} and hostname
                     and not parsed.username and not parsed.password) else None


def _citation_index(snapshot):
    claims = {row["claim_id"]: row for row in snapshot.get("claims", [])}
    facts, calcs = snapshot.get("facts", {}), snapshot.get("calcs", {})
    evidence, events = snapshot.get("evidence", {}), snapshot.get("events", {})
    manifest = snapshot.get("manifest", {})
    manifest_sources = manifest.get("sources", []) if isinstance(manifest, dict) else []
    if not isinstance(manifest_sources, list):
        manifest_sources = []
    manifest_rows = [row for row in manifest_sources
                     if isinstance(row, dict) and isinstance(row.get("id"), str)
                     and row.get("id")]
    source_counts = {}
    for row in manifest_rows:
        source_counts[row["id"]] = source_counts.get(row["id"], 0) + 1
    # A citation must resolve to one manifest row. Keeping the last duplicate
    # silently attaches an arbitrary URL/title to facts from the same source ID.
    ambiguous_source_ids = {source_id for source_id, count in source_counts.items()
                            if count > 1}
    source_entries = {row["id"]: row for row in manifest_rows
                      if source_counts[row["id"]] == 1}
    declarations = snapshot.get("source_declarations", {})
    if not isinstance(declarations, dict):
        declarations = {}
    lineage = snapshot.get("source_lineage", {})
    if not isinstance(lineage, dict):
        lineage = {}
    entries, keys = [], {}
    claim_numbers = {}

    def lineage_rows(ref_ids):
        rows, seen = [], set()
        for ref_id in dict.fromkeys(value for value in ref_ids if isinstance(value, str) and value):
            annotation = lineage.get(ref_id)
            row = dict(ref_id=ref_id, status="declared" if isinstance(annotation, dict) else "unknown",
                       annotation=deepcopy(annotation) if isinstance(annotation, dict) else {})
            key = json.dumps(row, ensure_ascii=False, sort_keys=True)
            if key not in seen:
                rows.append(row)
                seen.add(key)
        return rows

    def add_source(source_id, *, url=None, span=None, pointer=None, published=None,
                   accession=None, source_type=None, title=None, lineage_ref_ids=()):
        lookup_source_id = source_id if isinstance(source_id, str) else ""
        display_source_id = _citation_field(lookup_source_id) or "unknown"
        span, pointer = _citation_field(span), _citation_field(pointer)
        published, accession = _citation_field(published), _citation_field(accession)
        source_type, title = _citation_field(source_type), _citation_field(title)
        url = _http_url(url)
        key = ("url", url, lookup_source_id, accession) if url else (
            "source", lookup_source_id, accession)
        if key not in keys:
            keys[key] = len(entries) + 1
            entries.append(dict(number=keys[key], source_id=display_source_id,
                url=url, source_type=source_type or "", title=title or "",
                spans=[], source_pointers=[], published_or_filing_time=published or "",
                accession_or_version=accession or "", source_lineage=[]))
        item = entries[keys[key] - 1]
        for row in lineage_rows([lookup_source_id, *lineage_ref_ids]):
            if row not in item["source_lineage"]:
                item["source_lineage"].append(row)
        for field, value in (("spans", span), ("source_pointers", pointer)):
            if value and value not in item[field]:
                item[field].append(value)
        if published and not item["published_or_filing_time"]:
            item["published_or_filing_time"] = published
        if accession and not item["accession_or_version"]:
            item["accession_or_version"] = accession
        return keys[key]

    def resolve(ref, visited):
        if ref in visited:
            return []
        visited.add(ref)
        if ref in claims:
            claim = claims[ref]
            numbers = []
            for nested in list(claim.get("refs", [])) + list(claim.get("fact_refs", [])) + list(claim.get("dependencies", [])):
                numbers.extend(resolve(nested, visited))
            return numbers
        if ref in calcs:
            numbers = []
            for nested in calcs[ref].get("input_refs", []):
                numbers.extend(resolve(nested, visited))
            return numbers
        if ref in facts:
            fact = facts[ref]
            source_id = fact.get("source_id")
            source_id = source_id if isinstance(source_id, str) else ""
            source = source_entries.get(source_id, {})
            declaration = ({} if source_id in ambiguous_source_ids
                           else declarations.get(source_id, {}))
            if not isinstance(declaration, dict):
                declaration = {}
            url = (source.get("url") or source.get("source_url") or
                   declaration.get("url") or declaration.get("source_url"))
            return [add_source(source_id, url=url, pointer=fact.get("source_pointer"),
                published=fact.get("published_or_filing_time"),
                accession=fact.get("accession_or_version"), source_type=source.get("type"),
                title=source.get("title") or source.get("name"))]
        if ref in evidence:
            item = evidence[ref]
            source_id = item.get("source_id")
            source_id = source_id if isinstance(source_id, str) else ""
            source = source_entries.get(source_id, {})
            span = item.get("source_span", "")
            url = _http_url(span) or source.get("url") or source.get("source_url")
            return [add_source(source_id, url=url, span=span,
                published=item.get("published_or_filing_time"),
                source_type=item.get("source_type") or source.get("type"),
                title=source.get("title") or source.get("name"), lineage_ref_ids=[ref])]
        if ref in events:
            source_ref = events[ref].get("source_ref")
            return resolve(source_ref, visited) if source_ref else []
        return []

    for claim in snapshot.get("claims", []):
        numbers = []
        for ref in list(claim.get("refs", [])) + list(claim.get("fact_refs", [])) + list(claim.get("dependencies", [])):
            numbers.extend(resolve(ref, set()))
        claim_numbers[claim["claim_id"]] = sorted(set(numbers))
    return entries, claim_numbers


def render_report(snapshot):
    claims = snapshot["claims"]
    citations, claim_citations = _citation_index(snapshot)
    lines = [f"# {snapshot['ticker']} 投資分析（{snapshot.get('as_of', '')}）", ""]
    mandate = snapshot.get('manifest', {}).get('analysis_mandate')
    if mandate is not None:
        lines.extend(['## 分析期限與歷史資料範圍', '', '```json',
                      json.dumps(mandate, ensure_ascii=False, indent=2), '```', ''])
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
            refs = claim_citations.get(c["claim_id"], [])
            citation_text = ("來源：" + ", ".join(f"[{number}]" for number in refs)
                             if refs else "來源尚未連結")
            lines.extend([f"<!-- claim_id: {c['claim_id']} -->", c["text"], citation_text, ""])
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
    if citations:
        lines.extend(["## 可追溯來源", ""])
        for entry in citations:
            label = entry.get("title") or entry.get("source_id") or "來源"
            safe_label = _citation_markdown(label)
            url = entry.get("url")
            rendered = f"<{url}>" if url else f"{safe_label}（未提供外部網址）"
            details = [f"source_id={_citation_markdown(entry['source_id'])}"]
            if entry.get("source_type"):
                details.append(f"type={_citation_markdown(entry['source_type'])}")
            if entry.get("source_pointers"):
                details.append("位置=" + ", ".join(map(_citation_markdown, entry["source_pointers"])))
            if entry.get("spans"):
                details.append("定位=" + ", ".join(map(_citation_markdown, entry["spans"])))
            if entry.get("published_or_filing_time"):
                details.append("日期=" + _citation_markdown(entry["published_or_filing_time"]))
            if entry.get("accession_or_version"):
                details.append("版本=" + _citation_markdown(entry["accession_or_version"]))
            for relation in entry.get("source_lineage", []):
                ref_label = _citation_markdown(relation.get("ref_id", "unknown"))
                annotation = relation.get("annotation", {})
                if relation.get("status") != "declared":
                    details.append(f"來源沿革未知(ref={ref_label})")
                    continue
                declared = []
                for field, label in (("origin_id", "原始文件"), ("derived_from", "明示轉引"),
                                     ("publisher", "發布者"), ("reason", "註記理由")):
                    value = annotation.get(field)
                    if isinstance(value, list):
                        value = ", ".join(_citation_markdown(item) for item in value)
                    elif isinstance(value, str):
                        value = _citation_markdown(value)
                    if value:
                        declared.append(f"{label}={value}")
                details.append("來源沿革為資料註記、未獨立驗證(ref=" + ref_label + "): "
                               + ("; ".join(declared) if declared else "僅有未驗證註記"))
            lines.append(f"[{entry['number']}] {rendered} — " + "; ".join(details))
        lines.append("")
    report = "\n".join(lines)
    sidecar = dict(format="v4-claim-dag/1", run_id=snapshot["run_id"],
                   sections=sections, claims=claims, facts=snapshot["facts"],
                   calcs=snapshot["calcs"], evidence=snapshot["evidence"],
                   events=snapshot.get("events", {}), trade=snapshot.get("trade", {}),
                   source_declarations=snapshot.get('source_declarations', {}),
                   source_lineage=deepcopy(snapshot.get("source_lineage", {})),
                   citations=citations, claim_citations=claim_citations,
                   indicator_definitions=methods, issuer_identity=snapshot.get('issuer_identity', []), ingestion_diagnostics=snapshot.get('ingestion_diagnostics', []),
                   verification=dict(status='unverified', delivery_ready=False, global_review_complete=False),
                   report_hash=digest(report), total_claims=len(claims))
    if mandate is not None:
        sidecar['analysis_mandate'] = deepcopy(mandate)
    index = diagnostics_index(snapshot.get('ingestion_diagnostics', []))
    sidecar.update(diagnostics_sha256=index['sha256'], diagnostics_mode='summary+relevant',
                   diagnostics_index=index, review_context_format=REVIEW_CONTEXT_FORMAT)
    return dict(report_markdown=report, sidecar=sidecar)
