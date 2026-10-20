"""Typed representation rules, never financial prose classification.

Raw source values remain canonical. Display rounding is explicit and cannot
forgive a different formula, entity, period, accounting basis or currency.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_HALF_EVEN
import math
from datetime import date


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError('finite JSON number required')
    return Decimal(str(value))


def unit(value):
    text = value.strip()
    scales = {'thousand': Decimal(1000), 'million': Decimal(1000000), 'billion': Decimal(1000000000)}
    pieces = text.split()
    if len(pieces) == 2 and len(pieces[0]) == 3 and pieces[0].isupper() and pieces[1] in scales:
        return pieces[0], scales[pieces[1]]
    if len(text) == 3 and text.isascii() and text.isupper() and text not in {'PCT'}:
        return text, Decimal(1)
    if text.casefold() == 'shares':
        return 'shares', Decimal(1)
    ratios = {'RATIO': Decimal(1), 'ratio': Decimal(1), 'PCT': Decimal('.01'),
              'PERCENT': Decimal('.01'), '%': Decimal('.01')}
    if text in ratios:
        return 'ratio', ratios[text]
    return text, Decimal(1)


def known_unit(value):
    text = value.strip()
    currency = text.split()[0] if text.split() else ''
    if len(currency) == 3 and currency.isascii() and currency.isalpha() and currency.isupper():
        return len(text.split()) == 1 or text.split()[1:] in [['thousand'], ['million'], ['billion']]
    if text.endswith('_PER_SHARE'):
        prefix = text.removesuffix('_PER_SHARE')
        return len(prefix) == 3 and prefix.isascii() and prefix.isalpha() and prefix.isupper()
    return text.casefold() == 'shares' or text in {'RATIO','ratio','PCT','PERCENT','%'}


def period(value):
    text = value.strip().upper()
    # Lexical grammar applies only to an explicit typed period field. No
    # dates or meanings are inferred from Claim prose or publication dates.
    fiscal = text.removeprefix('FY').strip(' -') if text.startswith('FY') else text.removesuffix('FY').strip(' -') if text.endswith('FY') else ''
    if len(fiscal) == 4 and fiscal.isascii() and fiscal.isdigit():
        return 'FY' + fiscal
    parts = text.replace('-', ' ').split()
    if len(parts) == 2:
        year, quarter = parts if len(parts[0]) == 4 else parts[::-1]
        if len(year) == 4 and year.isascii() and year.isdigit() and quarter in {'Q1','Q2','Q3','Q4'}:
            return year + '-' + quarter
    if len(text) == 4 and text.isascii() and text.isdigit():
        return text
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def equivalent(dimension, left, right):
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    if left == right:
        return True
    if dimension == 'period':
        a, b = period(left), period(right)
        return a == b if a is not None and b is not None else None
    if dimension == 'unit':
        a, b = unit(left), unit(right)
        if a == b:
            return True
        return False if known_unit(left) and known_unit(right) else None
    if dimension in {'scope', 'basis', 'share_basis'}:
        a, b = left.strip().casefold(), right.strip().casefold()
        if a == b:
            return True
        known = {'scope': {'parent','consolidated','single_company','combined','industry','segment'}, 'basis': {'gaap','non_gaap','ifrs'},
                 'share_basis': {'basic','diluted'}}[dimension]
        return False if a in known and b in known else None
    return None


def validate_presentation(assertion, source_value, source_unit):
    presentation = assertion.get('presentation')
    if presentation is None:
        return
    if (not isinstance(presentation, dict) or set(presentation) != {'value','unit','decimal_places','rounding'}
            or not isinstance(presentation['unit'], str)
            or type(presentation['decimal_places']) is not int or not 0 <= presentation['decimal_places'] <= 18
            or presentation['rounding'] not in {'half_up','half_even'}):
        raise ValueError('presentation requires value/unit/decimal_places(0..18)/rounding(half_up|half_even)')
    family, scale = unit(source_unit)
    target, target_scale = unit(presentation['unit'])
    if family != target:
        raise ValueError('presentation changes currency or dimension')
    expected = number(source_value) * scale / target_scale
    rounding = ROUND_HALF_UP if presentation['rounding'] == 'half_up' else ROUND_HALF_EVEN
    try:
        expected = expected.quantize(Decimal(1).scaleb(-presentation['decimal_places']), rounding=rounding)
        if number(presentation['value']) != expected:
            raise ValueError('presentation differs from explicit unit conversion and rounding')
    except InvalidOperation:
        raise ValueError('presentation precision is not representable') from None
