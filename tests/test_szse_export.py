"""Synthetic official-export contracts; never live market acceptance data."""
from copy import deepcopy
from datetime import date
import hashlib
from io import BytesIO
import json
from pathlib import Path
from urllib.parse import urlencode
import zipfile

import openpyxl
import pytest

from ashare_daily.providers.exchange_universe import discover_exchange_lists, SZSE_ENDPOINT
from test_f11_exchange_universe import approved, szse_body

DAY=date(2026,9,11)
EXPORT='https://www.szse.cn/api/report/ShowReport'
HEADERS=['板块','A股代码','A股简称','A股上市日期','所属行业']


def source_rows(count=41):
    return [{'bk':'主板' if i%2==0 else '创业板','agdm':f'{800000+i:06d}',
             'agjc':f'OFFLINE-{i}','agssrq':'2020-01-02','sshymc':'C 制造业'} for i in range(count)]


def workbook(rows, *, bad_dimensions=True):
    book=openpyxl.Workbook();sheet=book.active;sheet.title='A股列表';sheet.append(HEADERS)
    for row in rows:sheet.append([row[k] for k in ['bk','agdm','agjc','agssrq','sshymc']])
    stream=BytesIO();book.save(stream);book.close()
    if not bad_dimensions:return stream.getvalue()
    result=BytesIO()
    with zipfile.ZipFile(stream) as source,zipfile.ZipFile(result,'w',zipfile.ZIP_DEFLATED) as target:
        for entry in source.infolist():
            body=source.read(entry.filename)
            if entry.filename=='xl/worksheets/sheet1.xml':
                import re
                body=re.sub(rb'<dimension ref="[^"]+"',b'<dimension ref="A1:A1"',body)
            target.writestr(entry.filename,body)
    return result.getvalue()


