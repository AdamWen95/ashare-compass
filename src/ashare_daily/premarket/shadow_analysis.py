"""Recompute setup diagnostics and same-cohort rankings; never select live stocks."""
from copy import deepcopy
from datetime import date
import re

from ..sector_selection import digest
from ..reference_indicators import _parse, score_reference_technical
from .contracts import decimal
from .shadow_inputs import INPUT_SCHEMA
from .shadow_scoring import VERSION, score_setup

SCHEMA = 'premarket-shadow-analysis-v1'
FAMILIES = ('trend_continuation', 'strong_pullback', 'rebound_watch')
NOTICE = '分类评分试运行：只比较同类研究对象，尚未验证效果，不改变正式名单；分数不是买点或上涨概率。'


def _require(condition):
    if not condition:
        raise ValueError('shadow_evidence_binding_invalid')


def build_shadow_analysis(report, source):
    _require(source.get('schema_version') == INPUT_SCHEMA
        and source.get('selection_id') == report.get('observation_selection_id')
        and source.get('market_as_of_date') == report['market_as_of_date'])
    audit = {r['security_id']: r for r in report.get('screening_audit', [])}
    records = source.get('records', [])
    _require(len(records) <= 15 and len(records) == len({r['security_id'] for r in records}))
    _require(source.get('status') in {'ready', 'unavailable'})
    _require(set(audit) == {r['security_id'] for r in records} if source['status'] == 'ready'
             else not records and bool(source.get('issues')))
    if source['status'] == 'ready':
        _require(set(source['source_hashes']) == {'selection', 'inputs', 'observation', 'reference'})
        _require(all(isinstance(h, str) and re.fullmatch('[a-f0-9]{64}', h) for h in source['source_hashes'].values()))
    shown = {r['security_id']: r for r in report.get('watch_candidates', [])}
    rejected = {r['security_id']: r for r in report.get('excluded_candidates', [])}
    direction_facts = {r['sector_id']: r['relative_metrics'] for r in report.get('directions', [])
                       if r.get('kind') == 'relative_strength'}
    output = []
    for item in records:
        identity = item['security_id']; row = audit[identity]
        _require(item['symbol'] == row['symbol'])
        relative = item['relative_observation']; sector = relative.get('sector_id')
        _require(relative.get('market_as_of_date') == report['market_as_of_date'] and sector in direction_facts)
        _require(relative.get('kind') == 'single_session_relative_strength')
        facts = direction_facts[sector]
        for key, field in [('sector_mean_change_pct', 'mean_change_pct'), ('market_mean_change_pct', 'market_mean_change_pct')]:
            _require(decimal(relative[key]) == decimal(facts[field]))
        _require(abs(decimal(relative['stock_change_pct']) - decimal(relative['sector_mean_change_pct'])
                     - decimal(relative['excess_sector_pp'], positive=True)) <= decimal('0.000000001'))
        old_score = item['old_score']
        _require(old_score is None or type(old_score) is int and 0 <= old_score <= 100)
        if identity in shown:
            _require(old_score == shown[identity].get('technical_score'))
        bars = item['bars']
        if bars:
            _require(len(bars) == 120 and digest(bars) == item['window_hash']
                and bars[-1]['trade_date'] == report['market_as_of_date'] and not item['issues'])
            _require(all(date.fromisoformat(b['trade_date']) <= date.fromisoformat(report['market_as_of_date']) for b in bars))
            _parse(bars)
            if old_score is not None:
                _require(score_reference_technical(bars)['score'] == old_score)
        else:
            _require(item['window_hash'] is None and bool(item['issues']))
        metrics = row.get('metrics') or {}
        calculated = score_setup(bars, relative_return_20=metrics.get('relative_return_20'),
                                 excess_sector_pp=relative['excess_sector_pp'])
        reasons = []
        failed = row.get('eligibility_status') == 'fail' or any(c.get('status') == 'fail' and c.get('required', True)
            for c in row.get('eligibility_conditions', []))
        if failed or row.get('technical_status') == 'not_applicable':
            reasons.append('qualification_failed')
        elif row.get('eligibility_status') != 'pass':
            reasons.append('qualification_pending')
        if row.get('technical_status') not in {'pass', 'fail'}:
            reasons.append('history_unverified')
        company = shown.get(identity, rejected.get(identity, {})).get('company_review', {})
        adverse = company.get('risk_status') == 'adverse' or any(e.get('validation_status') == 'verified'
            and e.get('risk_status') == 'adverse' and identity in e.get('security_ids', []) for e in report.get('overnight_events', []))
        if adverse:
            reasons.append('company_adverse')
        if calculated['score'] is None:
            reasons.append('setup_or_data_unavailable')
        if old_score is None:
            reasons.append('old_score_unavailable_for_paired_comparison')
        output.append({'security_id': identity, 'symbol': row['symbol'], 'name': row.get('name'),
            'sector_id': sector, 'old_score': old_score, **calculated, 'comparison_eligible': not reasons,
            'exclusion_reasons': reasons, 'company_status': 'reviewed' if company.get('status') == 'ready'
                and company.get('risk_status') == 'clear' and company.get('evidence_ids') else 'pending',
            'source_issues': deepcopy(item['issues']), 'relative_return_20': metrics.get('relative_return_20')})
    rankings = {}
    for family in FAMILIES:
        cohort = [r for r in output if r['setup'] == family and r['comparison_eligible']]
        orders = {'old_score': lambda r: (-r['old_score'], r['security_id']),
            'new_score': lambda r: (-r['score'], r['security_id']),
            'relative_strength': lambda r: (-decimal(r['relative_return_20']), r['security_id'])}
        rankings[family] = {'cohort_ids': sorted(r['security_id'] for r in cohort),
            **{key: [r['security_id'] for r in sorted(cohort, key=order)[:5]] for key, order in orders.items()}}
    return {'schema_version': SCHEMA, 'rule_version': VERSION, 'application': 'shadow_only',
        'changes_production_ranking': False, 'input_hash': digest(source), 'status': source['status'],
        'issues': deepcopy(source['issues']), 'records': output, 'rankings': rankings,
        'actual_watch_ids': list(shown), 'pool_audit': deepcopy(source['pool_audit']),
        'market_state': report['market_state']['state'], 'notice': NOTICE,
        'conclusion': 'evidence_insufficient_for_promotion'}


def attach_shadow_analysis(report, source):
    result = deepcopy(report)
    result['shadow_inputs'] = deepcopy(source)
    result['shadow_analysis'] = build_shadow_analysis(result, source)
    return result
