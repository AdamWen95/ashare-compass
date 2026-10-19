"""Coverage gaps cannot erase reliable observations or forge full-market facts."""
from collections import Counter
from copy import deepcopy

import pytest

from test_premarket_observation_selection import fixture, strict, freeze_universe, DAY


def partial_packet():
    packet = fixture([[-1, -2, -2, -2, -3], [-5] * 5])
    universe = packet['universe']
    universe.update(resolved_trade_date=DAY, calendar_verified=True,
        observed_at=DAY+'T18:00:00+08:00', cutoff_at=DAY+'T18:00:00+08:00',
        structural_verified=False, universe_verified=False, collection_ready=False,
        blockers=['board_count_drop_unexplained:sse_main', 'previous_members_missing_unexplained:2'],
        changes={'missing_unexplained':['absent-1','absent-2'], 'coverage_baseline_snapshot_id':'previous'},
        board_counts=dict(Counter(m['board'] for m in universe['members'])),
        source_manifests=[{'complete':True,'permission_status':'approved','provenance_mode':'offline_test'}])
    for member in universe['members']:
        member.update(listing_date='2020-01-01', discovery_classification='ordinary_a')
    freeze_universe(universe)
    return packet


def test_missing_members_keep_valid_observations_without_relabeling_universe():
    from ashare_daily.premarket.selection import build_observation_selection
    packet = partial_packet()
    old = deepcopy(packet)
    selection = build_observation_selection(strict(packet)[0], packet, allow_partial=True)
    assert selection['selection_verified'] is True
    assert [m['security_id'] for m in selection['members']] == ['id-000']
    assert selection['relative_market_baseline']['scope'] == 'observed_sample'
    assert selection['research_coverage']['missing_count'] == 2
    assert selection['research_coverage']['observed_count'] == 10
    assert selection['rule_version'] == 'premarket-relative-observation-v2'
    assert packet == old
    legacy = build_observation_selection(strict(packet)[0], packet)
    assert not legacy['selection_verified']


def test_one_missing_quote_does_not_block_other_valid_stocks():
    from ashare_daily.premarket.selection import build_observation_selection
    packet = partial_packet()
    packet['quotes']['rows'].pop()
    selection = build_observation_selection(strict(packet)[0], packet, allow_partial=True)
    assert selection['selection_verified'] is True
    assert selection['relative_market_baseline']['sample_count'] == 9
    assert selection['relative_market_baseline']['missing_count'] == 1
    assert selection['research_coverage']['quote_missing_count'] == 1
    assert [m['security_id'] for m in selection['members']] == ['id-000']


@pytest.mark.parametrize('mutation', ['identity','wrong_date','quote_identity','hash','source','extra_blocker','missing_count'])
def test_partial_mode_preserves_hard_validation(mutation):
    from ashare_daily.premarket.selection import build_observation_selection
    packet = partial_packet()
    if mutation == 'identity':
        packet['universe']['members'][1]['security_id'] = packet['universe']['members'][0]['security_id']
    elif mutation == 'wrong_date':
        for q in packet['quotes']['rows']:
            q['trade_date']='2026-10-16'
    elif mutation == 'quote_identity':
        packet['quotes']['rows'][0]['security_id']='other'
    elif mutation == 'source':
        packet['universe']['source_manifests'][0]['complete']=False
    elif mutation == 'extra_blocker':
        packet['universe']['blockers'].append('source_permission_denied')
    elif mutation == 'missing_count':
        packet['universe']['changes']['missing_unexplained'].pop()
    selection = strict(packet)[0]
    if mutation == 'hash':
        packet['universe']['content_hash']='0'*64
    try:
        result=build_observation_selection(selection,packet,allow_partial=True)
    except ValueError:
        return
    assert not result['selection_verified']
    assert not result['members']


def test_universe_coverage_rejects_forged_members_and_preserves_missing_identity_list():
    from ashare_daily.universe_coverage import assess_universe_coverage
    u=partial_packet()['universe']
    coverage=assess_universe_coverage(u)
    assert coverage['missing_ids']==['absent-1','absent-2']
    assert coverage['reference_count']==12
    bad=deepcopy(u)
    bad['members'][0]['metadata_verified']=False
    freeze_universe(bad)
    with pytest.raises(ValueError):
        assess_universe_coverage(bad)


