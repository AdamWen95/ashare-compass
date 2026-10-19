"""Presentation preserves frozen facts, visible risks and safe disclosure links."""
from copy import deepcopy
from html.parser import HTMLParser
import json

import pytest

from test_premarket_state_report import state_brief


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text=[];self.visible=[];self.unsafe=[];self.links=[];self.details=0;self.summary=0;self.ids=[]
        self.metrics=[];self.metric_depth=0;self.metric_text=[]

    def handle_starttag(self,tag,attrs):
        values=dict(attrs)
        if tag=='div':
            if self.metric_depth:self.metric_depth+=1
            elif 'pmr-metric' in values.get('class','').split():self.metric_depth=1;self.metric_text=[]
        if tag=='details':self.details+=1
        if tag=='summary':self.summary+=1
        if tag in {'script','iframe','img'} or any(key.startswith('on') for key in values):self.unsafe.append(tag)
        if tag=='a':self.links.append(values.get('href',''))
        if 'id' in values:self.ids.append(values['id'])

    def handle_endtag(self,tag):
        if tag=='div' and self.metric_depth:
            self.metric_depth-=1
            if not self.metric_depth:self.metrics.append(' '.join(self.metric_text))
        if tag=='details':self.details-=1
        if tag=='summary':self.summary-=1

    def handle_data(self,value):
        self.text.append(value)
        if self.metric_depth:self.metric_text.append(value)
        if not self.details or self.summary>=self.details:self.visible.append(value)


def parse(text):
    result=Page();result.feed(text);return result


def test_cards_preserve_reasons_conditions_and_risks_without_changing_report():
    from ashare_daily.premarket.presentation import render_body
    report=state_brief()
    row=report['watch_candidates'][0]
    row['company_review']['financial_risk_flags']=['来源净利润为负，需核查亏损原因及持续性。']
    row['score_components']=[{'label':'趋势条件','points':12,'reason':'已核验历史量价'}]
    before=deepcopy(report)
    page=parse(render_body(report))
    all_text=' '.join(page.text);visible=' '.join(page.visible)
    for value in row['reasons']+row['watch_conditions']+row['invalidation_conditions']+row['observation_risks']:
        assert value in all_text
    assert '盈利质量待核验' in all_text and '已核验历史量价' in all_text
    assert '来源净利润为负，需核查亏损原因及持续性。' in visible
    assert '依据范围' in visible and '风险提示' in visible and '弱势修复' in visible
    assert all_text.index('市场状态')<all_text.index('相对表现观察方向')<all_text.index('观察名单与风险')
    assert report==before
    assert len(page.ids)==len(set(page.ids))


def test_untrusted_content_and_links_cannot_execute_or_load_remote_assets():
    from ashare_daily.premarket.presentation import render_body
    report=state_brief();payload='<img src="https://bad.invalid/pixel" onerror="bad()">'
    report['watch_candidates'][0]['name']=payload
    report['watch_candidates'][0]['observation_risks'].append(payload)
    report['overnight_events']=[{'event_id':'untrusted','text':payload,'sources':[
        {'original_url':'javascript:alert(1)','published_at':'unknown'},
        {'original_url':'https://user:secret@example.invalid','published_at':'unknown'},
        {'original_url':'https://www.sse.com.cn/example','published_at':'2026-10-19'}]}]
    rendered=render_body(report);page=parse(rendered)
    assert not page.unsafe and payload not in rendered
    assert payload in ''.join(page.text)
    assert page.links==['https://www.sse.com.cn/example']


def test_missing_metric_is_not_zero_and_score_zero_is_not_missing():
    from ashare_daily.premarket.presentation import render_body
    report=state_brief();report['market']['benchmark']=None
    report['watch_candidates'][0]['technical_score']=0
    page=parse(render_body(report));text=' '.join(page.text)
    assert '未取得' in text and '0 分' in text
    assert any('上证指数 · 5日 未取得' in value for value in page.metrics)
    assert any('指数成交额比率 未取得' in value for value in page.metrics)


def test_frozen_score_precision_and_missing_verification_are_not_reinterpreted():
    from ashare_daily.premarket.presentation import stock_card
    page=parse(stock_card({'name':'历史观察','technical_score':'61.5','eligibility_status':'pass',
        'research_status':'focus','company_review':{}}))
    content=' '.join(page.text)
    assert '61.5 分' in content
    assert '本期已核查' not in content and '资料待核' not in content


def test_direction_sample_scope_and_empty_states_remain_explicit():
    from ashare_daily.premarket.presentation import render_body
    report=state_brief()
    report['directions'][0]['relative_metrics']['comparison_scope']='observed_sample'
    text=' '.join(parse(render_body(report)).text)
    assert '已核验样本' in text
    report['directions']=[];report['watch_candidates']=[]
    empty=' '.join(parse(render_body(report)).text)
    assert '暂无' in empty and '不强行' in empty


def test_empty_watchlist_is_neutral_and_research_boundary_stays_visible():
    from ashare_daily.premarket.presentation import render_body
    report=state_brief()
    report['directions']=[];report['watch_candidates']=[];report['screening_audit']=[]
    visible=' '.join(parse(render_body(report)).visible)
    assert '暂无符合条件' not in visible
    assert '观察名单为空，不强行补足' in visible
    assert '观察名单独立于市场状态' in visible
    assert '名单内只列研究条件，不构成买入指令' in visible


def test_document_and_streamlit_use_same_content_and_exports_stay_immutable(tmp_path,monkeypatch):
    from ashare_daily.premarket.presentation import render_body,render_document
    from ashare_daily.premarket.archive import publish_brief,scan_briefs
    from test_premarket_report import DUE
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    published=publish_brief(tmp_path,state_brief(),now=DUE)
    archives,_=scan_briefs(tmp_path/'outputs',now=DUE);report=archives[0]['report']
    before={p.name:p.read_bytes() for p in archives[0]['directory'].iterdir()}
    downloads={};real=st.download_button
    def capture(label,data,*args,**kwargs):
        downloads[kwargs['key']]=data
        return real(label,data,*args,**kwargs)
    monkeypatch.setattr(st,'download_button',capture)
    script=f'''from pathlib import Path
from datetime import datetime
import streamlit as st
from ashare_daily.premarket.archive import scan_briefs
from ashare_daily.viewer_premarket import render_morning
root=Path({str(tmp_path/'outputs')!r});now=datetime.fromisoformat({DUE.isoformat()!r})
archives,_=scan_briefs(root,now=now)
render_morning(st,archives,root,now=now,runs=[])
'''
    app=AppTest.from_string(script).run();assert not app.exception
    bodies=[node.proto.body for node in app.get('html')]
    assert render_body(report) in bodies
    assert downloads['premarket_download_premarket_brief.html'].decode()==render_document(report)
    for name in ['premarket_brief.json','premarket_brief.md']:
        assert downloads['premarket_download_'+name]==before[name]
    assert {p.name:p.read_bytes() for p in archives[0]['directory'].iterdir()}==before
