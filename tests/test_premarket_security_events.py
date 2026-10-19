"""Reviewed listing exits have effective and first-observation time boundaries."""
from copy import deepcopy
import json

import pytest

from test_premarket_partial_coverage import partial_packet
from test_premarket_observation_selection import strict


def event():
    return {'event_id':'reviewed-exit','exchange':'SSE','code':'600000','security_id':'id-000',
        'name':'公司0','kind':'delisting_after_merger','effective_date':'2026-10-15',
        'published_date':'2026-10-12','first_observed_at':'2026-10-15T19:00:00+08:00',
        'verified_at':'2026-10-15T19:00:00+08:00','verification':'reviewed_primary_publication',
        'source_url':'https://www.sse.com.cn/disclosure/announcement/listing/stock/c/example.shtml'}


def test_listing_event_not_applied_before_effective_or_observed_time(tmp_path):
    from ashare_daily.premarket.security_events import load_security_events
    path=tmp_path/'config/premarket_security_events.json'
    path.parent.mkdir()
    path.write_text(json.dumps({'schema_version':'premarket-security-events-v1','events':[event()]}),encoding='utf-8')
    assert load_security_events(tmp_path,'2026-10-14','2026-10-16T09:00:00+08:00')==[]
    assert load_security_events(tmp_path,'2026-10-15','2026-10-15T18:00:00+08:00')==[]
    assert len(load_security_events(tmp_path,'2026-10-15','2026-10-15T20:00:00+08:00'))==1


def test_reviewed_exit_removes_candidate_without_rewriting_historical_quote():
    from ashare_daily.premarket.selection import build_observation_selection
    packet=partial_packet(); original=deepcopy(packet)
    selected=build_observation_selection(strict(packet)[0],packet,allow_partial=True,security_events=[event()])
    assert selected['members']==[]
    assert selected['relative_market_baseline']['sample_count']==10
    assert selected['research_coverage']['security_events']==[event()]
    assert packet==original


def test_exit_identity_conflict_is_rejected():
    from ashare_daily.premarket.selection import build_observation_selection
    packet=partial_packet(); e=event();e['security_id']='another'
    with pytest.raises(ValueError):
        build_observation_selection(strict(packet)[0],packet,allow_partial=True,security_events=[e])


def test_v4_archive_rejects_observation_of_effective_reviewed_exit():
    from test_premarket_observation_report import observation_brief
    from ashare_daily.premarket.report import validate_brief
    report=observation_brief()
    e=event()
    e.update(security_id=report['watch_candidates'][0]['security_id'],effective_date='2026-10-12',
        published_date='2026-10-10',first_observed_at='2026-10-12T08:30:00+08:00',
        verified_at='2026-10-12T08:30:00+08:00')
    report['schema_version']='premarket-brief-v4'
    report['rule_versions']['recommendation']='premarket-observation-v2'
    report['research_coverage']={'schema_version':'premarket-coverage-v1','status':'complete',
        'observed_count':10,'reference_count':10,'missing_count':0,'missing_ids':[],
        'quote_sample_count':10,'quote_missing_count':0,'quote_missing_ids':[],'halted_count':0,
        'comparison_scope':'verified_universe','security_events':[e],'confirmed_exit_ids':[]}
    for row in report['watch_candidates']:
        row['relative_observation']['comparison_scope']='verified_universe'
    for row in report['directions']:
        row['relative_metrics']['comparison_scope']='verified_universe'
    with pytest.raises(ValueError,match='exit'):
        validate_brief(report)
