from copy import deepcopy
import json

import pytest

from ashare_daily.premarket.shadow_followup import observe_shadow_report, compare_shadow_reports
from test_shadow_analysis import shadow_brief


DAYS = ['2026-10-12', '2026-10-13', '2026-10-14', '2026-10-15', '2026-10-16',
        '2026-10-19', '2026-10-20', '2026-10-21', '2026-10-22', '2026-10-23', '2026-10-26']


def sample():
    report = shadow_brief()
    report.update(published_at='2026-10-12T09:00:00+08:00', publication_status='on_time')
    prices = [100, 99, 102, 105, 103, 104, 102, 106, 108, 107, 110]
    window = {'source_hash': 'a'*64, 'observed_at': '2026-10-26T19:00:00+08:00',
        'mode': 'offline_test', 'dates': DAYS, 'adjustment_versions': {'watch-id': 'one-version'},
        'members': {'watch-id': {'issues': [], 'rows': {day: {'close': str(p)} for day, p in zip(DAYS, prices)}}}}
    return report, [window]


def test_followup_starts_after_publication_not_from_previous_close():
    report, windows = sample()
    result = observe_shadow_report(report, windows, as_of='2026-10-26')
    rows = {r['horizon']: r for r in result['records']}
    assert rows[3]['anchor_date'] == '2026-10-12'
    assert rows[3]['target_date'] == '2026-10-15'
    assert float(rows[3]['change_pct']) == 5
    assert float(rows[3]['worst_close_change_pct']) == -1
    assert float(rows[3]['max_close_drawdown_pct']) == -1
    assert float(rows[10]['change_pct']) == 10
    assert rows[3]['industry_excess_pct'] is None


def test_unmatured_and_missing_data_remain_distinct():
    report, windows = sample()
    windows[0].update(dates=DAYS[:3], observed_at='2026-10-14T19:00:00+08:00')
    early = observe_shadow_report(report, windows, as_of='2026-10-14')
    assert all(r['status'] == 'not_matured' and r['change_pct'] is None for r in early['records'])
    report, windows = sample()
    del windows[0]['members']['watch-id']['rows'][DAYS[1]]
    result = observe_shadow_report(report, windows, as_of='2026-10-26')
    assert all(r['status'] == 'missing_data' and r['change_pct'] is None for r in result['records'])


def test_future_evidence_and_late_reports_do_not_become_forward_results():
    report, windows = sample()
    result = observe_shadow_report(report, windows, as_of='2026-10-15')
    assert all(r['status'] == 'calendar_incomplete' for r in result['records'])
    report['published_at'] = '2026-10-12T15:01:00+08:00'
    result = compare_shadow_reports([report], windows, as_of='2026-10-26')
    assert not result['anchors']
    assert result['excluded_reports'][0]['reason'] == 'published_after_anchor_close'


def test_never_splice_two_adjustment_versions_to_complete_a_path():
    report, windows = sample()
    second = deepcopy(windows[0]); second['adjustment_versions']['watch-id'] = 'second-version'
    del windows[0]['members']['watch-id']['rows'][DAYS[0]]
    del second['members']['watch-id']['rows'][DAYS[3]]
    result = observe_shadow_report(report, windows+[second], as_of='2026-10-26')
    assert result['records'][0]['status'] == 'missing_data'


def test_conflicting_same_time_versions_are_not_silently_selected():
    report, windows = sample(); second = deepcopy(windows[0])
    second['source_hash'] = 'b'*64
    second['members']['watch-id']['rows'][DAYS[3]]['close'] = '200'
    result = observe_shadow_report(report, windows+[second], as_of='2026-10-26')
    assert result['records'][0]['status'] == 'conflicting_data'


def test_conflicting_calendar_is_rejected():
    report, windows = sample(); second = deepcopy(windows[0])
    second['dates'] = [d for d in DAYS if d != '2026-10-13']
    with pytest.raises(ValueError, match='calendar_conflict'):
        observe_shadow_report(report, windows+[second], as_of='2026-10-26')


def test_paired_groups_and_duplicate_anchor_policy_do_not_promote():
    report, windows = sample()
    result = compare_shadow_reports([report, deepcopy(report)], windows, as_of='2026-10-26')
    group = next(g for g in result['groups'] if g['setup'] == 'trend_continuation' and g['horizon'] == 3)
    assert group['included_anchors'] == 1
    assert group['methods']['new_score'] == group['methods']['old_score'] == group['methods']['relative_strength']
    assert result['excluded_reports'][0]['reason'] == 'duplicate_target_session'
    assert result['ranking_change_allowed'] is False
    assert result['conclusion'] == 'evidence_insufficient_for_promotion'
    json.dumps(result, allow_nan=False)


def test_current_watch_reports_are_included_by_existing_followup_helper():
    from ashare_daily.premarket.study import followup_observations
    from test_premarket_observation_report import observation_brief
    report = observation_brief()
    calendar = {'2026-10-09': True, '2026-10-10': False, '2026-10-11': False, '2026-10-12': True,
                '2026-10-13': True, '2026-10-14': True}
    result = followup_observations(report, {}, calendar, as_of='2026-10-14')
    assert len(result['records']) == 3
    assert all(r['security_id'] == 'watch-id' and r['in_selected'] for r in result['records'])


def test_cli_reads_verified_archive_and_writes_new_artifact_only(tmp_path):
    import importlib.util
    from pathlib import Path
    from ashare_daily.premarket.archive import publish_brief
    from test_premarket_report import DUE
    archive = publish_brief(tmp_path, shadow_brief(), now=DUE)
    spec = importlib.util.spec_from_file_location('compare_shadow_cli', Path(__file__).resolve().parents[1]/'scripts/compare_premarket_shadow.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    output = tmp_path/'new-study.json'
    args = ['--output-root', str(tmp_path/'outputs'), '--report-directory', archive['directory'],
            '--as-of', '2026-10-26', '--output', str(output)]
    assert module.main(args) == 0
    assert json.loads(output.read_text(encoding='utf-8'))['status'] == 'insufficient_evidence'
    with pytest.raises(ValueError, match='must_be_new'):
        module.main(args)
