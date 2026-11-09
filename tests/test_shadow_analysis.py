from copy import deepcopy
from datetime import date, timedelta

import pytest

from ashare_daily.premarket.shadow_analysis import attach_shadow_analysis, build_shadow_analysis
from ashare_daily.premarket.shadow_inputs import unavailable_inputs, build_shadow_inputs
from ashare_daily.premarket.report import validate_brief
from ashare_daily.sector_selection import digest
from test_premarket_state_report import state_brief
from test_reference_indicators import bars


def fixture():
    report = state_brief()
    for row in (report['watch_candidates'][0], report['screening_audit'][0]):
        row.update(eligibility_status='pass', technical_score=61,
                   metrics={'relative_return_20': '0.10'})
    rows = bars()
    day = date.fromisoformat(report['market_as_of_date'])
    for i, row in enumerate(rows):
        row['trade_date'] = (day-timedelta(days=119-i)).isoformat()
    source = unavailable_inputs(report['observation_selection_id'], report['market_as_of_date'], 'test')
    source.update(status='ready', issues=[], source_hashes={k: 'a'*64 for k in ('selection', 'inputs', 'observation', 'reference')},
        records=[{'security_id': 'watch-id', 'symbol': 'sh.600000', 'bars': rows, 'window_hash': digest(rows),
            'issues': [], 'relative_observation': deepcopy(report['watch_candidates'][0]['relative_observation']), 'old_score': 61}])
    return report, source


def shadow_brief():
    report, source = fixture()
    report = attach_shadow_analysis(report, source)
    report['schema_version'] = 'premarket-brief-v6'
    report['rule_versions']['shadow'] = 'premarket-setup-score-v1'
    report['input_hashes']['shadow'] = digest(source)
    validate_brief(report)
    return report


def test_shadow_has_no_effect_on_official_candidates_or_state():
    report, source = fixture(); before = deepcopy(report)
    result = attach_shadow_analysis(report, source)
    assert report == before
    for key in ('watch_candidates', 'market_state', 'screening_audit', 'baseline_ids'):
        assert result[key] == before[key]
    analysis = result['shadow_analysis']
    assert analysis['changes_production_ranking'] is False
    assert analysis['rankings']['trend_continuation']['new_score'] == ['watch-id']
    assert analysis['records'][0]['company_status'] == 'pending'


@pytest.mark.parametrize('kind', ['pending', 'fail', 'adverse'])
def test_pending_and_negative_evidence_never_enter_shadow_comparison(kind):
    report, source = fixture()
    if kind == 'adverse':
        report['overnight_events'] = [{'validation_status': 'verified', 'risk_status': 'adverse', 'security_ids': ['watch-id']}]
    else:
        report['screening_audit'][0]['eligibility_status'] = kind
    analysis = build_shadow_analysis(report, source)
    assert not analysis['records'][0]['comparison_eligible']
    assert analysis['rankings']['trend_continuation']['new_score'] == []


@pytest.mark.parametrize('kind', ['date', 'identity', 'hash', 'future', 'duplicate', 'relative', 'old_score'])
def test_input_binding_and_temporal_tampering_are_rejected(kind):
    report, source = fixture(); row = source['records'][0]
    if kind == 'date': source['market_as_of_date'] = '2026-10-08'
    if kind == 'identity': row['symbol'] = 'other'
    if kind == 'hash': row['bars'][0]['close'] = '99'
    if kind == 'future':
        row['bars'][-1]['trade_date'] = '2026-10-10'; row['window_hash'] = digest(row['bars'])
    if kind == 'duplicate': source['records'].append(deepcopy(row))
    if kind == 'relative': row['relative_observation']['excess_sector_pp'] = '5'
    if kind == 'old_score': row['old_score'] = 100
    with pytest.raises(ValueError, match='shadow'):
        build_shadow_analysis(report, source)


def test_v6_validation_replays_classification_even_after_input_hash_update():
    report = shadow_brief(); report['shadow_analysis']['records'][0]['score'] = 100
    with pytest.raises(ValueError, match='shadow'):
        validate_brief(report)
    report = shadow_brief(); report['shadow_analysis']['changes_production_ranking'] = True
    with pytest.raises(ValueError, match='shadow'):
        validate_brief(report)


