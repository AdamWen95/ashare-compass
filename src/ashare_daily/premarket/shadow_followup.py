"""Frozen, paired forward price descriptions. No trading simulation or promotion."""
from collections import defaultdict
from datetime import date, datetime, time
from decimal import Decimal

from ..reference_strategy import load_reference_inputs, validated_panel
from ..sector_selection import digest
from .contracts import SHANGHAI, SHADOW_SCHEMAS, aware, decimal
from .report import validate_brief

HORIZONS = (3, 5, 10)
METHODS = ('old_score', 'new_score', 'relative_strength')


def read_followup_evidence(directories):
    windows = []
    for directory in directories:
        selection, inputs, observation = load_reference_inputs(directory)
        panel = validated_panel(selection, inputs, observation)
        windows.append({'source_hash': panel['source_input_hash'], 'observed_at': inputs['cutoff_at'],
            'mode': selection['mode'], 'dates': panel['dates'], 'members': panel['members'],
            'adjustment_versions': {identity: packet.get('adjustment_window', {}).get('window_id')
                for identity, packet in inputs['securities'].items() if packet.get('adjustment_window')}})
    return windows


def _calendar(windows, anchor, as_of):
    relevant = [w['dates'] for w in windows if w['dates'] and w['dates'][0] <= anchor <= w['dates'][-1]]
    if not relevant:
        return [], False
    for left in relevant:
        for right in relevant:
            start, end = max(left[0], right[0]), min(left[-1], right[-1])
            if [d for d in left if start <= d <= end] != [d for d in right if start <= d <= end]:
                raise ValueError('shadow_followup_calendar_conflict')
    longest = max(relevant, key=lambda ds: ds[-1])
    if anchor not in longest:
        raise ValueError('shadow_followup_anchor_not_session')
    return [d for d in longest if anchor < d <= as_of], longest[-1] >= as_of


def _price_path(identity, dates, windows):
    choices = []
    for window in windows:
        stock = window['members'].get(identity)
        version = window['adjustment_versions'].get(identity)
        if not stock or stock['issues'] or not version or not all(d in stock['rows'] for d in dates):
            continue
        try:
            # One complete version supplies the entire forward path. Never join
            # anchor/future closes from different adjustment windows.
            values = [decimal(stock['rows'][d]['close'], positive=True) for d in dates]
        except (ValueError, KeyError, TypeError):
            continue
        choices.append((aware(window['observed_at']), window['source_hash'], version, values))
    if not choices:
        return None
    latest = max(row[0] for row in choices)
    choices = [r for r in choices if r[0] == latest]
    # Different versions can rescale prices. Compare normalized paths rather
    # than mixing prices or treating consistent corporate-action scales as conflicts.
    normalized = [[p/r[3][0] for p in r[3]] for r in choices]
    if any(any(abs(a-b) > Decimal('0.00000001') for a, b in zip(path, normalized[0])) for path in normalized[1:]):
        return {'status': 'conflicting_data'}
    _, source_hash, version, values = sorted(choices, key=lambda r: r[1])[0]
    changes = [(p/values[0]-1)*100 for p in values]
    peak, drawdown = values[0], Decimal(0)
    for p in values:
        peak = max(peak, p)
        drawdown = min(drawdown, (p/peak-1)*100)
    return {'status': 'observed', 'change_pct': str(changes[-1]),
        'worst_close_change_pct': str(min(changes)), 'max_close_drawdown_pct': str(drawdown),
        'source_hash': source_hash, 'adjustment_version': version}


