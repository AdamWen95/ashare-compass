"""Explicit observed-sample permission; never rewrite the frozen universe verdict."""
from collections import Counter
from datetime import date
import hashlib
import re

from .providers.base import SecurityIdentity
from .universe import _json, KNOWN_OTHER_TYPES


def assess_universe_coverage(universe):
    """Accept only enumerated prior-member gaps, not untrusted source boundaries.

    Source bytes are additionally revalidated by exchange_industry_snapshot.
    The strict universe flags and its persisted coverage baseline stay unchanged.
    """
    partial = not (universe.get('structural_verified') is True
        or universe.get('universe_verified') is True and universe.get('collection_ready') is True)
    missing = universe.get('changes', {}).get('missing_unexplained', [])
    if partial:
        body = {k:v for k,v in universe.items() if k not in {'content_hash','snapshot_id'}}
        hashed = hashlib.sha256(_json(body).encode()).hexdigest()
        day = universe.get('requested_date')
        if (universe.get('scope') != 'sse_szse_a' or universe.get('mode') not in {'research','offline_test'}
                or universe.get('calendar_verified') is not True or universe.get('resolved_trade_date') != day
                or universe.get('content_hash') != hashed
                or universe.get('snapshot_id') != 'universe-'+str(day)+'-'+hashed[:20]):
            raise ValueError('partial_universe_scope_date_or_hash_invalid')
        date.fromisoformat(day)
        if (not isinstance(missing,list) or not missing or len(missing)!=len(set(missing))
                or any(not isinstance(key,str) or not key for key in missing)
                or not universe.get('changes',{}).get('coverage_baseline_snapshot_id')):
            raise ValueError('partial_universe_missing_identity_invalid')
        blockers = universe.get('blockers', [])
        count_reason = 'previous_members_missing_unexplained:'+str(len(missing))
        if (count_reason not in blockers or any(reason != count_reason and not re.fullmatch(
                r'board_count_drop_unexplained:(sse_main|szse_main|star|chinext)',reason) for reason in blockers)):
            raise ValueError('partial_universe_hard_blocker')
        sources = universe.get('source_manifests', [])
        provenance = 'online' if universe['mode']=='research' else 'offline_test'
        if not sources or any(s.get('complete') is not True or s.get('permission_status')!='approved'
                or s.get('provenance_mode')!=provenance or s.get('errors') for s in sources):
            raise ValueError('partial_universe_source_unverified')
        ids, symbols, counts = set(), set(), Counter()
        for member in universe.get('members', []):
            identity, symbol = member.get('security_id'), (member.get('exchange'),member.get('code'))
            if (not identity or identity in ids or identity in missing or symbol in symbols
                    or member.get('metadata_verified') is not True or member.get('metadata_conflict')):
                raise ValueError('partial_universe_identity_unverified')
            ids.add(identity)
            symbols.add(symbol)
            if member.get('security_type') in KNOWN_OTHER_TYPES:
                continue
            SecurityIdentity(**{k:member[k] for k in
                ('security_id','code','exchange','board','security_type','metadata_verified')})
            classification=member.get('discovery_classification')
            if classification=='delisted_with_evidence':
                if (member.get('listing_status')!='delisted' or not member.get('delisting_date')
                        or date.fromisoformat(member['delisting_date'])>date.fromisoformat(day)):
                    raise ValueError('partial_universe_exit_date_unverified')
                continue
            if classification=='not_yet_listed':
                if not member.get('listing_date') or date.fromisoformat(member['listing_date'])<=date.fromisoformat(day):
                    raise ValueError('partial_universe_listing_date_unverified')
                continue
            if (member.get('listing_status')!='listed' or not member.get('listing_date')
                    or date.fromisoformat(member['listing_date'])>date.fromisoformat(day)):
                raise ValueError('partial_universe_listing_unverified')
            counts[member['board']]+=1
        if not counts or dict(counts)!=universe.get('board_counts') or sum(counts.values())!=universe.get('ordinary_a_count'):
            raise ValueError('partial_universe_count_invalid')
    elif missing or universe.get('blockers'):
        raise ValueError('partial_universe_verdict_conflict')
    count=universe.get('ordinary_a_count',0)
    return {'schema_version':'premarket-coverage-v1', 'status':'partial' if partial else 'complete',
        'scope':'observed_sample' if partial else 'verified_universe',
        'universe_snapshot_id':universe.get('snapshot_id'), 'observed_count':count,
        'missing_count':len(missing), 'missing_ids':sorted(missing),
        'reference_count':count+len(missing), 'reference_basis':'observed_plus_unexplained_prior_members',
        'universe_date':universe.get('requested_date'),
        'notice':'仅分析已取得且独立核验的证券；未解释缺失保留，不将样本统计称为全市场统计。' if partial else
                 '名单已核验；行情有效性与报告日证券资格仍分别核验。'}