def test_unavailable_is_visible_and_does_not_replace_legacy_score():
    report, _ = fixture()
    source = unavailable_inputs(report['observation_selection_id'], report['market_as_of_date'], 'history unavailable')
    analysis = build_shadow_analysis(report, source)
    assert analysis['status'] == 'unavailable' and analysis['records'] == []
    assert report['watch_candidates'][0]['technical_score'] == 61


def test_frozen_adapter_reuses_existing_validation_and_preserves_inputs(tmp_path):
    from test_reference_strategy import sample
    from ashare_daily.reference_strategy import build_reference_review
    selection, inputs, observation = sample(tmp_path)
    reference = build_reference_review(selection, inputs, observation)
    before = deepcopy((selection, inputs, observation, reference))
    packet = build_shadow_inputs(selection, inputs, observation, reference)
    assert all(len(r['bars']) == 120 for r in packet['records'])
    assert packet['source_hashes']['inputs'] == inputs['input_hash']
    assert (selection, inputs, observation, reference) == before
    inputs['securities'][selection['members'][0]['security_id']]['raw_records'][0]['close'] = '999'
    with pytest.raises(ValueError):
        build_shadow_inputs(selection, inputs, observation, reference)


def test_v6_archives_roundtrip_and_leave_v5_unchanged(tmp_path):
    from pathlib import Path
    from ashare_daily.premarket.archive import publish_brief, scan_briefs
    from test_premarket_report import DUE
    old = publish_brief(tmp_path, state_brief(), now=DUE)
    path = Path(old['directory'])/'premarket_brief.json'; before = path.read_bytes()
    publish_brief(tmp_path, shadow_brief(), now=DUE)
    reports, errors = scan_briefs(tmp_path/'outputs', now=DUE)
    assert not errors and {r['report']['schema_version'] for r in reports} == {'premarket-brief-v5', 'premarket-brief-v6'}
    assert path.read_bytes() == before


def test_html_and_markdown_preserve_waiting_conditions_and_escape_values():
    from ashare_daily.premarket.presentation import render_body
    from ashare_daily.premarket.report import report_sections
    report = shadow_brief()
    report['shadow_analysis']['records'][0]['name'] = '<script>bad()</script>'
    html = render_body(report)
    assert '试运行' in html and '等待条件确认' in html and '公司资料仍待核查' in html
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert '观察形态与分类评分（试运行）' in [name for name, _ in report_sections(report)]


def test_existing_daily_job_publishes_v6_and_records_local_followup_without_extra_calls(tmp_path):
    from test_premarket_daily import Services, Clock, setup, invoke
    from ashare_daily.premarket.archive import scan_briefs
    class ShadowServices(Services):
        def market(self, *args):
            result = super().market(*args)
            pre = result['preselection']
            result['shadow_inputs'] = unavailable_inputs(pre['selection_id'], pre['market_as_of_date'], 'history gap')
            return result
    clock = Clock(); services = ShadowServices(clock)
    result = invoke(tmp_path, setup(tmp_path), services, clock)
    assert result['generation_status'] == 'ok'
    assert result['module_statuses']['shadow_analysis'] == 'unavailable'
    assert result['shadow_followup']['status'] == 'insufficient_evidence'
    assert services.calls == ['market', 'news', 'interpret']
    reports, errors = scan_briefs(tmp_path/'outputs', now=clock.now())
    assert not errors and reports[0]['report']['schema_version'] == 'premarket-brief-v6'
    assert len(reports[0]['report']['watch_candidates']) == 5


def test_optional_followup_failure_does_not_trigger_a_second_business_attempt(tmp_path, monkeypatch):
    from test_premarket_daily import Services, Clock, setup, invoke
    from ashare_daily.premarket import shadow_tracking
    class ShadowServices(Services):
        def market(self, *args):
            result = super().market(*args); pre = result['preselection']
            result['shadow_inputs'] = unavailable_inputs(pre['selection_id'], pre['market_as_of_date'], 'gap')
            return result
    def fail(*args, **kwargs): raise ValueError('frozen input invalid')
    monkeypatch.setattr(shadow_tracking, 'track_shadow_history', fail)
    clock = Clock(); services = ShadowServices(clock); cfg = setup(tmp_path)
    result = invoke(tmp_path, cfg, services, clock)
    assert result['generation_status'] == 'ok'
    assert result['module_statuses']['shadow_followup'] == 'unavailable'
    assert invoke(tmp_path, cfg, services, clock)['status'] == 'reused'
    assert services.calls == ['market', 'news', 'interpret']
