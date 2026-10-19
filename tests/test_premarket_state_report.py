"""New-state reports preserve frozen legacy editions and expose separate panels."""
from copy import deepcopy
from datetime import datetime

import pytest
from streamlit.testing.v1 import AppTest

from test_premarket_market_states import state_market
from test_premarket_observation_report import observation_inputs, observation_brief
from test_premarket_report import STAMP, DUE
from test_premarket_timing import resolve
from premarket_ui_helpers import morning_content


def state_brief(kind='weak_repair'):
    from ashare_daily.premarket.market_states import classify_market_state
    from ashare_daily.premarket.report import build_brief
    _,candidates=observation_inputs()
    market=state_market(kind,'2026-10-09')
    assessment=classify_market_state(market,news_ready=False,events=[])
    return build_brief(schedule=resolve(),market=market,market_state=assessment,candidates=candidates,
        events=[],coverage={'ready':False},previous=None,generated_at=STAMP)


def test_new_report_has_state_without_old_action_enum_and_keeps_stock_observations():
    from ashare_daily.premarket.report import report_sections
    report=state_brief()
    assert report['schema_version']=='premarket-brief-v5'
    assert report['market_state']['state']=='weak_repair'
    assert 'stance' not in report and 'stance' not in report['rule_versions']
    assert len(report['watch_candidates'])==1
    headings=[name for name,_ in report_sections(report)]
    assert headings[:3]==['市场状态与时间','依据范围','风险提示']


@pytest.mark.parametrize('field',['state','label','reasons','risk_notes','evidence_basis'])
def test_archive_validation_recomputes_state_basis_and_risk(field):
    from ashare_daily.premarket.report import validate_brief
    report=state_brief()
    report['market_state'][field]={'state':'trend_strength','label':'趋势偏强','reasons':[],
        'risk_notes':[],'evidence_basis':{}}[field]
    with pytest.raises(ValueError,match='state'):
        validate_brief(report)


def test_new_and_old_editions_roundtrip_without_rewriting_old_bytes(tmp_path):
    from ashare_daily.premarket.archive import publish_brief,scan_briefs
    from pathlib import Path
    old=publish_brief(tmp_path,observation_brief(),now=DUE)
    oldpath=Path(old['directory'])/'premarket_brief.json';before=oldpath.read_bytes()
    new=publish_brief(tmp_path,state_brief(),now=DUE)
    reports,errors=scan_briefs(tmp_path/'outputs',now=DUE)
    assert not errors and {r['report']['schema_version'] for r in reports}=={'premarket-brief-v3','premarket-brief-v5'}
    assert oldpath.read_bytes()==before and old['report_id']!=new['report_id']


def test_viewer_exposes_separate_state_basis_risk_and_candidate_panels(tmp_path):
    from ashare_daily.premarket.archive import publish_brief
    publish_brief(tmp_path,state_brief(),now=DUE)
    script=f'''
from pathlib import Path
from datetime import datetime
import streamlit as st
from ashare_daily.premarket.archive import scan_briefs
from ashare_daily.viewer_premarket import render_morning
root=Path({str(tmp_path/'outputs')!r})
now=datetime.fromisoformat('2026-10-12T09:00:00+08:00')
archives,errors=scan_briefs(root,now=now)
render_morning(st,archives,root,now=now,runs=[])
'''
    app=AppTest.from_string(script).run()
    assert not app.exception
    content=morning_content(app)
    headings=content.headings
    assert '依据范围' in headings and '风险提示' in headings and any('观察名单与风险' in h for h in headings)
    text=''.join(content.parts)
    assert '市场状态：弱势修复' in text
    assert '按兵不动' not in text and '减仓防守' not in text


def test_scheduled_pipeline_uses_state_rules_even_when_model_skips(tmp_path):
    from test_premarket_daily import setup,Clock,Services,invoke
    from ashare_daily.premarket.archive import scan_briefs
    class StateServices(Services):
        def market(self,schedule,directory,seconds,run_id):
            self.clock.value=datetime.fromisoformat('2026-10-12T08:50:00+08:00')
            return {'market':state_market('weak_repair','2026-10-09'),
                'preselection':{'observation_only':True,'rule_version':'premarket-observation-v1',
                    'market_as_of_date':'2026-10-09','candidates':[],'directions':[],'pending':[],'excluded':[]},
                'company_reviews':{},'entities':{}}
    clock=Clock();result=invoke(tmp_path,setup(tmp_path),StateServices(clock),clock)
    assert result['generation_status']=='ok'
    assert result['module_statuses']['decision']=='ok'
    assert result['model_summary']['status']=='skipped'
    reports,errors=scan_briefs(tmp_path/'outputs',now=clock.now())
    assert not errors and reports[0]['report']['market_state']['state']=='weak_repair'
