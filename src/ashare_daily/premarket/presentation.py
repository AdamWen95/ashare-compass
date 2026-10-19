"""Read-only presentation of frozen reports; no classification or data writes."""
from decimal import Decimal

from .contracts import OBSERVATION_SCHEMAS, STATE_SCHEMA, aware, decimal
from .coverage import comparison_label
from .report import direction_facts
from .presentation_details import (text,paragraphs,list_block,stock_details,news_details,audit_details,coverage_details)
from .presentation_style import STYLE


def number(value,*,scale=1,digits=2,signed=False,suffix=''):
    try:
        value=decimal(value)*Decimal(str(scale))
        return (('+' if signed and value>0 else '')+f'{value:.{digits}f}'+suffix)
    except (ValueError,TypeError):
        return '未取得'


def tone(value):
    try:
        value=decimal(value)
        return 'pmr-up' if value>0 else 'pmr-down' if value<0 else ''
    except (ValueError,TypeError):
        return ''


def timestamp(value):
    try:
        label=aware(value).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError,TypeError):
        label=value if value is not None else '未取得'
    return f'<time datetime="{text(value)}">{text(label)}</time>'


def heading(title,number_,id_,context=''):
    return f'<header class="pmr-section-heading"><h2 id="{id_}"><span class="pmr-step">{number_}</span>{text(title)}</h2><p>{text(context)}</p></header>'


def metric(label,value,context='',color=''):
    return f'<div class="pmr-metric"><span>{text(label)}</span><strong class="{color}">{text(value)}</strong><small>{text(context)}</small></div>'


def market_section(report):
    phased=report['schema_version']==STATE_SCHEMA
    assessment=report['market_state'] if phased else report['stance']
    title='市场状态' if phased else '市场姿态'
    market=report['market'];index=market.get('benchmark') or {};components=market.get('components',{})
    def usable(key):return components.get(key)=='ready' if components else market.get('status')=='ready'
    breadth=market.get('advancing_fraction') if usable('breadth') else None
    change=index.get('return5') if usable('trend') else None
    try:
        volume=decimal(index.get('amount'),nonnegative=True)/decimal(index.get('previous_mean_amount5'),positive=True) if usable('activity') else None
    except (ValueError,TypeError):
        volume=None
    badge=(assessment.get('evidence_basis',{}).get('status') if phased else assessment.get('evidence_status'))
    badge={'complete':'依据充分','partial':'依据有限','insufficient':'关键依据不足'}.get(badge,'按原冻结规则')
    phase=assessment.get('state',assessment.get('stance','undetermined'))
    color={'trend_strength':'strong','strong_consolidation':'steady','weak_repair':'repair','trend_pressure':'pressure',
        'attack':'strong','defend':'pressure','mixed_range':'mixed','hold':'mixed'}.get(phase,'unknown')
    hero=f'<div class="pmr-hero pmr-state-{color}"><div class="pmr-hero-top"><div><span class="pmr-pill">{text(badge)}</span>'
    hero+=f'<h3 class="pmr-state"><span class="pmr-sr-only">{title}：</span>{text(assessment["label"])}</h3>'+paragraphs(assessment['reasons'],'pmr-reason')+'</div>'
    hero+='<div class="pmr-metrics">'+metric('全市场上涨占比',number(breadth,scale=100,digits=1,suffix='%'),'沪深普通A股 · 行情日')
    hero+=metric('上证指数 · 5日',number(change,scale=100,signed=True,suffix='%'),'连续趋势独立核验',tone(change))
    hero+=metric('指数成交额比率',number(volume,scale=100,digits=1,suffix='%'),'相对此前5日均值')+'</div></div>'
    if phased:
        hero+='<div class="pmr-focus"><b>观察重点</b><span>'+text(assessment['observation_focus'])+'</span></div>'
    if assessment.get('analysis'):
        hero+='<details class="pmr-analysis"><summary>完整判断依据</summary>'+paragraphs(assessment['analysis'])+'</details>'
    hero+='</div>'
    if phased:
        basis=assessment['evidence_basis']
        hero+='<div class="pmr-basis-risk"><section><h3>依据范围</h3>'+paragraphs([basis['label']])+paragraphs(basis['limitations'],'pmr-muted')+'</section>'
        hero+='<section><h3>风险提示</h3>'+''.join('<p class="pmr-risk-line'+(' pmr-high' if r['severity']=='high' else '')+'">'+text(r['text'])+'</p>' for r in assessment['risk_notes'])
        if not assessment['risk_notes']:
            hero+='<p class="pmr-muted">未触发本规则的额外风险标记，不代表没有风险。</p>'
        hero+='</section></div>'
    else:
        hero+=list_block('资料限制',assessment.get('limitations',[]))
    if report['research_status']!='ready':
        hero+='<p class="pmr-coverage-note">资料覆盖尚不完整；以上按已有有效证据分析，缺失项可能改变结论。市场状态与个股核查状态分别判断。</p>'
    return '<section aria-labelledby="pmr-market">'+heading(title,'01','pmr-market','先看整体，再看局部观察')+hero+'</section>'


