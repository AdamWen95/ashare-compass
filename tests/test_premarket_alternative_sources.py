"""Synthetic HTML at the reviewed alternative official-source boundaries."""
import pytest


def test_mof_index_keeps_declared_main_site_scope_and_visible_date():
    from ashare_daily.premarket.official_alternatives import parse_mof_index
    body = '''<ul class="xwfb_listbox"><li><a href="./202610/t20261014_123.htm" title="财政新闻">财政新闻</a><span>2026-10-14</span></li><li><a href="https://jrs.mof.gov.cn/202610/test.htm" title="子站资料">子站资料</a><span>2026-10-13</span></li></ul>'''
    rows = parse_mof_index(body.encode())
    assert len(rows) == 2
    assert rows[0]["in_scope"] is True and rows[1]["in_scope"] is False
    assert rows[0]["date"] == "2026-10-14"


def test_mof_article_visible_date_beats_regenerated_meta_time():
    from ashare_daily.premarket.official_alternatives import parse_alternative_article
    html = '''<meta name="PubDate" content="2026-10-15 08:54:00"><h2 class="title_con">财政新闻</h2><div class="laiyuan">发布日期：2026年10月14日</div><div class="my_doccontent"><div class=TRS_Editor><div class="TRS_Editor"><p>2026年10月14日 来源：财政部新闻办公室</p><p>本次政策交流涉及财政政策、国际合作及实施条件，具体内容以原始发布为准。</p></div></div></div>'''
    result = parse_alternative_article(html.encode(), "mof", {"title": "财政新闻", "date": "2026-10-14", "url": "https://www.mof.gov.cn/zhengwuxinxi/caizhengxinwen/202610/t20261014_123.htm"})
    assert result["published_at"] == "2026-10-14" and result["publication_precision"] == "date"
    assert result["time_conflict"] is True and result["model_use_allowed"] is False
    with pytest.raises(ValueError, match="original_publisher"):
        parse_alternative_article(html.replace("财政部新闻办公室", "某转载媒体").encode(), "mof", {"title": "财政新闻", "date": "2026-10-14", "url": "https://www.mof.gov.cn/"})


def test_szse_literal_directory_does_not_execute_script_or_infer_url_date():
    from ashare_daily.premarket.official_alternatives import parse_szse_index
    page = '''<ul class="newslist date-right"><li><script>
//var curTitle = '忽略的注释';
var curHref = './t20261013_123.html';
var curTitle = '规则修订发布';
document.write('此脚本不执行');
</script><span class="time">2026-10-14</span></li></ul>'''
    rows = parse_szse_index(page.encode())
    assert rows[0]["title"] == "规则修订发布" and rows[0]["date"] == "2026-10-14"
    with pytest.raises(ValueError): parse_szse_index(page.replace("'./t20261013_123.html'", "buildUrl()").encode())
    with pytest.raises(ValueError): parse_szse_index(page.replace("'./t20261013_123.html'", "'https://evil.example/test.html'").encode())


def test_szse_article_keeps_private_locator_but_public_homepage_link():
    from ashare_daily.premarket.official_alternatives import parse_alternative_article
    row = {"title": "规则修订发布", "date": "2026-10-14", "url": "https://www.szse.cn/aboutus/trends/news/t20261013_123.html"}
    html = '<h2 class="title">规则修订发布</h2><div class="time">时间：2026-10-14</div><div class="des-content"><p>本次规则涉及市场制度建设，适用范围与实施日期须结合具体文件进一步核对。</p></div>'
    result = parse_alternative_article(html.encode(), "szse", row)
    assert result["original_url"] == row["url"]
    assert result["public_url"] == "https://www.szse.cn/"
    assert result["effective_at"] is None


def test_unreviewed_html_page_is_not_an_empty_window():
    from ashare_daily.premarket.official_alternatives import parse_mof_index, parse_szse_index
    for parse in (parse_mof_index, parse_szse_index):
        with pytest.raises(ValueError): parse(b"<html><script>dynamic()</script></html>")


def test_szse_unrendered_template_is_not_a_published_row():
    from ashare_daily.premarket.official_alternatives import parse_szse_index
    page = '''<ul class="newslist date-right"><li><script>
var curHref = './t20261013_123.html';
var curTitle = '已发布规则';
</script><span class="time">2026-10-13</span></li></ul>
<script type="text/html"><ul class="newslist date-right"><li><%= item.title %></li></ul></script>'''
    assert len(parse_szse_index(page.encode())) == 1
