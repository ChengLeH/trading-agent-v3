"""Explicit trade horizons and conditional losses; never a prose risk classifier.

Reference existence is not proof of a catalyst or a guarantee. The normal
per-Claim and global semantic reviews must still assess those premises.
"""
from datetime import date

from .precision import number, validate_calculation_value
from .schema import id_list

TRADE_EVIDENCE_CONTRACT = 'trade-evidence/1'
TRADE_EVIDENCE_INSTRUCTIONS = '''
新交易方案填 evidence_contract="trade-evidence/1"，並提供：
target_basis={use:trade_target|valuation_context|unknown,source_refs:[],bridge_refs:[]}；
source_horizon={start:ISO日期或null,end:ISO日期} 或 null（未知），trade_valid_until=ISO日期或null；
execution_assumption={order_type:已知委託方式或unknown,assumed_fill_price:數字或null,fill_guaranteed:false}。
source_refs/bridge_refs 只能用已提供的來源或上游 Claim ID，並列入交易 Claim 的 refs/dependencies。
source_horizon 是預測本身的期限，不是文章發布日。長期目標僅作 valuation_context 時，不可因此把它設為短期 target；
若其作 trade_target 且晚於交易期限，必須提供 bridge_refs，由既有語義驗證核對短期可達性的證據；存在引用不代表理由成立。
planned_stop_loss={value:非負數,unit:portfolio_pct} 是假設 assumed_fill_price 成交下的計畫損失，
gap_stress_loss={value:非負數,unit:portfolio_pct,scenario_return_pct:帶方向的報酬率} 是特定情境損失，兩者可留 null。
上述 value 若採捨入結果，另附 output_rounding={decimal_places:位數,rounding:half_up|half_even}；不能用任意 tolerance 放過錯算。
maximum_loss_bound 未獲證明必須是 null；若宣稱有上限，填 {value,unit:portfolio_pct,proof_refs:[]}，
由既有語義驗證核對完整前提及適用範圍。歷史最大跳空、ATR、停損指令或預計成交價均不是損失上限證明。
宣稱 fill_guaranteed=true 時必須另附 guarantee_refs，驗證員仍須核對保證是否確實存在及適用；程式不認證交易可成交。
未知維度明寫 null/unknown，不能捏造日期、滑價、催化劑或使用者風險預算；WAIT 不必捏造上述交易條件。
'''.strip()


def _day(value, label):
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f'trade.{label} requires an ISO date or null') from None


def trade_evidence_refs(trade):
    result = set()
    for field, keys in (('target_basis', ('source_refs', 'bridge_refs')),
                        ('execution_assumption', ('guarantee_refs',)),
                        ('maximum_loss_bound', ('proof_refs',))):
        row = trade.get(field)
        if isinstance(row, dict):
            for key in keys:
                result.update(id_list(row.get(key, []), f'trade.{field}.{key}'))
    return result


def validate_trade_evidence(trade, known_refs):
    """Validate supplied typed premises. Legacy plans remain explicitly unknown."""
    if trade.get('direction') == 'WAIT':
        return
    contract = trade.get('evidence_contract')
    if contract is not None and contract != TRADE_EVIDENCE_CONTRACT:
        raise ValueError('unsupported trade.evidence_contract')
    if contract:
        for key in ('target_basis', 'source_horizon', 'trade_valid_until', 'execution_assumption'):
            if key not in trade:
                raise ValueError(f'trade.{key} required; explicit unknown is allowed')

    def references(row, key, *, required=False):
        refs = id_list(row.get(key, []), f'trade.{key}')
        if required and not refs:
            raise ValueError(f'trade.{key} must cite the claimed premise')
        if set(refs) - set(known_refs):
            raise ValueError(f'trade.{key}: unknown source or Claim ref')
        return refs

    valid_until = _day(trade.get('trade_valid_until'), 'trade_valid_until')
    horizon = trade.get('source_horizon')
    end = None
    if horizon is not None:
        if not isinstance(horizon, dict) or 'end' not in horizon:
            raise ValueError('trade.source_horizon requires start/end dates or null')
        start = _day(horizon.get('start'), 'source_horizon.start')
        end = _day(horizon.get('end'), 'source_horizon.end')
        if start and end and start > end:
            raise ValueError('trade.source_horizon start is after end')
    basis = trade.get('target_basis')
    if basis is not None:
        if not isinstance(basis, dict) or basis.get('use') not in {'trade_target', 'valuation_context', 'unknown'}:
            raise ValueError('trade.target_basis requires an explicit use')
        references(basis, 'bridge_refs', required=bool(
            basis['use'] == 'trade_target' and end and valid_until and end > valid_until))
        references(basis, 'source_refs')
    execution = trade.get('execution_assumption')
    if execution is not None:
        if not isinstance(execution, dict) or type(execution.get('fill_guaranteed')) is not bool:
            raise ValueError('trade.execution_assumption requires boolean fill_guaranteed')
        fill = execution.get('assumed_fill_price')
        if fill is not None and number(fill) <= 0:
            raise ValueError('trade.execution_assumption.assumed_fill_price must be positive')
        references(execution, 'guarantee_refs', required=execution['fill_guaranteed'])
    else:
        fill = None
    for key in ('planned_stop_loss', 'gap_stress_loss', 'maximum_loss_bound'):
        row = trade.get(key)
        if row is None:
            continue
        if not isinstance(row, dict) or row.get('unit') != 'portfolio_pct' or number(row.get('value')) < 0:
            raise ValueError(f'trade.{key} requires nonnegative value in portfolio_pct')
        expected = None
        if key == 'planned_stop_loss' and fill is not None:
            entry, weight = number(trade['entry']), number(trade['position_pct'])
            loss = (entry - number(fill)) if trade['direction'] == 'BUY' else (number(fill) - entry)
            expected = max(number(0), loss / entry * weight)
        if key == 'gap_stress_loss':
            move = number(row.get('scenario_return_pct'))
            adverse = -move if trade['direction'] == 'BUY' else move
            expected = max(number(0), adverse * number(trade['position_pct']) / 100)
        if key == 'maximum_loss_bound':
            references(row, 'proof_refs', required=True)
        if expected is not None:
            try:
                validate_calculation_value(float(expected), row['value'], row)
            except ValueError as exc:
                raise ValueError(f'trade.{key}: {exc}') from None


def trade_evidence_findings(trade):
    """Surface legacy/unknown execution premises without inventing their values."""
    if not trade or trade.get('direction') == 'WAIT':
        return []
    unknown = [key for key in ('target_basis', 'source_horizon', 'trade_valid_until', 'execution_assumption')
               if trade.get(key) is None]
    return ([dict(status='WARN', check_id='trade_evidence_unknown', object_id='trade',
                 missing_dimensions=unknown,
                 reason='Trade horizon or execution premises are unknown; semantic verification must assess the limits.')]
        if unknown else [])