def transport(tmp_path, *, rows=None, fault=None):
    from ashare_daily.providers.exchange_industry import SECTION_LABELS
    original=source_rows() if rows is None else rows
    exported=deepcopy(original)
    if fault=='duplicate':exported[-1]=deepcopy(exported[0])
    if fault=='missing':exported.pop()
    if fault=='sample':exported[0]['agjc']='DIFFERENT'
    if fault=='formula':exported[0]['agjc']='=1+1'
    if fault=='board':exported[-1]['bk']='UNKNOWN'
    raw_xlsx=workbook(exported)
    if fault in {'shared_string','workbook_type'}:
        changed=BytesIO()
        with zipfile.ZipFile(BytesIO(raw_xlsx)) as source,zipfile.ZipFile(changed,'w',zipfile.ZIP_DEFLATED) as target:
            for entry in source.infolist():
                body=source.read(entry.filename)
                if fault=='shared_string' and entry.filename=='xl/worksheets/sheet1.xml':
                    import re
                    body=re.sub(rb'<c r="C2".*?</c>',b'<c r="C2" t="s"><v>99999</v></c>',body)
                if fault=='workbook_type' and entry.filename=='[Content_Types].xml':
                    body=body.replace(b'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml',b'application/unrecognized+xml')
                target.writestr(entry.filename,body)
        raw_xlsx=changed.getvalue()
    calls=[]
    def fetch(endpoint,params,*,label,target):
        calls.append((endpoint,deepcopy(params)))
        raw_dir=tmp_path/'archive/exchange_lists/requests';raw_dir.mkdir(parents=True,exist_ok=True)
        base=raw_dir/f'{len(calls):03d}'
        r={'ok':True,'status':'ok','http_status':200,'provenance_mode':'offline_test','verification_kind':'offline_test',
           'fetched_at':f'2026-09-11T18:00:0{len(calls)}+08:00','target_date':str(target),'attempts':1,
           'url':endpoint+'?'+urlencode(params),'params':deepcopy(params)}
        if fault=='denied' and len(calls)==1:r.update(ok=False,status='access_denied',http_status=403)
        if endpoint==EXPORT:
            raw=raw_xlsx;r['body_format']='xlsx'
        else:
            total=len(original)
            body=szse_body(deepcopy(original[:20]),total=total,pages=(total+19)//20)
            meta=body[0]['metadata'];meta['cols']['sshymc']='所属行业'
            meta['conditions']=[{'name':'selectHylb','options':[{'value':k,'text':k+' '+v[1]} for k,v in SECTION_LABELS.items()]}]
            if fault=='boundary' and len(calls)==3:meta['recordcount']+=1
            if fault=='date':meta['subname']='2026-09-10'
            raw=json.dumps(body,ensure_ascii=False).encode();r['body']=body
        base.with_suffix('.raw').write_bytes(raw)
        r.update(raw_path=str(base.with_suffix('.raw')),raw_sha256=hashlib.sha256(raw).hexdigest(),response_path=str(base.with_suffix('.json')))
        base.with_suffix('.json').write_text(json.dumps(r,ensure_ascii=False),encoding='utf-8')
        return r
    return fetch,calls


def discover(tmp_path,fetch):
    return discover_exchange_lists(target=DAY,directory=tmp_path/'archive/exchange_lists',permissions=approved('SZSE'),
        mode='offline_test',transport=fetch,scope='sse_szse_a',szse_list_format='xlsx_export')


def test_complete_export_uses_three_requests_and_actual_cells(tmp_path):
    fetch,calls=transport(tmp_path)
    pages,manifests,requests=discover(tmp_path,fetch)
    source=next(m for m in manifests if m['provider']=='szse')
    assert source['complete'] and source['expected_records']==41
    assert len(pages)==1 and len(pages[0]['records'])==41 and pages[0]['terminal']
    assert len(calls)==len(requests)==3
    assert [c[0] for c in calls]==[SZSE_ENDPOINT,EXPORT,SZSE_ENDPOINT]
    assert source['source_transport']=='official_xlsx_export'
    assert source['source_expected_pages']==3 and source['expected_pages']==1
    assert pages[0]['records'][-1]['code']=='800040'


def test_new_collection_does_not_reuse_export_cache_key(tmp_path):
    first,calls1=transport(tmp_path/'first');second,calls2=transport(tmp_path/'second')
    discover(tmp_path/'first',first);discover(tmp_path/'second',second)
    assert calls1[1][1]['random']!=calls2[1][1]['random']


@pytest.mark.parametrize('fault',['duplicate','missing','sample','formula','board','boundary','date','shared_string','workbook_type'])
def test_bad_export_or_unstable_metadata_stays_incomplete(tmp_path,fault):
    fetch,calls=transport(tmp_path,fault=fault)
    pages,manifests,requests=discover(tmp_path,fetch)
    source=next(m for m in manifests if m['provider']=='szse')
    assert not source['complete'] and source['errors'] and not pages
    assert len(calls)<=3
    assert (tmp_path/'archive/exchange_lists/exchange-manifests.json').is_file()
    assert len(json.loads((tmp_path/'archive/exchange_lists/exchange-requests.json').read_text()))==len(calls)


def test_permission_denial_does_not_attempt_export_or_retry(tmp_path):
    fetch,calls=transport(tmp_path,fault='denied')
    pages,manifests,requests=discover(tmp_path,fetch)
    assert len(calls)==len(requests)==1 and not pages
    assert not next(m for m in manifests if m['provider']=='szse')['complete']


def test_malformed_zip_becomes_explicit_contract_failure():
    from ashare_daily.providers.szse_export import parse_workbook
    with pytest.raises(ValueError):
        parse_workbook(b'PK-not-an-excel-file')


def test_real_transport_preserves_binary_response_without_fake_json(tmp_path,monkeypatch):
    from ashare_daily.providers.exchange_universe import WebsiteTransport
    raw=workbook(source_rows(2))
    class Response(BytesIO):
        status=200
        headers={'Content-Type':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}
    instance=WebsiteTransport(tmp_path,max_attempts=1)
    monkeypatch.setattr(instance.opener,'open',lambda *a,**k:Response(raw))
    response=instance(EXPORT,{'SHOWTYPE':'xlsx','CATALOGID':'1110','TABKEY':'tab1'},label='export',target=DAY)
    assert response['ok'] and response['body_format']=='xlsx' and 'body' not in response
    assert Path(response['raw_path']).read_bytes()==raw
    assert response['raw_sha256']==hashlib.sha256(raw).hexdigest()


def test_export_evidence_replays_into_existing_industry_pipeline(tmp_path):
    from test_exchange_industry import fixture,freeze
    from ashare_daily.providers.exchange_industry import exchange_industry_snapshot
    universe=fixture(tmp_path)
    szse=[member['raw'] for member in universe['members'] if member['exchange']=='SZSE']
    for row in szse:row.update(agjc='OFFLINE',agssrq='2020-01-02')
    fetch,calls=transport(tmp_path,rows=szse)
    pages,manifests,_=discover(tmp_path,fetch)
    source=next(m for m in manifests if m['provider']=='szse')
    universe['page_manifest']=[p for p in universe['page_manifest'] if p['provider']!='szse']
    universe['page_manifest'] += [{**{k:v for k,v in p.items() if k!='records'},'record_count':len(p['records'])} for p in pages]
    universe['source_manifests']=[m for m in universe['source_manifests'] if m['provider']!='szse']+[source]
    for member in universe['members']:
        if member['exchange']=='SZSE':member['raw']=next(r['raw'] for p in pages for r in p['records'] if r['code']==member['code'])
    universe['observed_at']='2026-09-11T18:00:05+08:00';freeze(tmp_path,universe)
    result=exchange_industry_snapshot(tmp_path,universe)
    assert result['catalog']['complete'],result['catalog']['issues']
    assert result['catalog']['raw_discovered_members']==5
    export_path=next(Path(r['path']) for r in result['catalog']['file_refs'] if Path(r['path']).suffix=='.raw' and Path(r['path']).read_bytes().startswith(b'PK'))
    export_path.write_bytes(b'changed')
    broken=exchange_industry_snapshot(tmp_path,universe)
    assert not broken['catalog']['complete']
