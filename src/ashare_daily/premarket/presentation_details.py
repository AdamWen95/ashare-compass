"""Escaped disclosure content shared by the morning reader and HTML export."""
from html import escape
import json
from urllib.parse import urlsplit

from .coverage import coverage_notice
from .report import observation_facts


def text(value):
    return escape(str(value if value is not None else '未取得'),quote=True)


def paragraphs(values,cls=''):
    return ''.join(f'<p class="{cls}">{text(v)}</p>' for v in values if v is not None)


def list_block(title,values):
    if not values:
        return ''
    return f'<div class="pmr-detail-group"><h4>{text(title)}</h4><ul>'+''.join(
        f'<li>{text(v)}</li>' for v in values)+'</ul></div>'


def table(headers,rows):
    return '<div class="pmr-table-wrap"><table><thead><tr>'+''.join(f'<th scope="col">{text(v)}</th>' for v in headers)+(
        '</tr></thead><tbody>'+''.join('<tr>'+''.join(f'<td>{text(v)}</td>' for v in row)+'</tr>' for row in rows)
        +'</tbody></table></div>')


def json_details(label,value):
    return f'<details class="pmr-nested"><summary>{text(label)}</summary><pre>'+text(
        json.dumps(value,ensure_ascii=False,indent=2))+'</pre></details>'


def source_link(url,label,*,company=False):
    try:
        parts=urlsplit(url)
        if (parts.scheme!='https' or not parts.hostname or parts.username or parts.password
                or company and parts.hostname!='static.cninfo.com.cn'):
            return ''
    except (ValueError,TypeError):
        return ''
    return f'<a href="{text(url)}" target="_blank" rel="noopener noreferrer">{text(label)}</a>'


def stock_details(row):
    review=row.get('company_review',{})
    values=[paragraphs(row.get('reasons',[])),paragraphs(observation_facts(row),'pmr-muted'),
        '<div class="pmr-condition-grid">'+list_block('观察条件',row.get('watch_conditions',[]))+
        list_block('失效条件',row.get('invalidation_conditions',[]))+'</div>',
        list_block('观察风险',row.get('observation_risks',[])),
        list_block('公司资料待查',review.get('gaps',[])),list_block('公告风险定位',review.get('risk_flags',[])),
        list_block('财务风险',review.get('financial_risk_flags',[])),list_block('财务待查',review.get('financial_gaps',[]))]
    components=row.get('score_components',[])
    if components:
        values.append('<h4>技术评分构成</h4>'+table(['条件','分值','依据'],[
            [c.get('label'),c.get('points'),c.get('reason')] for c in components]))
    for announcement in review.get('announcements',[]):
        values.append('<div class="pmr-source"><h4>'+text(announcement.get('title'))+'</h4><p>'+text(announcement.get('published_at'))+
            (' · 正文已取得，语义待核验' if announcement.get('body_status')=='extracted' else ' · 正文未取得')+'</p>'+
            source_link(announcement.get('original_url',''),'查看公司公告',company=True)+'</div>')
    if review.get('financial_facts'):
        values.append(json_details('财务原始事实与来源（保留接口口径）',{
            'facts':review['financial_facts'],'provenance':review.get('financial_provenance',{})}))
    return ''.join(values)


def news_details(report):
    events=report.get('overnight_events',[])
    if not events:
        return '<p class="pmr-empty">暂无已核验的新事件，不代表没有新闻或风险。</p>'
    cards=[]
    for event in events:
        notice=('来源发布记录，影响及生效条件尚待核验' if event.get('claim_type')=='publication_notice' else
                '原文支持的摘录，仍需核对语义' if event.get('validation_status')=='verified' else '研究推论，尚待核验')
        body=paragraphs([event.get('text')])
        if event.get('is_background'):
            body+='<p class="pmr-muted">此前已观察，本期作为背景保留</p>'
        body+=f'<p class="pmr-muted">{text(notice)}</p><details class="pmr-nested"><summary>核对消息出处</summary>'
        body+=''.join('<blockquote>'+text(c.get('quote'))+'</blockquote>' for c in event.get('citations',[]))
        for source in event.get('sources',[]):
            url=source.get('public_url',source.get('original_url',''))
            body+=source_link(url,'查看原文')+paragraphs([source.get('published_at','时间未知'),source.get('locator_notice')],'pmr-muted')
        cards.append('<article class="pmr-news-item">'+body+'</details></article>')
    return ''.join(cards)


def audit_details(report):
    labels={'pass':'通过','fail':'未通过','pending':'待核查','unknown':'数据不足'}
    return table(['证券','名称','量价条件','基本资格','待查事项','指标'],[
        [r.get('symbol'),r.get('name'),labels.get(r.get('technical_status'),'数据不足'),
         labels.get(r.get('eligibility_status'),'待核查'),'；'.join(r.get('eligibility_gaps',[])),
         json.dumps(r.get('metrics',{}),ensure_ascii=False)] for r in report.get('screening_audit',[])])


def coverage_details(report):
    body=''
    coverage=report.get('research_coverage')
    if coverage:
        body+=paragraphs([coverage_notice(coverage)])
        body+=json_details('证券覆盖与退出事件原始记录',coverage)
    body+=json_details('消息覆盖与来源记录',report['coverage'])+json_details('规则版本',report['rule_versions'])
    body+=json_details('本期原始时间口径',{key:report.get(key) for key in
        ('report_date','target_session_date','market_as_of_date','news_cutoff_at','generated_at','published_at')})
    return body+'<p class="pmr-muted">规则为研究启发式；技术验证不代表已证明策略效果。</p>'
