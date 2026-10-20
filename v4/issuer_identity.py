"""Verify declared issuer CIK against an independent SEC ticker registry.

No ticker-specific rules. Offline registries are explicit operator-supplied
SEC snapshots, never silently described as authenticated network downloads.
"""
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from .schema import digest
import urllib.request

SEC_TICKERS_URL = 'https://www.sec.gov/files/company_tickers.json'


class IssuerIdentityError(ValueError):
    def __init__(self, finding):
        self.finding = finding
        super().__init__(finding['reason'])

    def details(self):
        return dict(issuer_validation=self.finding, next_action='provide_SEC_issuer_registry_or_correct_subject')


def cik_id(value):
    text = str(value).strip()
    if not text.isascii() or not text.isdigit() or int(text) <= 0:
        raise ValueError('CIK must be a positive numeric issuer identifier')
    return str(int(text))


@lru_cache(maxsize=2)
def download_registry(day):
    request = urllib.request.Request(SEC_TICKERS_URL, headers={'User-Agent':os.environ.get('V4_SEC_USER_AGENT','TradingAgentV4 issuer-verification'), 'Accept':'application/json'})
    with urllib.request.urlopen(request, timeout=15) as response:
        if response.geturl() != SEC_TICKERS_URL:
            raise ValueError('SEC registry redirected to an unexpected URL')
        return response.read()


def registry_data(path=None):
    raw = Path(path).read_bytes() if path else download_registry(datetime.now(timezone.utc).date().isoformat())
    data = json.loads(raw)
    if not isinstance(data, dict) or not data:
        raise ValueError('SEC ticker registry must be a nonempty object')
    for row in data.values():
        if not isinstance(row, dict) or not isinstance(row.get('ticker'), str) or not row.get('title'):
            raise ValueError('SEC registry row requires ticker/title/cik_str')
        cik_id(row.get('cik_str'))
    return data, hashlib.sha256(raw).hexdigest()


def verify_issuer(run):
    subject = run.manifest.get('subject', {})
    declared = subject.get('cik')
    if declared in (None, ''):
        return []  # no CIK assertion exists to certify; payload checks remain
    records = [rec for rec in run.records if hasattr(rec, 'source_hash')]
    if not records:
        records = [SimpleNamespace(id='manifest', source_hash=digest(subject),
                                   subject_cik=declared, meta={})]
    ticker = subject.get('ticker', '').upper()
    path = run.manifest.get('issuer_registry_path') or os.environ.get('V4_SEC_ISSUER_REGISTRY')
    proof_key = dict(ticker=ticker, cik=str(declared), source_hashes={rec.id:rec.source_hash for rec in records})
    # Live lookups are persisted with the source records and reusable that UTC
    # day. Explicit local snapshot changes are re-read instead of bypassed.
    day = datetime.now(timezone.utc).date().isoformat()
    if not path and all(getattr(rec, 'meta', {}).get('issuer_identity', {}).get('request') == proof_key
                        and rec.meta['issuer_identity'].get('verified_day') == day
                        and rec.meta['issuer_identity'].get('status') == 'PASS'
                        and rec.meta['issuer_identity'].get('registry_origin') == SEC_TICKERS_URL for rec in records):
        return [dict(rec.meta['issuer_identity']) for rec in records]
    try:
        cik = cik_id(declared)
        registry, registry_hash = registry_data(path)
        matches = [row for row in registry.values() if row['ticker'].upper() == ticker]
        if len(matches) != 1:
            raise ValueError('declared ticker does not resolve uniquely in the SEC registry')
        row = matches[0]
        if cik_id(row['cik_str']) != cik:
            raise ValueError(f"SEC issuer mismatch: {ticker} resolves to CIK {cik_id(row['cik_str'])} ({row['title']}), declared CIK {cik}")
        if any(getattr(rec, 'subject_cik', None) not in (None, '') and
               cik_id(rec.subject_cik) != cik for rec in records):
            raise ValueError('source record CIK differs from the verified manifest issuer')
    except (ValueError, OSError, TypeError, KeyError) as exc:
        return [dict(status='FAIL',check_id='issuer_identity',object_id=rec.id,
                     reason=f'issuer verification unavailable or mismatched: {exc}',
                     next_action='provide_SEC_issuer_registry_or_correct_subject') for rec in records]
    results = []
    for rec in records:
        if not hasattr(rec, 'meta'): rec.meta = {}
        finding = dict(status='PASS',check_id='issuer_identity',object_id=rec.id,
            reason='declared ticker/CIK match independent SEC issuer registry',
            ticker=ticker,cik=cik,company_name=row['title'],registry_sha256=registry_hash,
            registry_origin='operator_supplied_SEC_snapshot' if path else SEC_TICKERS_URL,
            request=proof_key,verified_day=day)
        rec.meta['issuer_identity'] = finding
        results.append(dict(finding))
    return results
