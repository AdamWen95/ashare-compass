from copy import deepcopy

import pytest

from test_sector_observation import configure
from test_sector_screening import fixture, freeze_selection, seal_stock


@pytest.mark.parametrize('relative', [False, True])
def test_relative_pool_checks_qualification_even_when_trend_rule_fails(tmp_path, monkeypatch, relative):
    from ashare_daily import sector_observation as workflow
    configure(tmp_path)
    selection, inputs = fixture(known_risk=True)
    if relative:
        selection.update(observation_only=True, rule_version='premarket-relative-observation-v1')
        freeze_selection(selection)
    for packet in inputs['securities'].values():
        for row in packet['raw_records']:
            row['amount_cny'] = '1000000'
        for row in packet['adjustment_window']['records']:
            row['amount_cny'] = '1000000'
        seal_stock(packet)
    monkeypatch.setattr(workflow, 'prepare_history', lambda *a, **kw: {'status': 'ready', 'metrics': {'network_requests': 0}})
    monkeypatch.setattr(workflow, 'screening_history_inputs', lambda *a: {
        'calendar': inputs['calendar'], 'securities': inputs['securities'], 'file_refs': [], 'issues': []})
    monkeypatch.setattr(workflow, 'prepare_benchmark', lambda *a, **kw: inputs['benchmark'])
    result = workflow.run_observation(tmp_path, selection, {}, output_directory='evidence', online=False,
        require_delisting_check=False)
    assert result['technical']['evaluations'][0]['technical_status'] == 'fail'
    assert result['qualification_probe_security_ids'] == (['security-0'] if relative else [])
    assert result['network_requests'] == 0


def test_relative_pool_cannot_expand_history_beyond_fifteen(tmp_path, monkeypatch):
    from ashare_daily import sector_observation as workflow
    configure(tmp_path)
    selection, _ = fixture(count=16)
    selection.update(observation_only=True, rule_version='premarket-relative-observation-v1')
    freeze_selection(selection)
    monkeypatch.setattr(workflow, 'prepare_history', lambda *a, **kw: pytest.fail('oversized pool reached history'))
    with pytest.raises(ValueError, match='observation_pool'):
        workflow.run_observation(tmp_path, selection, {}, output_directory='evidence', online=False)


def test_frozen_packet_enters_independent_observation_selection(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from ashare_daily.premarket import services_market as service
    from test_premarket_market import market_inputs
    u, q, b = market_inputs()
    packet = {'universe': u, 'quotes': {'rows': q}, 'catalog': {}, 'memberships': {}, 'calendar': {}}
    strict = {'selection_verified': True, 'selection_id': 'strict', 'members': [], 'selected_sectors': []}
    relative = {**strict, 'observation_only': True, 'selection_id': 'relative', 'selection_verified': False,
        'target_date': '2026-09-21'}
    monkeypatch.setattr(service, 'load_config', lambda *a: {})
    monkeypatch.setattr(service, 'load_options', lambda *a: {})
    monkeypatch.setattr(service, 'evaluate', lambda *a, **kw: (strict, []))
    monkeypatch.setattr(service, 'build_observation_selection', lambda original, actual: relative
        if original is strict and actual is packet else pytest.fail('wrong packet binding'), raising=False)
    archived = []
    monkeypatch.setattr(service, 'archive_selection', lambda root, cfg, value, *a: archived.append(value['selection_id']))
    monkeypatch.setattr(service, 'prepare_benchmark', lambda *a, **kw: b)
    monkeypatch.setattr(service, '_observe', lambda *a, **kw: pytest.fail('blocked pool must not collect history'))
    result = service.prepare_collected_market(tmp_path, SimpleNamespace(sector_config='s', observation_config='o'),
        {'report_date': '2026-09-22', 'market_as_of_date': '2026-09-21'}, tmp_path, 10, packet,
        master_day='2026-09-22')
    assert archived == ['strict', 'relative']
    assert result['preselection']['observation_only'] is True
    assert result['selection_id'] == 'relative'
    assert result['market']['components']['breadth'] == 'ready'


def test_history_failure_keeps_dated_observations_with_explicit_unknowns(tmp_path):
    from ashare_daily.premarket.services_market import _available_market
    from test_premarket_observation_candidates import inputs
    selection, _, _ = inputs()
    result = _available_market(tmp_path, {}, {'universe': {}, 'quotes': {'rows': []}}, selection,
        {'market_as_of_date': '2026-10-15'}, tmp_path, '2026-10-16', 0,
        reason='history_unavailable', online=False, benchmark={})
    assert len(result['preselection']['candidates']) == 5
    assert all(r['technical_score'] is None and r['technical_status'] == 'unknown'
        and r['eligibility_status'] == 'pending' for r in result['preselection']['candidates'])
    assert len(result['entities']) == 5


def test_score_failure_preserves_already_verified_qualification_exclusions(tmp_path):
    from ashare_daily.premarket.services_market import _available_market
    from test_premarket_observation_candidates import inputs
    selection, evaluations, _ = inputs()
    evaluations[0]['eligibility_status'] = 'fail'
    evaluations[0]['exclusion_reasons'] = ['verified_ST']
    result = _available_market(tmp_path, {}, {'universe': {}, 'quotes': {'rows': []}}, selection,
        {'market_as_of_date': '2026-10-15'}, tmp_path, '2026-10-16', 0,
        reason='score_unavailable', online=False, benchmark={}, evaluations=evaluations)
    pre = result['preselection']
    assert evaluations[0]['security_id'] not in {r['security_id'] for r in pre['candidates']}
    assert pre['audit'][0]['eligibility_status'] == 'fail'
    assert any(r['security_id'] == evaluations[0]['security_id'] for r in pre['excluded'])
    assert all(r['technical_score'] is None for r in pre['candidates'])


def test_invalid_observation_cannot_be_replaced_by_unknown_candidates(tmp_path):
    from ashare_daily.premarket.services_market import _available_market
    from test_premarket_observation_candidates import inputs
    selection, _, _ = inputs()
    result = _available_market(tmp_path, {}, {'universe': {}, 'quotes': {'rows': []}}, selection,
        {'market_as_of_date': '2026-10-15'}, tmp_path, '2026-10-16', 0,
        reason='observation_invalid', online=False, benchmark={}, allow_unknown=False)
    assert result['preselection']['candidates'] == []
    assert len(result['preselection']['directions']) == 3
