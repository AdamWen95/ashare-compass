from copy import deepcopy
from datetime import datetime
import hashlib
import json

import pytest

from ashare_daily.premarket.announcements import collect_company_reviews
from ashare_daily.premarket.announcement_review import apply_local_decision, company_progress, decision_filename
from ashare_daily.premarket.contracts import SHANGHAI

START='2026-11-04T08:55:00+08:00'
END='2026-11-07T08:55:00+08:00'
SEEN='2026-11-07T08:30:00+08:00'


class Session:
    def __init__(self, folder, count=2, maximum=6):
        self.folder=folder;folder.mkdir(parents=True,exist_ok=True)
        self.candidates=[{'security_id':str(i),'symbol':'sh.'+str(603706+i),'name':'测试公司'+str(i)} for i in range(count)]
        self.calls=[]
        self.budget=type('Budget',(),{'used':0,'maximum':maximum})()
    def remaining(self):return 100
    def observe(self,*args):return {'first_seen_at':SEEN,'previously_observed':False}
    def get(self,url,sid,role,**kwargs):
        if self.budget.used>=self.budget.maximum:raise RuntimeError('budget')
        self.budget.used+=1;self.calls.append((role,url))
        if role=='mapping':
            value={'stockList':[{'code':r['symbol'][3:],'zwjc':r['name'],'category':'A股','orgId':'org'+r['security_id']} for r in self.candidates]}
        elif role=='directory':
            code=kwargs['data']['stock'].split(',')[0];r=next(r for r in self.candidates if r['symbol'][3:]==code)
            value={'totalRecordNum':2,'hasMore':False,'announcements':[{'secCode':code,'secName':r['name'],'orgId':'org'+r['security_id'],
                'announcementId':code+str(i),'announcementTitle':'公告'+str(i),'adjunctType':'PDF','adjunctSize':1,
                'adjunctUrl':'finalpage/2026-11-05/'+code+str(i)+'.pdf',
                'announcementTime':int(datetime(2026,11,5,tzinfo=SHANGHAI).timestamp()*1000)} for i in range(2)]}
        else:
            r=next(r for r in self.candidates if r['symbol'][3:] in url)
            value='%PDF-证券代码：'+r['symbol'][3:]+' 证券简称：'+r['name']+'\n本次会议采用网络互动方式召开，公司将及时回答投资者提问。'
        raw=value.encode() if isinstance(value,str) else json.dumps(value,ensure_ascii=False).encode()
        path=self.folder/(str(self.budget.used)+'.raw');path.write_bytes(raw)
        return raw,{'raw_sha256':hashlib.sha256(raw).hexdigest(),'raw_locator':str(path),
                    'requested_at':SEEN,'fetched_at':SEEN}


def run(session,folder,monkeypatch,**kwargs):
    from pathlib import Path
    from ashare_daily.premarket import announcements
    monkeypatch.setattr(announcements,'extract_pdf',lambda path,**kw:{'text':Path(path).read_bytes().decode()[5:],'pages':1})
    folder.mkdir(exist_ok=True)
    return collect_company_reviews(session,session.candidates,start=START,cutoff=END,directory=folder,**kwargs)


def decision(row,candidate,text):
    return {'schema_version':'company-semantic-decision-v1','source_id':'cninfo_company_announcements',
        'security_id':candidate['security_id'],'symbol':candidate['symbol'],'content_hash':row['content_hash'],
        'text_hash':hashlib.sha256(text.encode()).hexdigest(),'original_url':row['original_url'],
        'reviewed_at':'2026-11-07T08:40:00+08:00','reviewer_kind':'human','decision':'no_adverse_identified',
        'rationale':'已核对完整正文，为会议安排，所查文本没有识别到公司不利事项。',
        'quotes':['本次会议采用网络互动方式召开，公司将及时回答投资者提问。']}


def test_directories_precede_pdfs_and_budget_is_fair(tmp_path,monkeypatch):
    session=Session(tmp_path/'raw')
    result=run(session,tmp_path/'result',monkeypatch)
    assert [role for role,_ in session.calls[:3]]==['mapping','directory','directory']
    assert session.budget.used==6
    assert result['reviews']['0']['progress']['body_count']==2
    assert result['reviews']['1']['progress']['body_count']==1
    assert result['reviews']['0']['semantic_status']=='pending'
    assert result['reviews']['0']['collection_status']=='complete'
    assert result['reviews']['0']['status']=='partial'


def test_cached_bodies_and_bound_decisions_complete_actual_review(tmp_path,monkeypatch):
    from pathlib import Path
    first=Session(tmp_path/'raw',count=1,maximum=4)
    cached=tmp_path/'cache';notes=tmp_path/'notes';notes.mkdir()
    result=run(first,tmp_path/'one',monkeypatch,cache_directory=cached)
    for row in result['reviews']['0']['announcements']:
        text=Path(row['text_locator']).read_text(encoding='utf-8')
        (notes/decision_filename(row,first.candidates[0])).write_text(json.dumps(decision(row,first.candidates[0],text),ensure_ascii=False),encoding='utf-8')
    second=Session(tmp_path/'raw2',count=1,maximum=2)
    result=run(second,tmp_path/'two',monkeypatch,cache_directory=cached,decision_directory=notes)
    review=result['reviews']['0']
    assert second.budget.used==2 and all(role!='pdf' for role,_ in second.calls)
    assert review['status']=='ready' and review['risk_status']=='clear'
    assert review['covered_until_at']==SEEN and review['progress']['semantic_reviewed_count']==2
    assert all(r['cache_reused'] and r['first_seen_at']==SEEN for r in review['announcements'])


