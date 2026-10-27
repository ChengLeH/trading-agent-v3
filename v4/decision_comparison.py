"""Structural decision comparison; semantic correctness remains an LLM task."""
from datetime import date
from .schema import id_list

DECISION_CONTRACT = 'decision-comparison/1'


def comparison_refs(comparison):
    if not isinstance(comparison, dict):
        return set()
    rows = [comparison.get('research_view'), comparison.get('wait')]
    rows.extend(comparison.get('alternatives', []) if isinstance(comparison.get('alternatives'), list) else [])
    result = set()
    for row in rows:
        if isinstance(row, dict):
            for field in ('refs', 'support_refs', 'opposition_refs'):
                result.update(id_list(row.get(field, []), f'decision_comparison.{field}'))
    return result


def _text(row, field):
    if not isinstance(row.get(field), str) or not row[field].strip():
        raise ValueError(f'decision_comparison.{field} requires a nonempty explanation')


def validate_comparison(trade, known_refs, *, required=False):
    comparison = trade.get('decision_comparison')
    if comparison is None:
        if required:
            raise ValueError('trade.decision_comparison required; unknown or unassessable alternatives are allowed')
        return
    if not isinstance(comparison, dict):
        raise ValueError('trade.decision_comparison must be an object')
    view = comparison.get('research_view')
    if not isinstance(view, dict) or view.get('direction') not in {'BULLISH', 'BEARISH', 'NEUTRAL', 'UNKNOWN'}:
        raise ValueError('decision_comparison.research_view requires a direction')
    _text(view, 'reason')
    if not {'horizon_end', 'refs'} <= set(view):
        raise ValueError('decision_comparison.research_view requires horizon_end and refs; unknown values may be null or empty')
    horizon = view.get('horizon_end')
    if horizon is not None:
        try:
            if date.fromisoformat(horizon).isoformat() != horizon:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValueError('research_view.horizon_end must be an ISO date or null') from None
    alternatives = comparison.get('alternatives')
    if not isinstance(alternatives, list) or len(alternatives) != 2:
        raise ValueError('decision_comparison requires one BUY and one SHORT alternative')
    seen = set()
    for row in alternatives:
        if not isinstance(row, dict) or row.get('direction') not in {'BUY', 'SHORT'} or row['direction'] in seen:
            raise ValueError('decision_comparison requires one BUY and one SHORT alternative')
        seen.add(row['direction'])
        if row.get('status') not in {'VIABLE', 'REJECTED', 'UNASSESSABLE', 'NOT_ALLOWED'}:
            raise ValueError('decision_comparison alternative status invalid')
        _text(row, 'reason')
        if not {'support_refs', 'opposition_refs', 'missing_information'} <= set(row):
            raise ValueError('decision_comparison alternative requires support_refs, opposition_refs and missing_information')
        for field in ('support_refs', 'opposition_refs'):
            id_list(row.get(field, []), f'decision_comparison.{field}')
        missing = row.get('missing_information', [])
        if not isinstance(missing, list) or any(not isinstance(x, str) or not x.strip() for x in missing):
            raise ValueError('decision_comparison.missing_information must be a list of strings')
    if trade.get('direction') == 'WAIT':
        wait = comparison.get('wait')
        if not isinstance(wait, dict) or wait.get('best_alternative') not in {'BUY', 'SHORT', 'UNKNOWN'}:
            raise ValueError('WAIT requires best_alternative BUY, SHORT or UNKNOWN')
        _text(wait, 'reason')
        _text(wait, 'reassessment_trigger')
        if 'refs' not in wait:
            raise ValueError('decision_comparison.wait requires refs; an empty list is allowed')
    unknown = comparison_refs(comparison) - set(known_refs)
    if unknown:
        raise ValueError('decision_comparison has unknown refs: ' + ', '.join(sorted(unknown)))