def test_partial_market_never_claims_whole_market_breadth():
    from ashare_daily.premarket.market import market_snapshot
    p=partial_packet()
    result=market_snapshot(universe=p['universe'],quotes=p['quotes']['rows'],benchmark={},market_date=DAY)
    assert result['advancing_fraction'] is None
    assert result['components']['breadth']=='unavailable'
    assert result['sample_count']==10


def test_exchange_industry_can_verify_current_source_while_prior_members_are_missing(tmp_path):
    from test_exchange_industry import fixture as industry_fixture, freeze
    from ashare_daily.providers.exchange_industry import exchange_industry_snapshot
    u=industry_fixture(tmp_path)
    u.update(structural_verified=False,universe_verified=False,collection_ready=False,
        resolved_trade_date=u['requested_date'],calendar_verified=True,
        blockers=['board_count_drop_unexplained:sse_main','previous_members_missing_unexplained:1'],
        changes={'missing_unexplained':['absent'],'coverage_baseline_snapshot_id':'previous'})
    u['board_counts']=dict(Counter(m['board'] for m in u['members'] if m['security_type']=='ordinary_a'))
    u['ordinary_a_count']=4
    for m in u['members']:
        m.update(listing_date='2020-01-01',listing_status='listed')
    freeze(tmp_path,u)
    assert not exchange_industry_snapshot(tmp_path,u)['catalog']['complete']
    result=exchange_industry_snapshot(tmp_path,u,allow_partial=True)
    assert result['catalog']['complete'] is True
    assert sum(len(v['rows']) for v in result['memberships'].values())==5
    # Rehashing the derived snapshot must not bypass original source evidence.
    u['members'][0]['raw']={}
    freeze(tmp_path,u)
    assert not exchange_industry_snapshot(tmp_path,u,allow_partial=True)['catalog']['complete']


def test_sample_report_has_versioned_coverage_and_never_labels_sample_as_whole_market():
    from test_premarket_observation_report import observation_inputs, resolve, STAMP
    from ashare_daily.premarket.report import build_brief, report_sections, validate_brief
    from ashare_daily.premarket.decisions import decide_stance
    market, candidates=observation_inputs()
    coverage={'schema_version':'premarket-coverage-v1','status':'partial','observed_count':10,
        'reference_count':12,'missing_count':2,'missing_ids':['a','b'], 'quote_sample_count':9,
        'quote_missing_count':1,'quote_missing_ids':['c'],'halted_count':0,'comparison_scope':'observed_sample'}
    candidates['preselection'].update(rule_version='premarket-observation-v2',research_coverage=coverage)
    for d in candidates['preselection']['directions']:
        d['relative_metrics']['comparison_scope']='observed_sample'
    for r in candidates['watch_candidates']:
        r['relative_observation']['comparison_scope']='observed_sample'
    stance=decide_stance(market,news_ready=False,has_direction=True,independent=True)
    report=build_brief(schedule=resolve(),market=market,stance=stance,candidates=candidates,
        events=[],coverage={'ready':False},previous=None,generated_at=STAMP)
    assert report['schema_version']=='premarket-brief-v4'
    assert report['research_coverage']==coverage
    sections=dict(report_sections(report))
    text=str(sections['观察方向']) if '观察方向' in sections else str(report_sections(report))
    assert '相对全市场' not in text and '全市场平均' not in text
    assert '样本' in text
    bad=deepcopy(report)
    bad['directions'][0]['relative_metrics']['comparison_scope']='verified_universe'
    with pytest.raises(ValueError):
        validate_brief(bad)


def test_independent_index_survives_rejected_universe(tmp_path,monkeypatch):
    from ashare_daily.premarket import services_market as service
    from test_premarket_market import market_inputs
    from types import SimpleNamespace
    _,_,benchmark=market_inputs()
    sector={'taxonomy':'fixture','output_directory':'outputs/sector'}
    monkeypatch.setattr(service,'load_config',lambda *a:sector)
    monkeypatch.setattr(service,'load_options',lambda *a:{})
    monkeypatch.setattr(service,'load_universe_config',lambda *a:SimpleNamespace(calendar_provider='exchange_calendars'))
    monkeypatch.setattr(service,'_read_snapshot',lambda *a,**k: (_ for _ in ()).throw(ValueError('source_hash_invalid')))
    monkeypatch.setattr(service,'prepare_benchmark',lambda *a,**k:benchmark)
    schedule={'report_date':'2026-09-22','market_as_of_date':'2026-09-21','is_trading_day':False}
    result=service.collect_market(tmp_path,SimpleNamespace(sector_config='sector',observation_config='obs',universe_config='universe'),
        schedule,tmp_path/'run',60,'isolated-run')
    assert result['market']['components']['trend']=='ready'
    assert result['market']['components']['breadth']=='unavailable'
    assert result['preselection']['candidates']==[]
    from ashare_daily.premarket.decisions import decide_stance
    stance=decide_stance(result['market'],news_ready=False,has_direction=False,
        independent=result['preselection'].get('observation_only') is True)
    assert stance['stance']=='attack'


