"""Adapt already frozen source evidence into an independent, replayable packet."""
from copy import deepcopy

from ..reference_strategy import validated_panel
from ..sector_selection import digest

INPUT_SCHEMA = 'premarket-shadow-inputs-v1'


def unavailable_inputs(selection_id, market_day, reason):
    return {'schema_version': INPUT_SCHEMA, 'selection_id': selection_id, 'market_as_of_date': market_day,
        'status': 'unavailable', 'issues': [reason], 'records': [], 'source_hashes': {},
        'pool_audit': {'status': 'unavailable', 'outside_pool_history': 'not_requested_by_design'}}


def build_shadow_inputs(selection, inputs, observation, reference):
    panel = validated_panel(selection, inputs, observation, allow_unverified_calendar=True)
    if (reference.get('selection_id') != selection['selection_id']
            or reference.get('trade_date') != selection['target_date']):
        raise ValueError('shadow_reference_binding_mismatch')
    scores = {r['security_id']: r for r in reference.get('records', [])}
    if len(scores) != len(reference.get('records', [])) or not set(scores) <= set(panel['members']):
        raise ValueError('shadow_duplicate_or_unknown_scores')
    days = panel['dates'][-120:]
    records = []
    for member in selection['members']:
        identity = member['security_id']
        data = panel['members'][identity]
        score = scores.get(identity, {})
        bars = [data['rows'][d] for d in days if d in data['rows']]
        ready = len(days) == len(bars) == 120 and not data['issues']
        records.append({'security_id': identity, 'symbol': data['symbol'],
            'bars': deepcopy(bars) if ready else [], 'window_hash': digest(bars) if ready else None,
            'issues': list(data['issues']) if ready else list(data['issues']) + ['完整120日历史未取得'],
            'relative_observation': deepcopy(member.get('relative_observation', {})),
            'old_score': score.get('score') if score.get('status') == 'available' else None})
    return {'schema_version': INPUT_SCHEMA, 'selection_id': selection['selection_id'],
        'market_as_of_date': selection['target_date'], 'status': 'ready', 'issues': [], 'records': records,
        'source_hashes': {'selection': selection['content_hash'], 'inputs': panel['source_input_hash'],
            'observation': panel['source_observation_hash'], 'reference': digest(reference)},
        'pool_audit': {'status': 'available', 'selection_rule': selection.get('rule_version'),
            'history_count': len(records), 'history_limit': 15, 'per_sector_limit': 5,
            'eligible_before_history': selection.get('observation_scope', {}).get('eligible_security_count'),
            'outside_pool_history': 'not_requested_by_design',
            'notice': '当前池优先单日表现；本对照不能检验池外整理股，不能声称全市场改进'}}
