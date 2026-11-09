"""Read-only view of experimental classifications, separate from the watchlist."""
from .presentation_details import text, paragraphs, table, list_block
from .shadow_scoring import RISK_LABELS, DIMENSION_LABELS


def _rows(report):
    # Pool-only alternatives have not received the five-stock company review.
    # Keep them in audit JSON; the user-facing list contains only official watches.
    shown = set(report['shadow_analysis']['actual_watch_ids'])
    return [r for r in report['shadow_analysis']['records'] if r['security_id'] in shown]


def shadow_panel(report):
    analysis = report['shadow_analysis']
    body = '<h2 id="pmr-shadow-title">观察形态与分类评分 · 试运行</h2>' + paragraphs([analysis['notice']], 'pmr-muted')
    rows = _rows(report)
    if not rows:
        body += '<p class="pmr-empty">本期没有可展示的分类结果；资料不足不会用默认分补齐。</p>'
    body += paragraphs(analysis['issues'], 'pmr-muted')
    for row in rows:
        label = row['name'] or row['symbol']
        score = '未形成评分' if row['score'] is None else str(row['score']) + ' 分（仅同类比较）'
        state = '已观察到价格形态，其他核查仍需确认' if row['confirmation'] == 'price_pattern_observed' else '等待条件确认'
        body += '<details class="pmr-nested"><summary>' + text(label + ' · ' + row['label'] + ' · ' + score) + '</summary>'
        body += paragraphs([state, '公司资料已在所查范围核查' if row['company_status'] == 'reviewed' else '公司资料仍待核查'])
        body += table(['维度', '得分'], [[DIMENSION_LABELS[k], v] for k, v in row['dimensions'].items()])
        body += list_block('待确认条件', row['conditions'])
        body += list_block('风险', [RISK_LABELS[k] for k in row['risk_flags']])
        body += list_block('数据缺口', row['issues'] + row['source_issues']) + '</details>'
    body += paragraphs([analysis['pool_audit'].get('notice', '仅分析已取得证据的原观察池'),
        '效果对照尚未成熟；现有正式名单和旧评分保持原规则。'], 'pmr-muted')
    return '<section class="pmr-section" aria-labelledby="pmr-shadow-title">' + body + '</section>'


def shadow_sections(report):
    analysis = report['shadow_analysis']
    contents = [('paragraph', analysis['notice'])]
    contents.extend(('paragraph', issue) for issue in analysis['issues'])
    for row in _rows(report):
        contents.append(('subheading', (row['name'] or row['symbol']) + '：' + row['label']))
        contents.append(('paragraph', '分类分：' + ('未形成评分' if row['score'] is None else str(row['score']) + '，仅同类比较')))
        contents.append(('table', (['维度', '得分'], [[DIMENSION_LABELS[k], v] for k, v in row['dimensions'].items()])))
        contents.extend(('paragraph', v) for v in row['conditions'] + [RISK_LABELS[k] for k in row['risk_flags']]
                        + row['issues'] + row['source_issues'])
    contents.append(('paragraph', '效果尚未验证，未替换正式排序；池外对象没有新增历史核查。'))
    return [('观察形态与分类评分（试运行）', contents)]
