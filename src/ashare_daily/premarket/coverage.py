"""Coverage contracts and reader-facing wording for partial research editions."""
from copy import deepcopy
import hashlib


def market_basis_universe(packet, market_date):
    """A report-day universe cannot silently become a historical market denominator."""
    from ..universe import _json
    if 'market_universe' in packet:
        previous=packet['market_universe']
        body={k:v for k,v in previous.items() if k not in {'content_hash','snapshot_id'}}
        hashed=hashlib.sha256(_json(body).encode()).hexdigest()
        if (previous.get('requested_date')!=market_date or previous.get('resolved_trade_date')!=market_date
                or previous.get('scope')!='sse_szse_a' or previous.get('structural_verified') is not True
                or previous.get('calendar_verified') is not True or previous.get('blockers')
                or previous.get('mode')!=packet['universe'].get('mode')
                or previous.get('mode') not in {'research','offline_test'}
                or previous.get('mode')=='research' and (previous.get('universe_verified') is not True
                    or previous.get('collection_ready') is not True)
                or previous.get('content_hash')!=hashed
                or previous.get('snapshot_id')!='universe-'+market_date+'-'+hashed[:20]):
            raise ValueError('premarket_historical_universe_invalid')
        sources=previous.get('source_manifests',[])
        provenance='online' if previous['mode']=='research' else 'offline_test'
        if not sources or any(s.get('complete') is not True or s.get('permission_status')!='approved'
                or s.get('provenance_mode')!=provenance or s.get('errors') for s in sources):
            raise ValueError('premarket_historical_source_invalid')
        return previous
    value=deepcopy(packet['universe'])
    if value.get('requested_date')!=market_date:
        value['structural_verified']=False
    return value


def validate_coverage(value):
    if value.get('schema_version')!='premarket-coverage-v1':
        raise ValueError('premarket_coverage_schema_invalid')
    for key in ('observed_count','reference_count','missing_count','quote_sample_count','quote_missing_count','halted_count'):
        if type(value.get(key)) is not int or value[key]<0:
            raise ValueError('premarket_coverage_count_invalid')
    for count,ids in (('missing_count','missing_ids'),('quote_missing_count','quote_missing_ids')):
        rows=value.get(ids)
        if not isinstance(rows,list) or len(rows)!=value[count] or len(rows)!=len(set(rows)):
            raise ValueError('premarket_coverage_ids_invalid')
    if (value['reference_count']!=value['observed_count']+value['missing_count']
            or value['quote_sample_count']+value['quote_missing_count']+value['halted_count']!=value['observed_count']):
        raise ValueError('premarket_coverage_denominator_invalid')
    sample=value['missing_count']>0 or value['quote_missing_count']>0
    if value.get('comparison_scope')!=('observed_sample' if sample else 'verified_universe'):
        raise ValueError('premarket_coverage_scope_invalid')
    from .security_events import validate_security_event
    for event in value.get('security_events',[]):
        validate_security_event(event)
    confirmed=value.get('confirmed_exit_ids',[])
    expected=sorted(set(value['missing_ids']) & {e['security_id'] for e in value.get('security_events',[])})
    if confirmed!=expected:
        raise ValueError('premarket_coverage_exit_binding_invalid')


def coverage_notice(value):
    validate_coverage(value)
    confirmed=len(value.get('confirmed_exit_ids',[]))
    return (f"证券覆盖：当前名单{value['observed_count']}只，相对历史基线退出或缺失{value['missing_count']}只，"
        f"其中已核实退出{confirmed}只、仍待解释{value['missing_count']-confirmed}只；"
        f"有效行情样本{value['quote_sample_count']}只，已核验全日停牌{value['halted_count']}只，"
        f"行情缺失或未通过校验{value['quote_missing_count']}只。"
        + ('方向和股票的相对表现仅在已核验样本中比较，不代表全市场排名；缺失证券可能改变结果。'
           if value['comparison_scope']=='observed_sample' else '相对表现基于本期已核验证券范围。'))


def comparison_label(facts):
    return '已核验样本' if facts.get('comparison_scope')=='observed_sample' else '全市场'