def test_invalid_selection_binding_still_preserves_independent_index(tmp_path,monkeypatch):
    from ashare_daily.premarket import services_market as service
    from test_premarket_market import market_inputs
    from types import SimpleNamespace
    _,_,benchmark=market_inputs()
    sector={'taxonomy':'fixture_industry','output_directory':'outputs/sector'}
    monkeypatch.setattr(service,'load_config',lambda *a:sector)
    monkeypatch.setattr(service,'load_options',lambda *a:{})
    monkeypatch.setattr(service,'prepare_benchmark',lambda *a,**k:benchmark)
    packet=partial_packet();packet['universe']['content_hash']='0'*64
    result=service.prepare_collected_market(tmp_path,SimpleNamespace(sector_config='sector',observation_config='obs'),
        {'report_date':'2026-10-16','market_as_of_date':'2026-09-21'},tmp_path/'run',60,packet,
        master_day='2026-10-16',allow_partial=True)
    assert result['market']['components']['trend']=='ready'
    assert result['preselection']['observation_only'] is True
    assert result['preselection']['candidates']==[]


def test_market_basis_uses_market_day_universe_and_rejects_forged_history():
    from ashare_daily.premarket.coverage import market_basis_universe
    packet=partial_packet()
    previous=deepcopy(packet['universe'])
    previous.update(structural_verified=True,universe_verified=True,collection_ready=True,blockers=[])
    previous['changes']['missing_unexplained']=[]
    freeze_universe(previous)
    packet['market_universe']=previous
    assert market_basis_universe(packet,DAY)==previous
    packet['market_universe']['members'].pop()
    with pytest.raises(ValueError):
        market_basis_universe(packet,DAY)


def test_historical_breadth_rejects_same_symbol_with_different_security_identity():
    from ashare_daily.premarket.market import market_snapshot
    packet=partial_packet()
    u=packet['universe'];u['structural_verified']=True
    packet['quotes']['rows'][0]['security_id']='different-listing'
    result=market_snapshot(universe=u,quotes=packet['quotes']['rows'],benchmark={},market_date=DAY)
    assert result['components']['breadth']=='unavailable'
    assert result['sample_count']==9


@pytest.mark.parametrize('kind',['delisted_with_evidence','not_yet_listed'])
def test_valid_inactive_audit_members_do_not_poison_active_sample(kind):
    from ashare_daily.universe_coverage import assess_universe_coverage
    p=partial_packet();u=p['universe']
    member=deepcopy(u['members'][0]);member.update(security_id='inactive',code='600999',discovery_classification=kind)
    if kind=='delisted_with_evidence':
        member.update(listing_status='delisted',delisting_date='2026-10-14')
    else:
        member['listing_date']='2026-10-16'
    u['members'].append(member);freeze_universe(u)
    assert assess_universe_coverage(u)['observed_count']==10
    member['delisting_date']='2099-01-01';member['listing_date']='2020-01-01'
    freeze_universe(u)
    with pytest.raises(ValueError):
        assess_universe_coverage(u)


def test_historical_universe_hard_blockers_cannot_be_hidden_by_rehashing():
    from ashare_daily.premarket.coverage import market_basis_universe
    packet=partial_packet();previous=deepcopy(packet['universe'])
    previous.update(structural_verified=True,calendar_verified=False,blockers=['source_permission_denied'])
    previous['changes']['missing_unexplained']=[]
    previous['source_manifests'][0]['complete']=False
    freeze_universe(previous);packet['market_universe']=previous
    with pytest.raises(ValueError):
        market_basis_universe(packet,DAY)
