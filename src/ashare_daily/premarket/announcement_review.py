"""Date-precision intervals and evidence-bound local semantic review decisions."""
from copy import deepcopy
from datetime import date, datetime, time
import hashlib
import json
from pathlib import Path
import re

from .contracts import SHANGHAI, aware

VERSION = 'company-review-v2'


def decision_filename(announcement, candidate):
    binding = candidate['security_id']+'\n'+candidate['symbol']+'\n'+announcement['original_url']
    return announcement['content_hash']+'-'+hashlib.sha256(binding.encode()).hexdigest()[:16]+'.json'


def publication_scope(item, start, cutoff, observed_at):
    begin, end, seen = aware(start), aware(cutoff), aware(observed_at)
    if begin >= end or seen > end:
        raise ValueError('announcement_observed_after_cutoff')
    if item['publication_precision'] == 'datetime':
        stamp = aware(item['published_at'])
        return 'outside' if stamp < begin else 'future' if stamp > end else 'inside'
    if item['publication_precision'] != 'date':
        raise ValueError('announcement_precision_invalid')
    day = date.fromisoformat(item['published_at'])
    low, high = datetime.combine(day, time.min, SHANGHAI), datetime.combine(day, time.max, SHANGHAI)
    if high < begin:
        return 'outside'
    if low > end:
        return 'future'
    if low < begin:
        return 'start_boundary'
    return 'inside' if high <= end else 'observed_by_cutoff'


def apply_local_decision(announcement, candidate, text, directory, *, cutoff):
    result = deepcopy(announcement)
    result['semantic_status'] = 'pending'
    if directory is None:
        return result
    body_hash = announcement['content_hash']
    if not re.fullmatch('[a-f0-9]{64}', body_hash):
        raise ValueError('company_decision_content_hash_invalid')
    root = Path(directory).absolute(); path = root/decision_filename(announcement, candidate)
    if any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
        raise ValueError('company_decision_link_rejected')
    if not path.exists():
        return result
    if not path.is_file() or path.stat().st_size > 32768:
        raise ValueError('company_decision_file_invalid')
    value = json.loads(path.read_text(encoding='utf-8'))
    expected = {'schema_version': 'company-semantic-decision-v1', 'source_id': 'cninfo_company_announcements',
        'security_id': candidate['security_id'], 'symbol': candidate['symbol'],
        'content_hash': body_hash, 'text_hash': hashlib.sha256(text.encode()).hexdigest(),
        'original_url': announcement['original_url']}
    if any(value.get(k) != v for k, v in expected.items()):
        raise ValueError('company_decision_binding_mismatch')
    reviewed = aware(value['reviewed_at'])
    if not aware(announcement['first_seen_at']) <= reviewed <= aware(cutoff):
        # A later review cannot rewrite an earlier report. Keep the pending state.
        result['semantic_issue'] = '核查记录时间不属于本期，未用于本期结论'
        return result
    if (value.get('decision') not in {'no_adverse_identified', 'adverse', 'review_required'}
            or value.get('reviewer_kind') not in {'human', 'assistant_local_review'}
            or not isinstance(value.get('rationale'), str) or not 10 <= len(value['rationale']) <= 3000
            or not isinstance(value.get('quotes'), list) or not 1 <= len(value['quotes']) <= 8):
        raise ValueError('company_decision_schema_invalid')
    compact = re.sub(r'\s+', '', text)
    if any(not isinstance(q, str) or not 8 <= len(q) <= 500 or re.sub(r'\s+', '', q) not in compact for q in value['quotes']):
        raise ValueError('company_decision_quote_not_in_body')
    result['semantic_decision'] = value
    result['semantic_status'] = 'reviewed'
    result['notice'] = '已登记所列正文的语义核查；风险结论与资料完整性独立记录，不代表公司没有风险'
    result['risk_status'] = {'no_adverse_identified': 'clear', 'adverse': 'adverse', 'review_required': 'review_required'}[value['decision']]
    return result


