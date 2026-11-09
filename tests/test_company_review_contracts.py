from copy import deepcopy
import json

import pytest

from ashare_daily.premarket.report import build_brief,validate_brief
from ashare_daily.premarket.announcement_review import company_progress,finalize_review
from test_premarket_state_report import state_brief
from test_shadow_analysis import fixture


def v7_brief():
    report,source=fixture()
    rows=deepcopy(report['watch_candidates'])
    review=rows[0]['company_review']
    review.update(query_start_at='2026-10-09T08:55:00+08:00',query_cutoff_at='2026-10-12T08:55:00+08:00',
        window_status='complete',directory_evidence={'requested_at':'2026-10-12T08:30:00+08:00'},
        announcements=[{'body_status':'extracted','semantic_status':'pending','risk_status':'review_required',
            'first_seen_at':'2026-10-12T08:31:00+08:00','fetched_at':'2026-10-12T08:31:00+08:00'},
            {'body_status':'unavailable','semantic_status':'pending'}],financial_status='available')
    for index,announcement in enumerate(review['announcements']):
        announcement.update(title='测试公告'+str(index),published_at='2026-10-10',
            original_url='https://static.cninfo.com.cn/finalpage/2026-10-10/'+str(index)+'.pdf')
    finalize_review(review)
    pre={'observation_only':True,'rule_version':'premarket-observation-v1','selection_id':report['observation_selection_id'],
        'market_as_of_date':report['market_as_of_date'],'directions':report['directions'],'audit':report['screening_audit']}
    from test_premarket_timing import resolve
    return build_brief(schedule=resolve(),market=report['market'],market_state=report['market_state'],
        candidates={'preselection':pre,'watch_candidates':rows},events=[],coverage=report['coverage'],previous=None,
        generated_at=report['generated_at'],shadow_inputs=source,company_review_version='company-review-v2')


def test_v7_preserves_v6_shadow_and_has_recomputable_company_progress():
    report=v7_brief();validate_brief(report)
    assert report['schema_version']=='premarket-brief-v7'
    assert report['company_review_progress']['watch-id']['body_count']==1
    report['company_review_progress']['watch-id']['window_review_complete']=True
    with pytest.raises(ValueError,match='company_progress'):
        validate_brief(report)


def test_work_completed_and_risk_pending_are_not_collapsed_to_clear():
    review={'announcements':[{'body_status':'extracted','semantic_status':'reviewed','risk_status':'review_required'}],
        'gaps':[],'window_status':'complete','directory_evidence':{'requested_at':'2026-11-07T08:30:00+08:00'}}
    finalize_review(review)
    assert review['status']=='ready' and review['risk_status']=='review_required'
    assert review['semantic_status']=='complete' and 'covered_until_at' not in review


def test_real_adverse_decision_still_vetoes_candidate():
    from ashare_daily.premarket.observation_candidates import finalize_observations
    from test_premarket_observation_candidates import inputs
    from ashare_daily.premarket.candidates import preselect_candidates
    pre=preselect_candidates(*inputs());identity=pre['candidates'][0]['security_id']
    result=finalize_observations(pre,company_reviews={identity:{'status':'ready','risk_status':'adverse'}},stance={},events=[])
    assert identity not in {r['security_id'] for r in result['watch_candidates']}
    assert any(r['security_id']==identity for r in result['excluded'])


def test_source_collection_can_complete_without_semantic_clearance():
    from ashare_daily.premarket.source_policy import coverage_summary
    reg={'sources':[{'registration':{'source_id':'s','enabled':True},'research_roles':['company']}]}
    c={'source_health':[{'source_id':'s','status':'partial','collection_status':'complete',
        'semantic_review_required':True,'failures':['semantics pending']}]}
    result=coverage_summary(reg,c,required_roles=['company'])
    assert not result['ready']
    assert result['roles']['company']['collection_status']=='complete'
    assert result['roles']['company']['semantic_status']=='pending'