def observe_shadow_report(report, windows, *, as_of):
    validate_brief(report)
    if report['schema_version'] not in SHADOW_SCHEMAS:
        raise ValueError('shadow_followup_requires_v6')
    end = date.fromisoformat(as_of)
    end_at = datetime.combine(end, time.max, SHANGHAI)
    anchor = report['target_session_date']
    anchor_close = datetime.combine(date.fromisoformat(anchor), time(15), SHANGHAI)
    published = report.get('published_at')
    blocked = None
    if not published:
        blocked = 'unpublished'
    elif aware(published) >= anchor_close:
        blocked = 'published_after_anchor_close'
    elif aware(published) > end_at:
        blocked = 'report_not_yet_available'
    usable = [w for w in windows if aware(w['observed_at']) <= end_at]
    days, complete = _calendar(usable, anchor, as_of) if not blocked else ([], False)
    groups = report['shadow_analysis']['rankings']
    identities = set(report['shadow_analysis']['actual_watch_ids'])
    for group in groups.values():
        for method in METHODS:
            identities.update(group[method])
    records = []
    for horizon in HORIZONS:
        target = days[horizon-1] if len(days) >= horizon else None
        path_dates = [anchor, *days[:horizon]]
        for identity in sorted(identities):
            row = {'security_id': identity, 'horizon': horizon, 'anchor_date': anchor, 'target_date': target,
                'status': blocked or ('not_matured' if complete else 'calendar_incomplete'),
                'change_pct': None, 'worst_close_change_pct': None, 'max_close_drawdown_pct': None,
                'industry_relative_status': 'unavailable_no_independent_sector_series',
                'industry_excess_pct': None}
            if not blocked and target:
                row.update(_price_path(identity, path_dates, usable) or {'status': 'missing_data'})
            records.append(row)
    return {'report_id': report.get('report_id'), 'report_date': report['report_date'],
        'report_hash': digest(report), 'anchor_date': anchor, 'published_at': published,
        'market_state': report['market_state']['state'], 'rankings': groups,
        'actual_watch_ids': report['shadow_analysis']['actual_watch_ids'], 'records': records,
        'excluded_reason': blocked, 'analysis_status': report['shadow_analysis']['status']}


def compare_shadow_reports(reports, windows, *, as_of):
    anchors, excluded, seen = [], [], set()
    for report in sorted(reports, key=lambda r: (r['target_session_date'], r.get('published_at') or '')):
        result = observe_shadow_report(report, windows, as_of=as_of)
        key = result['anchor_date']
        if result['excluded_reason']:
            excluded.append({'report_id': result['report_id'], 'reason': result['excluded_reason']})
        elif key in seen:
            excluded.append({'report_id': result['report_id'], 'reason': 'duplicate_target_session'})
        else:
            seen.add(key); anchors.append(result)
    groups = defaultdict(lambda: {'included_anchors': 0, 'excluded_anchors': 0, 'values': {m: [] for m in METHODS}})
    stability, previous = [], None
    for anchor in anchors:
        ids = set(anchor['actual_watch_ids'])
        if previous is not None:
            union = ids | previous
            stability.append({'anchor_date': anchor['anchor_date'], 'retained_count': len(ids & previous),
                'added_count': len(ids-previous), 'removed_count': len(previous-ids),
                'jaccard': str(Decimal(len(ids & previous))/len(union)) if union else None})
        previous = ids
        rows = {(r['security_id'], r['horizon']): r for r in anchor['records']}
        for family, ranking in anchor['rankings'].items():
            for horizon in HORIZONS:
                group = groups[(anchor['market_state'], family, horizon)]
                needed = set().union(*(set(ranking[m]) for m in METHODS))
                if not needed or any(rows[(i, horizon)]['status'] != 'observed' for i in needed):
                    group['excluded_anchors'] += 1
                    continue
                group['included_anchors'] += 1
                for method in METHODS:
                    selected = [rows[(i, horizon)] for i in ranking[method]]
                    group['values'][method].append({metric: sum(decimal(r[metric]) for r in selected)/len(selected)
                        for metric in ('change_pct', 'worst_close_change_pct', 'max_close_drawdown_pct')})
    summary = []
    for (state, family, horizon), group in sorted(groups.items()):
        values = group.pop('values')
        summary.append({'market_state': state, 'setup': family, 'horizon': horizon, **group,
            'methods': {method: {metric: str(sum(v[metric] for v in samples)/len(samples)) for metric in samples[0]}
                if samples else None for method, samples in values.items()}})
    return {'schema_version': 'premarket-shadow-followup-v1', 'as_of': as_of,
        'mode': 'research' if windows and all(w['mode'] == 'research' for w in windows) else 'offline_or_no_evidence',
        'measure': 'post_publication_anchor_close_changes_not_executable_returns',
        'status': 'observed' if any(g['included_anchors'] for g in summary) else 'insufficient_evidence',
        'anchors': anchors, 'excluded_reports': excluded, 'groups': summary, 'watchlist_stability': stability,
        'evidence_hashes': sorted({w['source_hash'] for w in windows}), 'ranking_change_allowed': False,
        'conclusion': 'evidence_insufficient_for_promotion',
        'limitations': ['未建模交易成本或成交可行性，不是账户收益',
            '仅检验原观察池，不能识别池外机会；行业独立价格序列尚未取得',
            '重叠观察周期不构成独立试验；无有效性自动晋级或胜率校准'],
        'network_requests': 0, 'model_calls': 0}