def finalize_review(review):
    """Collection completeness and risk clearance are deliberately separate."""
    rows = review['announcements']
    bodies = sum(r.get('body_status') == 'extracted' for r in rows)
    reviewed = sum(r.get('semantic_status') == 'reviewed' for r in rows)
    directory_ok = review.get('window_status') == 'complete' and 'directory_evidence' in review
    collection_gaps = review.setdefault('collection_gaps', list(review['gaps']))
    collection_ok = directory_ok and not collection_gaps and bodies == len(rows)
    review['collection_status'] = 'complete' if collection_ok else 'partial' if 'directory_evidence' in review else 'unavailable'
    semantic_ok = reviewed == len(rows) and collection_ok and not review.get('semantic_issues')
    review['semantic_status'] = 'complete' if semantic_ok else 'pending'
    adverse = any(r.get('risk_status') == 'adverse' for r in rows)
    unresolved = any(r.get('risk_status') != 'clear' for r in rows)
    review['risk_status'] = 'adverse' if adverse else 'clear' if semantic_ok and not unresolved else 'review_required' if rows else 'unknown'
    review['status'] = 'ready' if semantic_ok else 'partial'
    review.pop('covered_until_at', None)
    if semantic_ok and review['risk_status'] == 'clear':
        review['covered_until_at'] = review['directory_evidence']['requested_at']
    review['review_version'] = VERSION
    review['progress'] = {'directory_status': 'complete' if directory_ok else 'partial',
        'announcement_count': len(rows), 'body_count': bodies, 'semantic_reviewed_count': reviewed,
        'body_pending_count': len(rows)-bodies, 'semantic_pending_count': len(rows)-reviewed}
    review['gaps'] = list(dict.fromkeys(collection_gaps + review.get('semantic_issues', [])))
    if rows and reviewed < len(rows):
        review['gaps'].append('公告语义核查尚未完成；下载和关键词定位不能替代核查决定')
    return review


def validate_review(review, *, cutoff, security_id, symbol):
    if review.get('review_version') != VERSION:
        return
    end = aware(cutoff)
    if not aware(review['query_start_at']) < aware(review['query_cutoff_at']) <= end:
        raise ValueError('company_review_window_invalid')
    for row in review['announcements']:
        if row.get('body_status') != 'extracted':
            if row.get('semantic_status') == 'reviewed':
                raise ValueError('company_review_without_body')
            continue
        if not aware(row['first_seen_at']) <= aware(row['fetched_at']) <= end:
            raise ValueError('company_review_body_after_cutoff')
        if row.get('semantic_status') == 'reviewed':
            note = row.get('semantic_decision', {})
            expected = {'security_id': security_id, 'symbol': symbol, 'original_url': row['original_url'],
                'content_hash': row['content_hash'], 'text_hash': row['text_hash'], 'source_id': 'cninfo_company_announcements'}
            if any(note.get(k) != v for k,v in expected.items()) or not aware(row['first_seen_at']) <= aware(note['reviewed_at']) <= end:
                raise ValueError('company_review_decision_binding_invalid')
            expected_risk={'no_adverse_identified':'clear','adverse':'adverse','review_required':'review_required'}.get(note.get('decision'))
            if expected_risk is None or row.get('risk_status') != expected_risk:
                raise ValueError('company_review_decision_risk_invalid')
    expected = finalize_review(deepcopy(review))
    for key in ('collection_status','semantic_status','progress'):
        if expected[key] != review.get(key):
            raise ValueError('company_review_progress_invalid')
    # Financial merging can add a pending status/risk but cannot promote it.
    if review.get('risk_status') == 'clear' and expected['risk_status'] != 'clear':
        raise ValueError('company_review_false_clearance')
    if review.get('status') == 'ready' and expected['status'] != 'ready':
        raise ValueError('company_review_false_completion')


def company_progress(review):
    """Report view model; financial 'available' means only declared provider fields."""
    progress = review.get('progress', {})
    return {**deepcopy(progress), 'collection_status': review.get('collection_status', 'unavailable'),
        'semantic_status': review.get('semantic_status', 'pending'),
        'financial_status': review.get('financial_status', 'unavailable'),
        'financial_gap_count': len(review.get('financial_gaps', [])),
        'processing_complete': review.get('collection_status') == 'complete' and review.get('semantic_status') == 'complete'
            and review.get('financial_status') == 'available',
        'window_review_complete': review.get('status') == 'ready' and review.get('risk_status') == 'clear'
            and bool(review.get('evidence_ids')),
        'scope_notice': '仅表示本期公告窗口及已接入财务字段核查，不代表全公司无风险'}