def directions_section(report):
    observation=report['schema_version'] in OBSERVATION_SCHEMAS
    body=heading('相对表现观察方向' if observation else '重点关注方向','02','pmr-directions',f'{len(report["directions"])}个方向 · 不代表全市场最优')
    if not report['directions']:
        return '<section class="pmr-section">'+body+'<p class="pmr-empty">暂无完成核验的重点方向，保留空结果，不强行补足。</p></section>'
    cards=[]
    for index,direction in enumerate(report['directions'],1):
        facts=direction.get('relative_metrics')
        label=('相对强势' if facts['absolute_up'] else '相对抗跌') if facts else '事件观察' if direction.get('kind')=='event' else '量价方向'
        card=f'<div class="pmr-card-top"><span class="pmr-kicker">方向 {index}</span><span class="pmr-pill">{label}</span></div>'
        card+='<h3>'+text(direction.get('name',direction['sector_id']))+'</h3>'
        if facts:
            scope=comparison_label(facts)
            card+='<div class="pmr-direction-value '+tone(facts['mean_change_pct'])+'">'+text(number(facts['mean_change_pct'],signed=True,suffix='%'))+'</div><p class="pmr-muted">行业单日平均涨跌幅</p>'
            card+='<p class="pmr-comparison">相对'+scope+' <b>'+text(number(facts['excess_market_pp'],signed=True))+'</b> 个百分点</p>'
            card+='<div class="pmr-direction-stats"><div><b>'+text(number(facts['advancing_fraction'],scale=100,digits=1,suffix='%'))+'</b>行业上涨占比</div><div><b>'+text(facts['member_count'])+' 只</b>行业样本</div></div>'
        card+='<details class="pmr-nested"><summary>依据与失效条件</summary>'+paragraphs([direction.get('reason'),direction.get('continuity')])+paragraphs(direction_facts(direction),'pmr-muted')
        if direction.get('invalidation'):
            card+='<p>失效条件：'+text(direction['invalidation'])+'</p>'
        cards.append('<article class="pmr-direction">'+card+'</details></article>')
    return '<section class="pmr-section" aria-labelledby="pmr-directions">'+body+'<div class="pmr-directions">'+''.join(cards)+'</div></section>'


def _visible_risks(row):
    review=row.get('company_review',{})
    risks=list(review.get('risk_flags',[]))+list(review.get('financial_risk_flags',[]))
    risks.extend(r for r in row.get('observation_risks',[]) if any(word in str(r) for word in ('净利润为负','每股收益为负','比率为负')))
    result=[]
    for risk in risks:
        if risk not in result:result.append(risk)
    return result


def stock_card(row):
    facts=row.get('relative_observation') or {}
    verified=row.get('observation_status')=='verified'
    pending=(row.get('observation_status')=='conditional' or row.get('research_status')=='pending'
        or row.get('eligibility_status') in {'pending','unknown'})
    badge='本期已核查' if verified else '资料待核' if pending else '按原报告核查状态'
    badge_style='pmr-ok' if verified else 'pmr-pending' if pending else ''
    try:
        score=format(decimal(row.get('technical_score')),'f')+' 分'
    except (ValueError,TypeError):
        score='未取得'
    head='<div><div class="pmr-stock-name">'+text(row.get('name',''))+f' <span class="pmr-pill {badge_style}">{badge}</span></div>'
    head+='<div class="pmr-stock-symbol">'+text(row.get('symbol',''))+' · '+text(row.get('sector_name',''))+'</div></div>'
    head+=metric('行情日涨跌幅',number(facts.get('stock_change_pct'),signed=True,suffix='%'),color=tone(facts.get('stock_change_pct')))
    head+='<div class="pmr-relative-column">'+metric('相对行业',number(facts.get('excess_sector_pp'),signed=True,suffix=' 个百分点'))+'</div>'
    head+='<div class="pmr-score-column">'+metric('技术评分',score)+'</div><span class="pmr-chevron" aria-hidden="true"></span>'
    for risk in _visible_risks(row):
        head+='<div class="pmr-stock-risk">'+text(risk)+'</div>'
    head+='<div class="pmr-trade-notice">'+text(row.get('trade_status_notice','报告日交易状态仍待核验'))+'</div>'
    details='<p class="pmr-muted">技术评分 '+text(score)+' · 仅用于研究排序，不是上涨概率</p>'
    if row.get('reason')=='company_review_incomplete':details+='<p class="pmr-risk-line">公司公告或风险资料尚未完成核查</p>'
    elif row.get('reason')=='market_not_attack':details+='<p class="pmr-muted">原报告市场姿态不支持新增进攻，保留后续观察</p>'
    details+=stock_details(row)
    return '<details class="pmr-stock"><summary>'+head+'</summary><div class="pmr-stock-content">'+details+'</div></details>'


