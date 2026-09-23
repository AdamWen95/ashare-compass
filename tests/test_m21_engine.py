"""M2.1 status integration on explicitly synthetic, hand-calculable inputs."""

from copy import deepcopy
from decimal import Decimal
import json
import sqlite3

import pytest

from test_m2_engine import input_snapshot
from test_m2_snapshots import small_market, TARGET
from ashare_daily.cli import main
from ashare_daily.m21 import run_m21
from ashare_daily.screening.engine import evaluate_snapshot
from ashare_daily.screening.m21 import M21Config, evaluate_m21, load_m21_config
from ashare_daily.screening.snapshots import freeze_input, load_snapshot, read_market_input


@pytest.fixture
def m21_input(input_snapshot):
    value = deepcopy(input_snapshot)
    value['schema_version'] = 'm2-input-v1'
    value['strategy_config'] = M21Config(**value['strategy_config']).model_dump(mode='json')
    value['eligibility_states'] = {}
    return value


def state(value, status):
    value['eligibility_states']['sh.600000'] = {
        'delisting_period': status, 'status': 'unknown' if status is None else str(status).lower(),
        'effective_date': value['trade_date'], 'evidence_id': 'OFFLINE TEST EXPLICIT DATE EVIDENCE',
        'evidence_ids': ['OFFLINE TEST EXPLICIT DATE EVIDENCE'],
    }


def test_pass_unknown_is_pending_table_without_candidate_or_rank(m21_input):
    report = evaluate_m21(m21_input)
    row = report['evaluations'][0]
    assert (row['technical_screen_status'], row['eligibility_status'], row['status']) == ('pass', 'pending', 'data_insufficient')
    assert Decimal(row['ma_short']) == Decimal('110.5')
    assert Decimal(row['ma_long']) == Decimal('90.5')
    assert Decimal(row['period_return']) == Decimal('.2')
    assert report['candidates'] == [] and report['pending_eligibility'] == [row]
    assert row['rank'] is None and row['data_issues']
    assert report['counts']['candidate_count'] == 0
    assert report['counts']['pending_eligibility_count'] == 1


def test_fail_unknown_is_excluded_and_still_has_gap(m21_input):
    for bar in m21_input['raw_bars']:
        if bar['symbol'] == 'sh.600000':
            bar['amount_cny'] = '49999999'
    report = evaluate_m21(m21_input)
    row = report['evaluations'][0]
    assert (row['technical_screen_status'], row['eligibility_status'], row['status']) == ('fail', 'pending', 'excluded')
    assert row['exclusion_reasons'] and row['data_issues']
    assert report['counts']['excluded_count'] == report['counts']['stocks_with_data_gaps'] == 1
    assert report['counts']['data_insufficient_count'] == report['counts']['pending_eligibility_count'] == 0


@pytest.mark.parametrize('flag,eligibility,classification,candidates', [
    (True, 'fail', 'excluded', 0), (False, 'pass', 'candidate', 1),
    (None, 'pending', 'data_insufficient', 0),
])
def test_explicit_three_state_eligibility(m21_input, flag, eligibility, classification, candidates):
    state(m21_input, flag)
    report = evaluate_m21(m21_input)
    row = report['evaluations'][0]
    assert row['eligibility_status'] == eligibility and row['status'] == classification
    assert report['counts']['candidate_count'] == candidates
    assert report['counts']['eligibility_verified_count'] == int(flag is not None)


def test_known_st_exclusion_with_unknown_delisting_is_not_complete_coverage(m21_input):
    m21_input['raw_bars'][-1]['is_st'] = True
    report = evaluate_m21(m21_input)
    assert report['evaluations'][0]['eligibility_status'] == 'fail'
    counts = report['counts']
    assert counts['eligibility_conclusion_count'] == counts['excluded_count'] == counts['stocks_with_data_gaps'] == 1
    assert counts['eligibility_verified_count'] == counts['eligibility_pending_count'] == 0


def test_source_conflict_prevents_technical_judgement(m21_input):
    m21_input['source_issues']['sh.600000'] = ['OFFLINE TEST 来源版本冲突']
    report = evaluate_m21(m21_input)
    assert report['evaluations'][0]['technical_screen_status'] == 'not_computable'
    assert report['pending_eligibility'] == []
    assert report['counts']['market_data_success_count'] == 0


def test_all_axes_partition_and_gap_overlap(m21_input):
    template_raw = [bar for bar in m21_input['raw_bars'] if bar['symbol'] == 'sh.600000']
    template_series = deepcopy(m21_input['adjusted_data']['series']['sh.600000'])
    template_instrument = deepcopy(m21_input['instruments'][-1])
    for symbol, amount, flag in [('sh.600001', '1', None), ('sh.600002', '50000000', False), ('sh.600003', '50000000', True)]:
        m21_input['sample_types'][symbol] = 'stock'
        m21_input['instruments'].append(dict(template_instrument, symbol=symbol))
        m21_input['raw_bars'].extend(dict(bar, symbol=symbol, amount_cny=amount) for bar in template_raw)
        m21_input['adjusted_data']['series'][symbol] = deepcopy(template_series)
        if flag is not None:
            m21_input['eligibility_states'][symbol] = {'delisting_period': flag, 'effective_date': m21_input['trade_date'], 'evidence_id': 'OFFLINE TEST'}
    report = evaluate_m21(m21_input)
    c = report['counts']
    assert (c['candidate_count'], c['excluded_count'], c['data_insufficient_count'], c['stocks_with_data_gaps']) == (1, 2, 1, 2)
    assert sum(c[k] for k in ('candidate_count', 'excluded_count', 'data_insufficient_count', 'qualified_not_selected_count')) == 4
    assert sum(c[k] for k in ('technical_pass_count', 'technical_fail_count', 'technical_not_computable_count')) == 4
    assert sum(c[k] for k in ('eligibility_pass_count', 'eligibility_fail_count', 'eligibility_pending_count')) == 4
    assert [row['symbol'] for row in report['pending_eligibility']] == ['sh.600000']
    assert [row['symbol'] for row in report['candidates']] == ['sh.600002']