@pytest.mark.parametrize('fault',['identity','hash','quote','time','risk'])
def test_review_decisions_cannot_clear_wrong_or_unresolved_evidence(tmp_path,monkeypatch,fault):
    from pathlib import Path
    session=Session(tmp_path/'raw',count=1,maximum=4)
    value=run(session,tmp_path/'result',monkeypatch)['reviews']['0']['announcements'][0]
    text=Path(value['text_locator']).read_text(encoding='utf-8')
    note=decision(value,session.candidates[0],text)
    if fault=='identity':note['security_id']='other'
    if fault=='hash':note['text_hash']='a'*64
    if fault=='quote':note['quotes']=['这个文本根本没有出现过的语句。']
    if fault=='time':note['reviewed_at']='2026-11-07T09:00:00+08:00'
    if fault=='risk':note['decision']='review_required'
    notes=tmp_path/'notes';notes.mkdir()
    (notes/decision_filename(value,session.candidates[0])).write_text(json.dumps(note,ensure_ascii=False),encoding='utf-8')
    if fault in {'identity','hash','quote'}:
        with pytest.raises(ValueError):apply_local_decision(value,session.candidates[0],text,notes,cutoff=END)
    else:
        result=apply_local_decision(value,session.candidates[0],text,notes,cutoff=END)
        assert result['risk_status']=='review_required'
        assert result['semantic_status']==('pending' if fault=='time' else 'reviewed')


def test_completed_processing_does_not_mean_risk_clearance():
    review={'collection_status':'complete','semantic_status':'complete','financial_status':'available',
        'status':'ready','risk_status':'review_required','evidence_ids':['e'],'progress':{}}
    progress=company_progress(review)
    assert progress['processing_complete'] is True and progress['window_review_complete'] is False


@pytest.mark.parametrize('fault',['body','metadata','expired','directory_version'])
def test_untrusted_or_stale_cache_never_supplies_a_body(tmp_path,monkeypatch,fault):
    from ashare_daily.premarket.announcement_cache import load_body
    from ashare_daily.sector_selection import digest
    session=Session(tmp_path/'raw',count=1,maximum=4);cache=tmp_path/'cache'
    review=run(session,tmp_path/'run',monkeypatch,cache_directory=cache)['reviews']['0']
    item=review['announcements'][0]
    if fault=='body':
        for path in cache.glob('*/' + item['content_hash'] + '/body.txt'):path.write_text('corrupt',encoding='utf-8')
    if fault=='metadata':
        for path in cache.glob('*/' + item['content_hash'] + '/metadata.json'):
            value=json.loads(path.read_text());value['extra']='forged';value['metadata_hash']=digest({k:v for k,v in value.items() if k!='metadata_hash'})
            path.write_text(json.dumps(value),encoding='utf-8')
    if fault=='directory_version':item=deepcopy(item);item['directory_version']='changed'
    cutoff='2026-11-23T08:55:00+08:00' if fault=='expired' else END
    assert load_body(cache,session.candidates[0],item,cutoff=cutoff) is None


def test_only_covered_clear_company_advances_its_own_window():
    from ashare_daily.premarket.company_windows import company_query_windows
    candidates=[{'security_id':'a','symbol':'sh.600000','name':'A'},{'security_id':'b','symbol':'sh.600001','name':'B'}]
    prior={'report_id':'old','published_at':'2026-11-06T09:00:00+08:00','watch_candidates':[]}
    for c in candidates:
        prior['watch_candidates'].append({**c,'company_review':{'status':'ready','risk_status':'clear' if c['security_id']=='a' else 'review_required',
            'evidence_ids':['old-evidence'],'query_start_at':'2026-10-24T08:55:00+08:00','covered_until_at':'2026-11-06T08:34:00+08:00',
            'directory_evidence':{'requested_at':'2026-11-06T08:34:00+08:00'}}})
    result=company_query_windows([{'report':prior}],candidates,start=START,cutoff=END)
    assert set(result)=={'a'} and result['a']['start_at']=='2026-11-06T08:34:00+08:00'


def test_mof_verified_treasury_source_is_allowed_but_unknown_publisher_is_not():
    from ashare_daily.premarket.official_alternatives import parse_alternative_article
    item={'title':'主站财政新闻','date':'2026-10-14','url':'https://www.mof.gov.cn/example'}
    # Existing fixture documents the precise parser containers.
    body='<h2 class="title_con">主站财政新闻</h2><div class="laiyuan">发布日期：2026年10月14日</div><div class="my_doccontent"><p>2026年10月14日 来源：国库司</p><p>本次交流涉及公共采购制度和国际合作，具体适用范围、效力和实施条件需要独立核查。</p></div>'
    assert parse_alternative_article(body.encode(),'mof',item)['original_publisher']=='国库司'
    with pytest.raises(ValueError):parse_alternative_article(body.replace('国库司','国库司转载消息').encode(),'mof',item)
