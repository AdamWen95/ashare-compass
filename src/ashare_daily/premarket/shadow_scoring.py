"""Pure, preregistered setup research. Scores compare only within a setup family."""
from decimal import Context, Decimal, ROUND_HALF_UP, localcontext

from ..reference_indicators import _parse
from .contracts import decimal

VERSION = 'premarket-setup-score-v1'
LABELS = {'trend_continuation': '趋势延续', 'strong_pullback': '强势整理',
          'rebound_watch': '超跌修复观察', 'unconfirmed': '形态尚未确认', 'unavailable': '资料不足'}
RISK_LABELS = {'extended': '价格偏离MA20超过2个ATR，等待位置改善',
    'high_volatility': '日均真实波幅超过收盘价5%，波动较大',
    'down_on_volume': '最新交易日放量下跌，需核对承接与风险',
    'repair_is_not_reversal': '仅确认近期修复，尚不能视为趋势反转'}
DIMENSION_LABELS = {'structure': '结构 / 30', 'relative_strength': '相对强弱 / 30',
                    'price_volume': '量价配合 / 20', 'position': '位置与波动 / 20'}


def _bounded(value, high):
    return max(Decimal(0), min(Decimal(high), value))


def _integer(value):
    return int(Decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def score_setup(bars, *, relative_return_20, excess_sector_pp):
    result = {'rule_version': VERSION, 'status': 'unavailable', 'setup': 'unavailable',
        'label': LABELS['unavailable'], 'score': None, 'dimensions': {}, 'facts': {},
        'risk_flags': [], 'confirmation': 'unavailable', 'conditions': [], 'issues': []}
    try:
        _, c, h, low, v = _parse(bars)
        relative = decimal(relative_return_20) * 100
        excess = decimal(excess_sector_pp)
        with localcontext(Context(prec=40)):
            facts = _facts(c, h, low, v, relative, excess)
            result.update(_classify(facts))
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        result['issues'] = ['历史或比较数据不可用：' + type(exc).__name__]
    return result


def _facts(c, h, low, v, relative, excess):
    ma5, ma20, ma60 = (sum(c[-n:]) / n for n in (5, 20, 60))
    slope = ma20 - sum(c[-25:-5]) / 20
    atr = sum(max(h[i]-low[i], abs(h[i]-c[i-1]), abs(low[i]-c[i-1]))
              for i in range(len(c)-14, len(c))) / 14
    volume_mean = sum(v[-21:-1]) / 20
    if atr <= 0 or volume_mean <= 0:
        raise ValueError('shadow_zero_range_or_volume')
    return {'close': c[-1], 'ma5': ma5, 'ma20': ma20, 'ma60': ma60, 'ma20_slope5': slope,
        'return1_pct': (c[-1]/c[-2]-1)*100, 'return3_pct': (c[-1]/c[-4]-1)*100,
        'return5_pct': (c[-1]/c[-6]-1)*100, 'return20_pct': (c[-1]/c[-21]-1)*100,
        'up_days3': sum(c[i] > c[i-1] for i in range(len(c)-3, len(c))),
        'drawdown20_pct': (c[-1]/max(c[-20:])-1)*100, 'atr14': atr,
        'atr_pct': atr/c[-1]*100, 'extension_atr': (c[-1]-ma20)/atr,
        'volume_ratio_previous20': v[-1]/volume_mean,
        'close_location': (c[-1]-low[-1])/(h[-1]-low[-1]) if h[-1] > low[-1] else Decimal('0.5'),
        'relative20_pp': relative, 'excess_sector_pp': excess}


def _classify(f):
    up = f['close'] > f['ma20'] > f['ma60'] and f['ma20_slope5'] > 0
    setup = 'unconfirmed'
    if up and f['return5_pct'] > 0:
        setup = 'trend_continuation'
    elif up and f['return5_pct'] <= 0 and f['drawdown20_pct'] >= -8:
        setup = 'strong_pullback'
    elif f['return20_pct'] < 0 and f['return3_pct'] > 0 and f['up_days3'] >= 2 and f['return1_pct'] > 0:
        setup = 'rebound_watch'
    structure = 10 * sum((f['return3_pct'] > 0, f['up_days3'] >= 2, f['close'] > f['ma5'])
        if setup == 'rebound_watch' else (f['close'] > f['ma20'], f['ma20'] > f['ma60'], f['ma20_slope5'] > 0))
    relative = _bounded(f['relative20_pp']*2, 20) + _bounded(f['excess_sector_pp']*5, 10)
    ratio, change = f['volume_ratio_previous20'], f['return1_pct']
    if change > 0:
        volume = 20 if ratio >= Decimal('1.2') and f['close_location'] >= Decimal('.6') else 14 if ratio >= Decimal('.7') else 8
    elif change == 0:
        volume = 10
    else:
        volume = 12 if up and ratio < Decimal('.8') else 4 if up and ratio < Decimal('1.5') else 0
    position = _bounded(20 - max(Decimal(0), f['extension_atr']-1)*10
                        - max(Decimal(0), f['atr_pct']-5)*2, 20)
    risks = []
    for flag, triggered in [('extended', f['extension_atr'] > 2), ('high_volatility', f['atr_pct'] > 5),
            ('down_on_volume', change < 0 and ratio >= Decimal('1.5')),
            ('repair_is_not_reversal', setup == 'rebound_watch')]:
        if triggered:
            risks.append(flag)
    confirmed = setup != 'unconfirmed' and change > 0 and not set(risks) & {'extended', 'down_on_volume'}
    dimensions = {key: _integer(value) for key, value in (
        ('structure', structure), ('relative_strength', relative), ('price_volume', volume), ('position', position))}
    conditions = ['持续核对相对行业表现与公司风险；报告日交易状态仍待核验']
    if not confirmed:
        conditions.append('等待价格修复及位置条件确认，不能由分数替代')
    if setup == 'rebound_watch':
        conditions.append('继续观察修复能否延续；再次走弱时修复依据失效')
    else:
        conditions.append('跌破趋势结构或相对强势消失时，重新评估观察依据')
    return {'status': 'available', 'setup': setup, 'label': LABELS[setup],
        'score': sum(dimensions.values()) if setup != 'unconfirmed' else None,
        'dimensions': dimensions, 'facts': {key: str(value) for key, value in f.items()},
        'risk_flags': risks, 'confirmation': 'price_pattern_observed' if confirmed else 'waiting',
        'conditions': conditions, 'issues': []}