def test_old_numerical_engine_is_unchanged(input_snapshot, m21_input):
    old = evaluate_snapshot(input_snapshot)['evaluations'][0]
    new = evaluate_m21(m21_input)['evaluations'][0]
    for field in ('ma_short', 'ma_long', 'period_return', 'benchmark_period_return', 'relative_return', 'avg_amount_cny', 'valid_history_count', 'adjusted_close'):
        assert old[field] == new[field]


def test_freeze_replay_preserves_config_evidence_and_input(m21_input, tmp_path):
    source = deepcopy(m21_input)
    frozen, path = freeze_input({k: v for k, v in m21_input.items() if k != 'snapshot_id'}, None, tmp_path / 'snapshots')
    before = path.read_bytes()
    report = evaluate_m21(frozen)
    loaded, _ = load_snapshot(path, tmp_path)
    assert evaluate_m21(loaded) == report
    assert path.read_bytes() == before and source == m21_input
    loaded['strategy_config']['max_candidates'] = 1
    path.write_text(json.dumps(loaded), encoding='utf-8')
    with pytest.raises(ValueError, match='快照哈希'):
        load_snapshot(path, tmp_path)


def test_offline_replay_publishes_separately_and_deterministically(m21_input, tmp_path):
    _, path = freeze_input({k: v for k, v in m21_input.items() if k != 'snapshot_id'}, None, tmp_path / 'inputs')
    first = run_m21(snapshot=str(path), output_dir=tmp_path / 'published')
    second = run_m21(snapshot=str(path), output_dir=tmp_path / 'published')
    assert first['result_hash'] == second['result_hash']
    assert first['run_directory'] != second['run_directory']
    assert first['verification_kind'] == 'offline_test' and 'offline_test' in first['run_directory']
    assert first['generation_status'] == 'ok' and first['counts']['candidate_count'] == 0
    from pathlib import Path
    markdown = Path(first['markdown']).read_text(encoding='utf-8')
    assert '明确结论数不表示每个资格字段均已补齐' in markdown
    assert '资格核验覆盖数只计所有必要资格字段均已判定的股票' in markdown
    assert '覆盖数不表示每个资格字段均已补齐' not in markdown


@pytest.mark.parametrize('extra', [dict(fetch_adjusted=True), dict(refresh_adjusted=True), dict(eligibility_evidence='unused.json'), dict(adjusted_manifest='unused.json')])
def test_replay_rejects_changed_evidence_or_fetch(m21_input, tmp_path, extra):
    _, path = freeze_input({k: v for k, v in m21_input.items() if k != 'snapshot_id'}, None, tmp_path)
    with pytest.raises(ValueError, match='重放快照'):
        run_m21(snapshot=str(path), output_dir=tmp_path, **extra)


def test_real_run_rejects_offline_evidence(small_market, tmp_path):
    store, config, _, _ = small_market
    strategy = tmp_path / 'config.json'
    strategy.write_text(M21Config(**config.model_dump()).model_dump_json(), encoding='utf-8')
    evidence = tmp_path / 'evidence.json'
    evidence.write_text(json.dumps({'schema_version': 'eligibility-evidence-v1', 'verification_kind': 'offline_test', 'sources': [], 'records': []}), encoding='utf-8')
    with pytest.raises(ValueError, match='离线测试资格证据'):
        run_m21(target_date=TARGET, database=store.path, config_path=strategy, eligibility_evidence=evidence, output_dir=tmp_path)


def test_research_reader_rejects_offline_database(small_market):
    store, config, _, _ = small_market
    with sqlite3.connect(store.path) as conn:
        conn.execute("INSERT OR REPLACE INTO market_metadata VALUES ('verification_kind','offline_test')")
    with pytest.raises(ValueError, match='离线测试'):
        read_market_input(store.path, TARGET, config)


@pytest.mark.parametrize('count', [6, 30, 100])
def test_configs_keep_original_quantitative_parameters(count):
    from ashare_daily.screening.settings import load_config
    old = load_config().model_dump()
    new = load_m21_config(f'config/m21_{count}.json').calculation_config().model_dump()
    for field in ('strategy_version', 'benchmark_id', 'min_history_trading_days', 'ma_short_days', 'ma_long_days', 'return_days', 'amount_days', 'min_avg_amount_cny', 'max_candidates', 'trend_adjustment_mode', 'display_adjustment_mode', 'allowed_exchanges', 'allowed_board'):
        assert new[field] == old[field]


def test_cli_invalid_snapshot_object_is_readable_error(tmp_path, capsys):
    path = tmp_path / 'bad.json'
    path.write_text('[]', encoding='utf-8')
    assert main(['brief', '--mode', 'research', '--snapshot', str(path)]) == 1
    assert '快照必须为 JSON 对象' in capsys.readouterr().err
