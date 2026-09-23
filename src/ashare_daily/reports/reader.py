"""Deterministic reading view of frozen data, shared by web/HTML/Markdown."""
from decimal import Decimal, InvalidOperation

PATHS = {'technical_candidate': '正式量价预候选', 'pending_observation': '量价观察 · 资格待核查',
         'event_observation': '事件观察 · 业务关联待验证'}


def number(value, *, percent=False):
    if value is None:
        return '未取得'
    try:
        n = Decimal(str(value))
        if not n.is_finite():
            return '未取得'
        return f'{n * 100:.2f}%' if percent else f'{n:,.2f}'
    except InvalidOperation:
        return '未取得'


def reader_content(report: dict) -> dict:
    market = report['market']
    rows = {r['symbol']: r for r in market.get('evaluations', []) if r.get('symbol')}
    claims = report['analysis'].get('accepted_claims', [])
    catalog = {e['evidence_id']: e for e in report.get('evidence_catalog', []) if e.get('evidence_id')}
    objects = list(report.get('research_objects', []))
    # Old archives can be read as quantitative observations without pretending
    # these companies were submitted to their historical model call.
    if not objects:
        objects = [{'symbol': r['symbol'], 'name': r.get('name'), 'path': path, 'association_evidence_ids': []}
                   for key, path in [('candidates', 'technical_candidate'), ('pending_eligibility', 'pending_observation')]
                   for r in market.get(key, [])][:10]
    cards = []
    for obj in objects[:10]:
        row = rows.get(obj['symbol'], {})
        company_claims = [c for c in claims if c.get('symbol') == obj['symbol']]
        passed = [c.get('label', c.get('id', '')) for c in row.get('technical_conditions', row.get('conditions', []))
                  if c.get('status') == 'pass']
        risks = list(dict.fromkeys(row.get('exclusion_reasons', []) + row.get('data_issues', [])))
        if not company_claims:
            risks.append('尚无通过证据核验的个股模型分析；以下仅为程序量价观察。')
        related_ids = set(obj.get('association_evidence_ids', []))
        related_ids.update(c['evidence_id'] for claim in company_claims for c in claim.get('citations', []))
        business = []
        for eid in sorted(related_ids):
            item = catalog.get(eid, {})
            for assoc in item.get('security_associations', []):
                if assoc['symbol'] == obj['symbol'] and assoc['association_type'] == 'business_relationship':
                    business.append({'evidence_id': eid, 'quote': assoc['basis_quote'], 'title': item.get('title'),
                                     'published_at': item.get('published_at'), 'is_background': item.get('is_background', False),
                                     'original_url': item.get('original_url')})
        if not business:
            risks.append('公司业务与本期消息的联系尚无充分原文依据，不能据此判断受益。')
        risks.append('上市公司公告与研报未完整覆盖，未取得资料不代表没有风险。')
        label = PATHS.get(obj['path'], '研究观察')
        if obj['path'] == 'event_observation' and business:
            label = ('历史业务背景观察' if all(e['is_background'] for e in business) else '消息与业务研究观察')
            if row.get('eligibility_status') == 'pending':
                label += ' · 资格待查'
        cards.append({'symbol': obj['symbol'], 'name': obj.get('name') or row.get('name', ''),
                      'path': obj['path'], 'label': label,
                      'technical_status': row.get('technical_screen_status', obj.get('technical_screen_status')),
                      'eligibility_status': row.get('eligibility_status', obj.get('eligibility_status')),
                      'reasons': passed or ['查看逐项筛选条件及资料关联'],
                      'metrics': [{'label': '收盘价（元，未复权）', 'value': number(row.get('display_close'))},
                                  {'label': '20 日相对基准收益', 'value': number(row.get('relative_return'), percent=True)},
                                  {'label': '20 日平均成交额（元）', 'value': number(row.get('avg_amount_cny'))}],
                      'claims': company_claims, 'business_evidence': business, 'risks': list(dict.fromkeys(risks)),
                      'followup': [c['text'] for c in company_claims if c['claim_type'] in {'followup', 'unknown'}]
                                  or ['核查适用日期的证券资格；补充最新公告和业务证据；观察量价条件能否持续。']})
    directions = [c for c in claims if c.get('symbol') and c.get('claim_type') in {'inference', 'opinion'}]
    counts = market.get('counts', {})
    summary = (f"本期覆盖 {counts.get('stock_count', 0)} 只股票，"
               f"行情完整 {counts.get('market_data_success_count', 0)} 只；"
               f"正式预候选 {counts.get('candidate_count', 0)} 只，"
               f"量价达标但资格待查 {counts.get('pending_eligibility_count', 0)} 只。")
    return {'summary': summary, 'cards': cards, 'directions': directions,
            'market_claims': [c for c in claims if not c.get('symbol')]}


def reader_sections(report: dict) -> list:
    content = reader_content(report)
    overview = [('paragraph', content['summary']),
                ('paragraph', '观察池分别呈现量价条件、业务证据与资格状态；待核查对象不计为正式候选。')]
    directions = [('paragraph', c['text']) for c in content['directions']]
    if not directions:
        directions = [('paragraph', '本期尚无通过证据核验的个股方向推论；可以先阅读量价观察及具体待查事项。')]
    blocks = []
    for card in content['cards']:
        blocks.append(('subheading', f"{card['name']} {card['symbol']} · {card['label']}"))
        blocks.append(('paragraph', '入选观察的依据：' + '；'.join(card['reasons'])))
        blocks.append(('table', (['程序指标', '数值'], [[m['label'], m['value']] for m in card['metrics']])))
        for e in card['business_evidence']:
            blocks.append(('paragraph', ('历史业务背景' if e['is_background'] else '业务依据') + f"（{e['published_at']}）：{e['quote']}；来源 {e['title']}；证据 {e['evidence_id']}"))
        for claim in card['claims']:
            blocks.append(('paragraph', f"{claim['claim_type']}：{claim['text']}"))
            blocks.extend(('paragraph', f"证据 {c['evidence_id']}：{c['quote']}") for c in claim['citations'])
        blocks.extend(('paragraph', '风险与缺口：' + x) for x in card['risks'])
        blocks.extend(('paragraph', '继续观察：' + x) for x in card['followup'])
    if not blocks:
        blocks.append(('paragraph', '本期没有可列入观察池的对象；见筛选核验表中的排除或数据不足原因。'))
    return [('今日研究摘要', overview), ('重点方向与研究逻辑', directions), ('个股观察与研究', blocks)]
