"""Dated exchange website evidence; no names, ST flags or empty errors imply false.

These are reviewed public website requests, not advertised licensed public APIs.
Only the exact source/adapter pairs below can be enabled in configuration.
"""
from datetime import date, datetime
import hashlib
import json
import math
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser
from urllib.request import Request, build_opener
from uuid import uuid4

from ashare_daily.market_schemas import SHANGHAI
from ashare_daily.research.sources import _NoRedirect, HttpResponse

SSE_URL = 'https://query.sse.com.cn/commonSoaQuery.do?sqlId=PL_SSGSXX_FXJSBGPLB&domesticIndicator=P&productType=0'
SSE_PAGE = 'https://www.sse.com.cn/disclosure/listedinfo/riskplate/'
SZSE_URL = 'https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON&CATALOGID=fxjsb&loading=first'
SZSE_PAGE = 'https://www.szse.cn/disclosure/listed/warn/index.html'
VERSION = 'exchange-eligibility-v1'
USER_AGENT = 'ashare-daily-research/0.5 (bounded eligibility verification)'


def merge_eligibility(automatic: Path, manual: Path, destination: Path) -> Path:
    """Preserve imported assertions; the existing resolver detects contradictions."""
    from ashare_daily.eligibility import load_evidence_bundle
    bundles = [load_evidence_bundle(path) for path in (automatic, manual)]
    if any(b['verification_kind'] == 'offline_test' for b in bundles):
        raise ValueError('真实资格采集不能合并离线测试证据')
    result = {key: bundles[0][key] for key in ('schema_version', 'verification_kind')}
    for key, identifier in [('sources', 'source_id'), ('records', 'evidence_id')]:
        unique = {}
        for bundle in bundles:
            for item in bundle[key]:
                identity = item.get(identifier)
                if not identity or identity in unique and unique[identity] != item:
                    raise ValueError('人工与自动资格包存在同 ID 冲突，不能覆盖任何证据')
                unique[identity] = item
        result[key] = list(unique.values())
    with destination.open('x', encoding='utf-8') as file:
        json.dump(result, file, ensure_ascii=False, indent=2)
    return destination


def parse_sse_list(body: bytes) -> list[str]:
    payload = json.loads(body.decode('utf-8-sig'))
    if (not isinstance(payload, dict) or payload.get('sqlId') != 'PL_SSGSXX_FXJSBGPLB'
            or payload.get('actionErrors') != [] or payload.get('fieldErrors') != {}
            or payload.get('isPagination') != 'false'):
        raise ValueError('上交所名单结构/查询结果异常，不能确认为完整名单')
    rows, page = payload.get('result'), payload.get('pageHelp')
    if not isinstance(rows, list) or not isinstance(page, dict) or type(page.get('total')) is not int or page['total'] != len(rows):
        raise ValueError('名单缺少可信总数或条数不符')
    if len(rows) > 200:
        raise ValueError('名单超出有限核验范围')
    symbols = []
    for row in rows:
        code = row.get('INSTRUMENT_ID')
        if not isinstance(code, str) or not re.fullmatch(r'(?:600|601|603|605)\d{3}', code) or not row.get('INSTRUMENT_SHORT'):
            raise ValueError('名单证券字段改变或超出沪市主板 A 股范围')
        symbols.append('sh.' + code)
    if len(symbols) != len(set(symbols)):
        raise ValueError('名单有重复证券，拒绝认定完整')
    return symbols


def parse_szse_page(body: bytes, target: date, page_number: int = 1) -> dict:
    payload = json.loads(body.decode('utf-8-sig'))
    if not isinstance(payload, list):
        raise ValueError('深交所风险警示板响应结构改变')
    tabs = [t for t in payload if isinstance(t, dict) and t.get('metadata', {}).get('tabkey') == 'tab2']
    if len(tabs) != 1:
        raise ValueError('缺少唯一退市整理股票页签，不能以风险警示股票代替')
    tab = tabs[0]
    meta, rows = tab.get('metadata', {}), tab.get('data')
    if (tab.get('error') is not None or meta.get('catalogid') != 'fxjsb'
            or meta.get('name') != '退市整理股票' or not isinstance(rows, list)
            or meta.get('cols') != {'zqdm': '证券代码', 'gsjc': '证券简称'}):
        raise ValueError('退市整理页签字段或错误状态异常')
    total, pages, size = (meta.get(k) for k in ('recordcount', 'pagecount', 'pagesize'))
    if (any(type(n) is not int for n in (total, pages, size, meta.get('pageno')))
            or total < 0 or not 1 <= size <= 100 or not 0 <= pages <= 5
            or meta['pageno'] != page_number or pages != (total + size - 1) // size
            or len(rows) != min(size, max(0, total - (page_number - 1) * size))):
        raise ValueError('退市整理名单总数、分页或实际条数不符')
    dates = [str(t.get('metadata', {}).get('subname', '')).strip() for t in payload]
    if any(d and d != target.isoformat() for d in dates):
        raise ValueError('官网显示日期不是取得当日，拒绝套用当前资格')
    symbols = []
    for row in rows:
        if not isinstance(row, dict) or not re.fullmatch(r'(?:000|001|002|003|300|301)\d{3}', str(row.get('zqdm', ''))) or not row.get('gsjc'):
            raise ValueError('深市名单证券字段改变')
        symbols.append('sz.' + row['zqdm'])
    if len(set(symbols)) != len(symbols):
        raise ValueError('名单页内证券重复')
    # The full SZ list includes ChiNext. Keeping all members makes the original
    # recordcount auditable; resolving a mainboard symbol is still well-defined.
    return {'symbols': symbols, 'total': total, 'pages': max(1, pages)}