def watch_section(report):
    observation=report['schema_version'] in OBSERVATION_SCHEMAS
    groups=[('观察名单与风险','watch_candidates')] if observation else [('重点关注股票','focus_candidates'),('待核查观察','pending_candidates')]
    count=sum(len(report[key]) for _,key in groups)
    body=heading('观察名单与风险' if observation else '关注股票与风险','03','pmr-watch',f'{count}只股票 · 展开查看条件与完整风险')
    state_label='市场状态' if report['schema_version']==STATE_SCHEMA else '市场姿态'
    body+='<p class="pmr-muted">观察名单独立于'+state_label+'；名单内只列研究条件，不构成买入指令。</p>'
    body+='<p class="pmr-muted">技术评分仅用于研究排序，不是上涨概率；报告日交易状态仍待核验。</p>'
    for title,key in groups:
        if not observation:body+='<h3>'+text(title)+'</h3>'
        if not report[key]:body+='<p class="pmr-empty">本期观察名单为空，不强行补足；核查范围与缺口见数据覆盖。</p>'
        body+='<div class="pmr-watch-list">'+''.join(stock_card(row) for row in report[key])+'</div>'
    return '<section class="pmr-section" aria-labelledby="pmr-watch">'+body+'</section>'


def render_body(report):
    """Return only presentation; never mutate facts or invoke upstream collectors."""
    meta=''.join(f'<span>{label} <b>{text(report.get(key))}</b></span>' for label,key in
        [('报告日','report_date'),('目标交易日','target_session_date'),('行情日','market_as_of_date')])
    meta+='<span>消息截点 <b>'+timestamp(report.get('news_cutoff_at'))+'</b></span><span>实际发布 <b>'+timestamp(report.get('published_at'))+'</b></span><span>北京时间</span>'
    body='<div id="premarket-reader" class="pmr"><header class="pmr-edition"><div><p class="pmr-kicker">今日方向简报 · 只读研究</p><h1>'+text(report['title'])+'</h1></div></header>'
    body+='<div class="pmr-dates">'+meta+'</div>'+market_section(report)+directions_section(report)+watch_section(report)
    body+='<div class="pmr-support">'
    for id_,label,count,content in [('pmr-news','隔夜重要变化与出处',f'{len(report["overnight_events"])}条记录',news_details(report)),
        ('pmr-audit','完整筛选明细',f'{len(report.get("screening_audit",[]))}只筛选',audit_details(report)),
        ('pmr-coverage','数据覆盖与规则版本','保留来源与限制',coverage_details(report))]:
        body+=f'<details id="{id_}"><summary><span>{label}</span><span class="pmr-muted">{count} · 展开</span></summary><div class="pmr-support-content">{content}</div></details>'
    body+='</div><footer class="pmr-footer">'+text(report['notice'])+'</footer></div>'
    return body


def render_document(report):
    nav='<nav class="report-nav" aria-label="报告章节">'+''.join(f'<a href="#{id_}">{label}</a>' for id_,label in
        [('pmr-market','市场状态'),('pmr-directions','观察方向'),('pmr-watch','观察名单'),('pmr-news','消息出处'),('pmr-audit','筛选明细'),('pmr-coverage','数据覆盖')])+'</nav>'
    return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+text(report['title'])+'</title>'+STYLE+'</head><body class="pmr-document">'+nav+render_body(report)+'</body></html>'
