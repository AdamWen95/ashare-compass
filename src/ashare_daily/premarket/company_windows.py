"""Advance only a company's fully reviewed, source-bound window, never a global gap."""
from .contracts import aware


def company_query_windows(archives, candidates, *, start, cutoff):
    begin, end = aware(start), aware(cutoff)
    result = {}
    for candidate in candidates:
        for archive in archives[:40]:
            report = archive['report']
            if not report.get('published_at') or aware(report['published_at']) >= end:
                continue
            rows = report.get('watch_candidates', report.get('focus_candidates', []))
            row = next((r for r in rows if r.get('security_id') == candidate['security_id']
                        and r.get('symbol') == candidate['symbol'] and r.get('name') == candidate['name']), None)
            review = row.get('company_review', {}) if row else {}
            if review.get('status') != 'ready' or review.get('risk_status') != 'clear' or not review.get('evidence_ids'):
                continue
            try:
                origin = aware(review.get('coverage_origin_at', review['query_start_at']))
                covered = aware(review['covered_until_at'])
                requested = aware(review['directory_evidence']['requested_at'])
                if not origin <= begin < covered == requested <= aware(report['published_at']) < end:
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            old = result.get(candidate['security_id'])
            if old and aware(old['start_at']) >= covered:
                continue
            result[candidate['security_id']] = {'start_at': covered.isoformat(), 'coverage_origin_at': origin.isoformat(),
                'prior_window_evidence': {'report_id': report['report_id'], 'covered_until_at': covered.isoformat(),
                    'evidence_ids': list(review['evidence_ids'])}}
    return result
