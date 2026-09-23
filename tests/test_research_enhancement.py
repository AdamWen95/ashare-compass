"""Synthetic, explicitly offline checks of daily evidence and eligibility boundaries."""
from copy import deepcopy
from datetime import date, datetime
import json
from pathlib import Path

import pytest

from ashare_daily.eligibility import load_evidence_bundle, resolve_eligibility
from ashare_daily.qualification_sources import collect_eligibility, parse_sse_list, parse_szse_page, SSE_URL, SZSE_URL
from ashare_daily.research.associations import link_subjects
from ashare_daily.research.discovery import discover_links
from ashare_daily.research.sources import HttpResponse
from ashare_daily.research.preparation import ResearchConfig, prepare_model_input
from ashare_daily.reports.reader import reader_content
from test_m3_workflow import material

DAY = date(2026, 9, 11)
NOW = datetime.fromisoformat('2026-09-11T11:00:00+08:00')


def sse(rows=None):
    rows = rows or []
    return {'sqlId': 'PL_SSGSXX_FXJSBGPLB', 'actionErrors': [], 'fieldErrors': {},
            'isPagination': 'false', 'result': rows, 'pageHelp': {'total': len(rows)}}


def szse(rows=None, total=0, page=1, size=20):
    return [{'metadata': {'catalogid': 'fxjsb', 'name': '退市整理股票', 'tabkey': 'tab2',
              'subname': '2026-09-11', 'cols': {'zqdm': '证券代码', 'gsjc': '证券简称'},
              'recordcount': total, 'pagecount': (total + size - 1) // size, 'pageno': page, 'pagesize': size},
             'data': rows or [], 'error': None}]


def encode(value):
    return json.dumps(value, ensure_ascii=False).encode()


def collect(tmp_path, transport, target=DAY):
    return collect_eligibility(config_path=Path('config/eligibility_sources.json'), target=target,
        output_dir=tmp_path / 'offline', transport=transport, now=NOW, sleep=lambda _: None)


def normal(url):
    if url.endswith('/robots.txt'):
        return HttpResponse(404, b'')
    return HttpResponse(200, encode(sse() if url == SSE_URL else szse()), 'application/json')


def test_both_confirmed_empty_lists_resolve_only_same_date(tmp_path):
    result = collect(tmp_path, normal)
    assert result['status'] == 'ok' and result['verification_kind'] == 'offline_test'
    bundle = load_evidence_bundle(result['bundle_file'])
    assert len(bundle['records']) == 2
    symbols = ['sh.600001', 'sz.000001']
    assert all(v['status'] == 'false' for v in resolve_eligibility(bundle, symbols, DAY).values())
    assert all(v['status'] == 'unknown' for v in resolve_eligibility(bundle, symbols, date(2026, 9, 10)).values())
    assert all(Path(p['raw_locator']).exists() for r in bundle['records'] for p in r['pages'])


def test_historical_collection_never_fetches_current_list(tmp_path):
    def forbidden(url):
        raise AssertionError('network must not be called')
    result = collect(tmp_path, forbidden, date(2026, 9, 10))
    assert all(h['status'] == 'date_not_supported' for h in result['source_health'])


@pytest.mark.parametrize('status', [401, 403, 429, 500])
def test_failed_response_never_implies_not_delisting(tmp_path, status):
    calls = []
    def transport(url):
        calls.append(url)
        return HttpResponse(404, b'') if url.endswith('robots.txt') else HttpResponse(status, b'[]')
    result = collect(tmp_path, transport)
    assert len(calls) == 4
    assert load_evidence_bundle(result['bundle_file'])['records'] == []


def test_robots_block_does_not_fetch_list(tmp_path):
    calls = []
    def transport(url):
        calls.append(url)
        return HttpResponse(200, b'User-agent: *\nDisallow: /')
    result = collect(tmp_path, transport)
    assert len(calls) == 2 and not load_evidence_bundle(result['bundle_file'])['records']


@pytest.mark.parametrize('change', [{'actionErrors': ['failed']}, {'pageHelp': {'total': 1}},
                                  {'isPagination': 'true'}, {'result': None}])
def test_sse_error_or_unconfirmed_empty_rejected(change):
    payload = sse()
    payload.update(change)
    with pytest.raises(ValueError):
        parse_sse_list(encode(payload))


@pytest.mark.parametrize('field,value', [('name', '风险警示股票'), ('recordcount', 1),
                                       ('pagecount', 1), ('subname', '2026-09-10'), ('pageno', 2)])
def test_szse_st_list_stale_or_incomplete_rejected(field, value):
    payload = szse()
    payload[0]['metadata'][field] = value
    with pytest.raises(ValueError):
        parse_szse_page(encode(payload), DAY)


def test_szse_all_pages_and_duplicate_detection(tmp_path):
    def transport(url):
        if url == SZSE_URL:
            return HttpResponse(200, encode(szse([{'zqdm': '000001', 'gsjc': 'OFFLINE_TEST'}], 2, size=1)))
        if 'PAGENO=2' in url:
            return HttpResponse(200, encode(szse([{'zqdm': '000002', 'gsjc': 'OFFLINE_TEST'}], 2, page=2, size=1)))
        return normal(url)
    result = collect(tmp_path, transport)
    bundle = load_evidence_bundle(result['bundle_file'])
    record = next(r for r in bundle['records'] if r['market'] == 'SZ')
    assert record['total_pages'] == 2 and record['expected_total_records'] == 2
    assert resolve_eligibility(bundle, ['sz.000001'], DAY)['sz.000001']['status'] == 'true'


def test_discovery_restricts_host_path_and_no_tracking_queries():
    settings = {'article_path_prefixes': ['/xwfb/']}
    body = ('<a href="/xwfb/art/2026/art_abcd.html">news</a>'
            '<a href="https://evil.invalid/xwfb/art/2026/art_abcd.html">bad</a>'
            '<a href="/xwfb/art/2026/art_abce.html?key=x">query</a>'
            '<a href="/other/art/2026/art_abcf.html">outside</a>').encode()
    result = discover_links(body, 'https://example.org/xwfb/index.html', settings)
    assert [p['url'] for p in result['articles']] == ['https://example.org/xwfb/art/2026/art_abcd.html']


def test_empty_script_shell_is_gap():
    with pytest.raises(ValueError):
        discover_links(b'<script>loadNews()</script>', 'https://example.org/xwfb/index.html', {'article_path_prefixes': ['/xwfb/']})


@pytest.mark.parametrize('text,expected', [
    ('测试银行将增加金融供给，提供跨境金融服务。', 'business_relationship'),
    ('测试银行出席会议。另一公司从事生产制造。', 'explicit_subject'),
    ('测试银行不从事生产制造。', 'explicit_subject'),
    ('测试银行与样本公司交流，样本公司从事生产制造。', 'explicit_subject'),
])
def test_business_link_needs_same_company_activity(material, text, expected):
    _, item = material
    item['content'] = text
    linked = link_subjects(item, {'sh.600001': '测试银行', 'sz.000001': '样本公司'})
    link = next(a for a in linked['security_associations'] if a['symbol'] == 'sh.600001')
    assert link['association_type'] == expected
    assert link['basis_quote'] in text


def test_parent_group_is_not_listed_business(material):
    _, item = material
    item['content'] = '测试银行集团有限公司从事生产制造。'
    assert not link_subjects(item, {'sh.600001': '测试银行'})['security_associations']


def test_later_business_paragraph_is_not_lost_after_first_mention(material):
    _, item = material
    item['content'] = '测试银行出席会议。\n测试银行将增加金融供给。'
    links = link_subjects(item, {'sh.600001': '测试银行'})['security_associations']
    assert [a['association_type'] for a in links] == ['explicit_subject', 'business_relationship']


def test_positive_target_cannot_be_counterevidence(material):
    from ashare_daily.research.evidence import validate_claims
    from test_m3_workflow import output
    _, item = material
    claim = output(item, claim_type='counterevidence')
    result = validate_claims(claim, [item], {}, {}, '2025-04-30T23:59:59+08:00', strict_counterevidence=True)
    assert result['accepted_count'] == 0
    assert 'counterevidence_has_no_explicit_adverse_fact' in result['rejected_claims'][0]['validation_reasons']


def test_new_output_contract_requires_separately_cited_risk(material):
    from ashare_daily.research.contracts import DailyResearchOutput
    from pydantic import ValidationError
    from test_m3_workflow import output
    _, item = material
    with pytest.raises(ValidationError):
        DailyResearchOutput.model_validate(output(item, risks=['uncited assertion']))


@pytest.mark.parametrize('text,expected', [
    ('不能视为今日新增催化。', False), ('并非今日首次。', False),
    ('属于历史背景，不构成今日新增催化。', False),
    ('今日新增催化值得关注。', True), ('不能确认，今日新增催化值得关注。', True),
    ('并非今日新增催化；但是今天首次带来利好。', True),
])
def test_negated_today_claim_is_distinct_from_affirmation(text, expected):
    from ashare_daily.research.evidence import asserts_today_novelty
    assert asserts_today_novelty(text) is expected


def test_pending_objects_do_not_change_formal_candidates():
    row = {'symbol': 'sh.600001', 'name': 'OFFLINE测试', 'technical_screen_status': 'pass',
           'eligibility_status': 'pending', 'relative_return': '0.1'}
    market = {'evaluations': [row], 'candidates': [], 'pending_eligibility': [row]}
    before = deepcopy(market)
    prepared = prepare_model_input([], [], market, ResearchConfig(include_pending_observations=True))
    assert prepared['research_objects'][0]['path'] == 'pending_observation'
    assert market == before and not market['candidates']
    content = reader_content({'market': market, 'analysis': {'accepted_claims': []},
                              'research_objects': prepared['research_objects']})
    assert content['cards'][0]['label'] == '量价观察 · 资格待核查'
    assert '程序量价观察' in content['cards'][0]['risks'][0]
