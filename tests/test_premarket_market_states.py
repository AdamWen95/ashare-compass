"""Five deterministic market states, independently scoped evidence and risks."""
from copy import deepcopy
from datetime import date, timedelta

import pytest

from test_premarket_market import market_inputs
from ashare_daily.premarket.market import market_snapshot


PATHS = {
    'trend_strength': [100]*15+[102,104,106,108,110,112],
    'strong_consolidation': [90]*15+[105,106,107,108,107,106],
    'weak_repair': [120]*15+[110,100,101,102,103,104],
    'trend_pressure': [120]*15+[115,110,108,106,104,102],
    'mixed_range': [120]*15+[115,110,108,106,104,102],
}


def state_market(kind='trend_strength',day='2026-09-21',path=None):
    changes=(1,1,-1,-1,-1) if kind=='trend_pressure' else (1,1,1,1,-1) if kind=='trend_strength' else (1,1,1,-1,-1)
    u,q,b=market_inputs(changes)
    dates=[(date.fromisoformat(day)-timedelta(days=20-i)).isoformat() for i in range(21)]
    b['expected_dates']=dates
    for i,row in enumerate(b['records']):
        row.update(trade_date=dates[i],close=str((path or PATHS[kind])[i]))
    b['records'][-1]['amount_cny']='120' if kind=='trend_strength' else '95'
    for row in q:
        row.update(trade_date=day,quote_at=day+'T15:00:00+08:00')
    return market_snapshot(universe=u,quotes=q,benchmark=b,market_date=day)


@pytest.mark.parametrize('kind,label',[
    ('trend_strength','趋势偏强'),('strong_consolidation','强势整理'),('mixed_range','震荡分化'),
    ('weak_repair','弱势修复'),('trend_pressure','趋势承压')])
def test_five_distinct_price_paths_classify_with_separate_basis_and_risk(kind,label):
    from ashare_daily.premarket.market_states import classify_market_state
    result=classify_market_state(state_market(kind),news_ready=False,events=[])
    assert result['state']==kind and result['label']==label
    assert result['evidence_basis']['scope']=='whole_market_and_index'
    assert result['evidence_basis']['status']=='partial'
    assert result['observation_focus'] and result['reasons']
    assert 'stance' not in result and 'probability' not in result


def test_single_up_day_does_not_certify_weak_repair():
    from ashare_daily.premarket.market_states import classify_market_state
    value=state_market('mixed_range',path=[120]*15+[110,100,101,97,96,103])
    assert classify_market_state(value,news_ready=True,events=[])['state']=='mixed_range'


@pytest.mark.parametrize('kind',['strong_consolidation','weak_repair'])
def test_missing_continuous_path_cannot_certify_consolidation_or_repair(kind):
    from ashare_daily.premarket.market_states import classify_market_state
    value=state_market(kind)
    value['benchmark'].pop('state_window',None)
    result=classify_market_state(value,news_ready=True,events=[])
    assert result['state'] not in {'strong_consolidation','weak_repair'}
    assert result['evidence_basis']['limitations']


def test_verified_adverse_event_warns_without_rewriting_price_state():
    from ashare_daily.premarket.market_states import classify_market_state
    value=state_market()
    event={'event_id':'verified-event','validation_status':'verified','risk_status':'adverse','impact_scope':'market'}
    result=classify_market_state(value,news_ready=True,events=[event])
    assert result['state']=='trend_strength'
    assert any(r['code']=='verified_adverse_event' and r['severity']=='high' for r in result['risk_notes'])
    event['validation_status']='pending'
    pending=classify_market_state(value,news_ready=True,events=[event])
    assert not any(r['code']=='verified_adverse_event' for r in pending['risk_notes'])


def test_index_only_remains_usable_without_promoting_sample_breadth():
    from ashare_daily.premarket.market_states import classify_market_state
    value=state_market()
    value['components']['breadth']='unavailable'
    value.update(advancing_fraction=None,median_change_pct=None,sample_advancing_fraction='1')
    result=classify_market_state(value,news_ready=False,events=[])
    assert result['state']=='trend_strength'
    assert result['evidence_basis']['scope']=='sse_index'
    assert 'breadth' not in result['evidence_basis']['used_components']


@pytest.mark.parametrize('mutation',['date','hashless_numeric_conflict','nan','bool','duplicate_date','shape'])
def test_invalid_index_path_does_not_certify_temporal_states(mutation):
    from ashare_daily.premarket.market_states import classify_market_state
    value=state_market('weak_repair')
    value['components']['breadth']='unavailable'
    window=value['benchmark']['state_window']
    if mutation=='date': window['dates'][-1]='2026-09-22'
    elif mutation=='hashless_numeric_conflict': window['closes'][-1]='105'
    elif mutation=='nan': window['closes'][-1]='NaN'
    elif mutation=='bool': window['closes'][-1]=True
    elif mutation=='duplicate_date': window['dates'][-2]=window['dates'][-1]
    else: value['benchmark']['state_window']=[]
    result=classify_market_state(value,news_ready=True,events=[])
    assert result['state']=='undetermined'
    assert result['label']=='暂无法判断'


def test_local_sample_alone_is_not_a_full_market_state():
    from ashare_daily.premarket.market_states import classify_market_state
    result=classify_market_state({'market_as_of_date':'2026-09-21','status':'partial',
        'sample_count':100,'sample_advancing_fraction':'1','components':{}},news_ready=False,events=[])
    assert result['state']=='undetermined'
    assert result['evidence_basis']['scope']=='observed_sample'


def test_breadth_threshold_is_inclusive_and_data_gaps_do_not_force_neutral():
    from ashare_daily.premarket.market_states import classify_market_state
    value=state_market()
    value.update(advancing_fraction='0.6',median_change_pct='0')
    value['components']['trend']='unavailable';value['components']['activity']='unavailable'
    result=classify_market_state(value,news_ready=False,events=[])
    assert result['state']=='trend_strength'
    assert result['evidence_basis']['scope']=='whole_market_breadth'
    value['advancing_fraction']='0.5999'
    assert classify_market_state(value,news_ready=False,events=[])['state']=='mixed_range'


@pytest.mark.parametrize('amount,state',[('80','weak_repair'),('79.99','mixed_range')])
def test_repair_volume_boundary_is_inclusive(amount,state):
    from ashare_daily.premarket.market_states import classify_market_state
    value=state_market('weak_repair');value['benchmark']['amount']=amount
    assert classify_market_state(value,news_ready=True,events=[])['state']==state


@pytest.mark.parametrize('amount,state',[('119.99','strong_consolidation'),('120','mixed_range')])
def test_high_volume_decline_is_not_called_orderly_consolidation(amount,state):
    from ashare_daily.premarket.market_states import classify_market_state
    value=state_market('strong_consolidation');value['benchmark']['amount']=amount
    result=classify_market_state(value,news_ready=True,events=[])
    assert result['state']==state
    assert any(r['code']=='high_volume_decline' for r in result['risk_notes'])==(state=='mixed_range')


def test_bad_price_path_keeps_independent_weak_volume_evidence():
    from ashare_daily.premarket.market_states import classify_market_state
    value=state_market();value['benchmark']['amount']='50'
    value['benchmark']['state_window']['closes'][-1]='113'
    result=classify_market_state(value,news_ready=True,events=[])
    assert result['state']=='mixed_range'
    assert 'activity' in result['evidence_basis']['used_components']
    assert 'trend' not in result['evidence_basis']['used_components']
    assert any(r['code']=='activity_below_mean' for r in result['risk_notes'])
