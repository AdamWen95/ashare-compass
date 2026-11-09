from copy import deepcopy

from test_reference_indicators import bars
from ashare_daily.premarket.shadow_scoring import score_setup


def test_falling_and_falling_with_volume_do_not_gain_an_opportunity():
    rows = bars([220 - i for i in range(120)])
    for volume in (100, 200):
        rows[-1]['volume_shares'] = volume
        result = score_setup(rows, relative_return_20='-0.1', excess_sector_pp='1')
        assert result['setup'] == 'unconfirmed'
        assert result['score'] is None
        assert result['dimensions']['price_volume'] == 0


def test_strong_trend_is_not_penalized_just_for_overbought_oscillators():
    result = score_setup(bars(), relative_return_20='0.1', excess_sector_pp='2')
    assert result['setup'] == 'trend_continuation'
    assert result['dimensions']['structure'] == 30
    assert result['dimensions']['relative_strength'] == 30
    assert 'extended' in result['risk_flags']
    assert result['confirmation'] == 'waiting'


def test_shrinking_pullback_is_separate_from_rising_volume():
    rows = bars([100 + i * .1 for i in range(120)])
    for i, price in enumerate([111.4, 111.3, 111.2, 111.1, 111.0], 115):
        rows[i].update(close=str(price), high=str(price+1), low=str(price-1))
    rows[-1]['volume_shares'] = 50
    result = score_setup(rows, relative_return_20='0.02', excess_sector_pp='.5')
    assert result['setup'] == 'strong_pullback'
    assert result['dimensions']['price_volume'] == 12
    assert result['confirmation'] == 'waiting'
    rows[-1]['volume_shares'] = 200
    other = score_setup(rows, relative_return_20='0.02', excess_sector_pp='.5')
    assert other['dimensions']['price_volume'] == 0
    assert 'down_on_volume' in other['risk_flags']


def test_rebound_requires_observed_price_repair():
    rows = bars([220 - i for i in range(120)])
    for i, p in enumerate([104, 105, 106], 117):
        rows[i].update(close=str(p), high=str(p+1), low=str(p-1))
    result = score_setup(rows, relative_return_20='-.05', excess_sector_pp='1')
    assert result['setup'] == 'rebound_watch'
    assert result['score'] is not None
    assert 'repair_is_not_reversal' in result['risk_flags']


def test_missing_data_is_not_zero_and_input_is_not_mutated():
    rows = bars(); before = deepcopy(rows)
    assert score_setup(rows[:-1], relative_return_20='0', excess_sector_pp='1')['score'] is None
    assert score_setup(rows, relative_return_20=None, excess_sector_pp='1')['status'] == 'unavailable'
    assert score_setup(rows, relative_return_20='NaN', excess_sector_pp='1')['score'] is None
    assert rows == before