def test_v7_archive_and_followup_keep_v6_readable(tmp_path):
    from ashare_daily.premarket.archive import publish_brief,scan_briefs
    from test_premarket_report import DUE
    from test_shadow_analysis import shadow_brief
    from ashare_daily.premarket.shadow_followup import compare_shadow_reports
    a=publish_brief(tmp_path,shadow_brief(),now=DUE)
    from pathlib import Path
    p=Path(a['directory'])/'premarket_brief.json';before=p.read_bytes()
    publish_brief(tmp_path,v7_brief(),now=DUE)
    reports,errors=scan_briefs(tmp_path/'outputs',now=DUE)
    assert not errors and {r['report']['schema_version'] for r in reports}=={'premarket-brief-v6','premarket-brief-v7'}
    assert before==p.read_bytes()
    result=compare_shadow_reports([r['report'] for r in reports],[],as_of='2026-11-07')
    assert result['ranking_change_allowed'] is False


def test_partial_status_opt_in_preserves_hash_and_source_checks():
    from ashare_daily.providers.sector_status import _universe_date
    from test_premarket_partial_coverage import partial_packet
    u=partial_packet()['universe'];day=u['requested_date']
    with pytest.raises(ValueError):_universe_date(u,day,None)
    assert _universe_date(u,day,None,allow_partial_universe=True)==day
    for field,value in [('content_hash','a'*64),('calendar_verified',False),('source_manifests',[])]:
        bad=deepcopy(u);bad[field]=value
        with pytest.raises(ValueError):_universe_date(bad,day,None,allow_partial_universe=True)


def test_partial_bridge_still_checks_full_dated_sdk_proof(bridge,monkeypatch):
    from test_observation_quote_status import fake_sdk
    from ashare_daily.operations.observation import supplement_zero_quotes
    from test_premarket_observation_selection import freeze_universe
    from test_sector_status import DAY
    from collections import Counter
    root,packet,_=bridge;u=packet['universe']
    u.update(structural_verified=False,universe_verified=False,collection_ready=False,
        blockers=['previous_members_missing_unexplained:1'],changes={'missing_unexplained':['absent'],'coverage_baseline_snapshot_id':'old'},
        board_counts=dict(Counter(m['board'] for m in u['members'])),
        source_manifests=[{'complete':True,'permission_status':'approved','provenance_mode':'online'}])
    freeze_universe(u)
    calls,_=fake_sdk(monkeypatch)
    result=supplement_zero_quotes(root,packet,DAY,root/'offline_test/partial-status',max_seconds=10,max_queries=4,allow_partial_universe=True)
    assert len(calls)==1 and result['quotes']['rows'][0]['full_day_halt_evidence']['verified']
    assert result['universe']==packet['universe'] and not result['universe']['universe_verified']


from test_observation_quote_status import bridge,source_fixture


def test_reviewed_listing_exit_records_do_not_backdate_first_observation():
    from pathlib import Path
    from ashare_daily.premarket.security_events import load_security_events
    root=Path(__file__).resolve().parents[1]
    old=load_security_events(root,'2026-11-07','2026-11-07T08:55:00+08:00')
    fresh=load_security_events(root,'2026-11-07','2026-11-07T23:59:59+08:00')
    assert {'000016','301139'}.isdisjoint({r['code'] for r in old})
    assert {'000016','301139'} <= {r['code'] for r in fresh}
    for event in fresh:
        if event['code'] in {'000016','301139'}:
            assert event['first_observed_at'].startswith('2026-11-07')
            assert event['kind']=='delisting'


def test_listing_decision_does_not_accept_unreviewed_or_third_party_sources():
    from ashare_daily.premarket.security_events import validate_security_event
    from test_premarket_security_events import event
    e=event();e.update(kind='delisting',source_url='https://disc.static.szse.cn/download/example.PDF')
    validate_security_event(e)
    for key,value in [('verification','headline_only'),('source_url','https://example.com/a.PDF'),('kind','delisting_period')]:
        bad={**e,key:value}
        with pytest.raises(ValueError):validate_security_event(bad)
