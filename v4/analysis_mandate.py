"""Explicit analysis and historical-source windows, without inferred defaults."""
from calendar import monthrange
from datetime import date


def _date(value, name):
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f'{name} must be an ISO date') from None
    if parsed.isoformat() != value:
        raise ValueError(f'{name} must be an ISO date')
    return parsed


def _shift_months(day, count):
    index = day.year * 12 + day.month - 1 + count
    year, month0 = divmod(index, 12)
    if year < 1 or year > 9999:
        raise ValueError('date outside supported range')
    month = month0 + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def normalize_mandate(value, as_of):
    if not isinstance(value, dict):
        raise ValueError('analysis_mandate must be an object')
    allowed = {'analysis_end', 'history_start', 'objective'}
    if set(value) - allowed:
        raise ValueError('unknown analysis_mandate fields')
    today = _date(as_of, 'as_of')
    out = dict(value)
    if out.get('analysis_end') is not None and _date(out['analysis_end'], 'analysis_end') < today:
        raise ValueError('analysis_end must not precede as_of')
    if out.get('history_start') is not None and _date(out['history_start'], 'history_start') > today:
        raise ValueError('history_start must not follow as_of')
    if out.get('objective') is not None and (not isinstance(out['objective'], str) or not out['objective'].strip()):
        raise ValueError('objective must be a nonempty string or null')
    return out


def plan_data(as_of, *, lookback_months=None, horizon_months=None, objective=None):
    anchor = _date(as_of, 'as_of')
    for name, count in (('lookback_months', lookback_months), ('horizon_months', horizon_months)):
        if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count <= 0):
            raise ValueError(f'{name} must be a positive integer')
    mandate = normalize_mandate({
        'history_start': _shift_months(anchor, -lookback_months).isoformat() if lookback_months else None,
        'analysis_end': _shift_months(anchor, horizon_months).isoformat() if horizon_months else None,
        'objective': objective,
    }, as_of)
    return {'as_of': as_of, 'analysis_mandate': mandate,
            'market_history': {'start_inclusive': mandate['history_start'], 'end_inclusive': as_of},
            'acquisition': 'The host agent must select and fetch sources; provider date-boundary semantics may differ.',
            'limitations': 'Short history may prevent long-window indicators. Do not invent or silently expand the requested range.'}