def _get(url, *, timeout_seconds=15):
    request = Request(url, headers={'User-Agent': USER_AGENT,
                                   'Referer': SSE_PAGE if urlsplit(url).hostname == 'query.sse.com.cn' else SZSE_PAGE,
                                   'Accept': 'application/json,text/plain'})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout_seconds) as response:
            return HttpResponse(response.status, response.read(1000001), response.headers.get('Content-Type', ''))
    except HTTPError as exc:
        return HttpResponse(exc.code, exc.read(100000), exc.headers.get('Content-Type', ''))


def collect_eligibility(*, config_path: Path, target: date, output_dir: Path, transport=None, now=None, sleep=time.sleep,
                        max_seconds=None) -> dict:
    if max_seconds is not None and (type(max_seconds) not in {int, float} or not math.isfinite(max_seconds) or not 0 < max_seconds <= 120):
        raise ValueError('资格来源总时限必须大于0且不超过120秒')
    deadline = time.monotonic() + max_seconds if max_seconds is not None else None
    config = json.loads(Path(config_path).read_text(encoding='utf-8-sig'))
    if config.get('schema_version') != VERSION or set(config) != {'schema_version', 'sources'}:
        raise ValueError('资格来源登记结构不符')
    if not isinstance(config['sources'], list) or len(config['sources']) > 2:
        raise ValueError('仅允许已核验的沪深两个市场来源')
    started = now or datetime.now(SHANGHAI)
    if started.tzinfo is None:
        raise ValueError('核验时间必须带时区')
    started = started.astimezone(SHANGHAI)
    kind = 'offline_test' if transport is not None else 'automatic_source'
    directory = Path(output_dir).resolve() / (started.strftime('%Y%m%dT%H%M%S%f') + '-' + uuid4().hex[:8])
    if kind == 'offline_test' and directory.is_relative_to(Path(__file__).resolve().parents[2] / 'data/research'):
        raise ValueError('离线资格测试不能写入真实研究目录')
    directory.mkdir(parents=True, exist_ok=False)
    bundle = {'schema_version': 'eligibility-evidence-v1', 'verification_kind': kind, 'sources': [], 'records': []}
    health = []
    for source in config['sources']:
        sid = source['source_id']
        entry = {'source_id': sid, 'market': source.get('market'), 'status': 'disabled',
                 'target_date': target.isoformat(), 'observed_at': started.isoformat()}
        health.append(entry)
        if source.get('enabled') is not True:
            entry['reason'] = source.get('reason', '来源尚未核实')
            continue
        pairs = {'sse_mainboard_delisting': ('SH', SSE_URL), 'szse_delisting': ('SZ', SZSE_URL)}
        if sid not in pairs or (source.get('market'), source.get('url')) != pairs[sid]:
            raise ValueError('不允许启用未经实现与核验的资格来源')
        if source.get('approved_for_local_use') is not True or not source.get('permission_basis') or not source.get('checked_at'):
            raise ValueError('启用资格来源需要实际许可核对记录')
        if target != started.date():
            entry.update(status='date_not_supported', reason='当前名单只支持取得当日，历史日期继续使用已冻结证据')
            continue
        try:
            url, market = source['url'], source['market']
            entry['requests'] = []
            def fetch(location, label, *, robots=False):
                remaining = deadline - time.monotonic() if deadline is not None else None
                if remaining is not None and remaining <= 0:
                    raise ValueError('资格来源总时间预算用尽；保留未知')
                if entry['requests']:
                    sleep(min(1, remaining) if remaining is not None else 1)
                remaining = deadline - time.monotonic() if deadline is not None else None
                if remaining is not None and remaining <= 0:
                    raise ValueError('资格来源总时间预算用尽；保留未知')
                response = (transport(location) if transport is not None else _get(location) if remaining is None
                            else _get(location, timeout_seconds=min(15, remaining)))
                request = {'url': location, 'http_status': response.status, 'role': label,
                           'fetched_at': (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI).isoformat()}
                entry['requests'].append(request)
                if len(response.body) > 1000000:
                    raise ValueError('名单响应超出大小限制')
                raw = directory / (sid + '-' + label + '.raw')
                raw.write_bytes(response.body)
                request.update(raw_locator=str(raw), raw_sha256=hashlib.sha256(response.body).hexdigest())
                if response.status != 200 and not (robots and response.status == 404):
                    raise ValueError(f'HTTP {response.status}；停止此源，不重试权限或限流错误')
                return response, request
            robots, _ = fetch('https://' + urlsplit(url).hostname + '/robots.txt', 'robots', robots=True)
            robot = RobotFileParser()
            robot.parse(robots.body.decode('utf-8-sig').splitlines() if robots.status == 200 else [])
            if not robot.can_fetch(USER_AGENT, url):
                raise ValueError('robots 禁止访问登记名单，停止此源')
            response, first = fetch(url, 'page1')
            raw_hash = first['raw_sha256']
            entry.update(http_status=response.status, raw_locator=first['raw_locator'], raw_sha256=raw_hash)
            if market == 'SH':
                members = parse_sse_list(response.body)
                total, page_count = len(members), 1
            else:
                parsed = parse_szse_page(response.body, target)
                members, total, page_count = parsed['symbols'], parsed['total'], parsed['pages']
            pages = [{'number': 1, 'source_url': url, 'raw_locator': first['raw_locator'],
                      'raw_sha256': raw_hash, 'status': 'ok', 'symbols': list(members)}]
            for number in range(2, page_count + 1):
                location = SZSE_URL + f'&TABKEY=tab2&PAGENO={number}'
                if not robot.can_fetch(USER_AGENT, location):
                    raise ValueError('robots 禁止访问后续名单页，停止此源')
                response, fetched = fetch(location, f'page{number}')
                parsed = parse_szse_page(response.body, target, number)
                if parsed['total'] != total or parsed['pages'] != page_count:
                    raise ValueError('翻页期间总数改变，名单不完整')
                members.extend(parsed['symbols'])
                pages.append({'number': number, 'source_url': location, 'raw_locator': fetched['raw_locator'],
                              'raw_sha256': fetched['raw_sha256'], 'status': 'ok', 'symbols': parsed['symbols']})
            if len(members) != total or len(set(members)) != total:
                raise ValueError('完整名单总数不符或跨页重复')
            observed_time = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
            if observed_time.date() != target:
                raise ValueError('采集跨越日期，不能冻结为同日资格')
            observed = observed_time.isoformat()
            registration = {'source_id': sid, 'name': ('上交所主板' if market == 'SH' else '深交所') + '退市整理股票当日完整列表',
                            'source_url': url, 'access_method': '已核对官网风险警示板使用的公开 GET；非对外授权 API',
                            'field_meaning': ('domesticIndicator=P 是退市整理股票；productType=0 是主板' if market == 'SH' else 'fxjsb 的 tab2 明确为退市整理股票；全深市名单覆盖主板'),
                            'markets': [market], 'coverage_scope': 'all_mainboard_a_shares',
                            'supports_historical_dates': False, 'supports_complete_lists': True,
                            'approved_for_local_use': True, 'usage_limits': '非商业本地资格核验；不向模型发送名单原文；每次一份完整响应',
                            'reviewed_by': VERSION + ' adapter; source mapping verified during implementation',
                            'reviewed_at': observed, 'review_basis': source['permission_basis']}
            bundle['sources'].append(registration)
            record = {'evidence_id': sid + '-' + raw_hash[:20] + '-' + target.isoformat(), 'source_id': sid,
                      'kind': 'complete_delisting_list', 'market': market, 'source_url': url,
                      'raw_locator': first['raw_locator'], 'raw_sha256': raw_hash, 'content_version': VERSION + ':' + hashlib.sha256(json.dumps(pages, sort_keys=True).encode()).hexdigest(),
                      'first_seen_at': observed, 'fetched_at': observed, 'effective_from': target.isoformat(),
                      'effective_to': target.isoformat(), 'temporal_basis': 'same_date_complete_snapshot',
                      'retrieval_status': 'ok' if members else 'empty_confirmed', 'parse_status': 'ok',
                      'reviewed_by': registration['reviewed_by'], 'reviewed_at': observed,
                      'review_basis': '自动结构核验：查询标识、无错误、完整分页、明确总数、合法证券及无重复均通过；非人工逐股背书',
                      'evidence_excerpt': f'官网退市整理股票列表明确总数={total}；实际{len(members)}条，完整读取{page_count}页；深市列表如含创业板一并保留以核对原始总数。',
                      'assertion_basis': 'complete_list', 'complete_scope': 'all_mainboard_a_shares',
                      'expected_total_records': len(members), 'total_pages': page_count, 'pages': pages}
            bundle['records'].append(record)
            entry.update(status='ok', total_records=len(members), effective_date=target.isoformat())
        except Exception as exc:
            entry.update(status='failed', error_type=type(exc).__name__, reason=str(exc)[:250] if isinstance(exc, ValueError) else '资格来源请求失败，保留未知')
    path = directory / 'eligibility.json'
    path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding='utf-8')
    result = {'status': 'ok' if health and all(h['status'] == 'ok' for h in health) else 'partial',
              'verification_kind': kind, 'source_health': health, 'bundle_file': str(path)}
    (directory / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result
