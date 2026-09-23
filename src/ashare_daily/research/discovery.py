"""Discover bounded article links from reviewed public HTML index pages."""
from html.parser import HTMLParser
import re
from urllib.parse import urljoin, urlsplit


class IndexLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.current = None
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {'script', 'style', 'noscript'}:
            self.skip += 1
        if tag == 'a' and not self.skip:
            self.current = {'href': attrs.get('href', ''), 'title': attrs.get('title', ''), 'text': ''}

    def handle_data(self, text):
        if self.current and not self.skip:
            self.current['text'] += text

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'noscript'}:
            self.skip = max(0, self.skip - 1)
        if tag == 'a' and self.current:
            self.links.append(self.current)
            self.current = None


def discover_links(body: bytes, index_url: str, settings: dict) -> dict:
    text = body.decode('utf-8-sig', errors='strict')
    if any(marker in text[:1500].lower() for marker in ('captcha', 'access denied', '访问过于频繁')):
        raise PermissionError('资讯目录出现访问限制')
    parser = IndexLinks()
    parser.feed(text)
    host = urlsplit(index_url).hostname
    articles, next_pages, seen = [], [], set()
    for item in parser.links:
        href = urljoin(index_url, item['href'])
        parsed = urlsplit(href)
        if (parsed.scheme != 'https' or parsed.hostname != host or parsed.port not in (None, 443)
                or parsed.username or parsed.password or parsed.query or parsed.fragment or href in seen):
            continue
        if any(parsed.path.startswith(prefix) for prefix in settings['article_path_prefixes']) and re.search(r'/art/\d{4}/art_[a-f0-9]+\.html$', parsed.path):
            seen.add(href)
            articles.append({'url': href, 'content_type': 'fulltext', 'discovered_from': index_url,
                             'discovery_title': item['title'] or item['text'].strip()})
        elif ('下一页' in item['text'] or item['text'].strip().lower() == 'next') and parsed.path.startswith(urlsplit(index_url).path.rsplit('/', 1)[0] + '/'):
            next_pages.append(href)
    if not articles:
        raise ValueError('目录未解析到已登记范围的文章链接；不将脚本空壳视为当天零消息')
    return {'articles': articles, 'next_pages': list(dict.fromkeys(next_pages)),
            'coverage': '已读取目录可见链接；目录或条数上限之外未覆盖'}


def validate_discovery(settings: dict, host: str) -> None:
    if set(settings) - {'index_urls', 'article_path_prefixes', 'max_index_pages', 'max_articles'}:
        raise ValueError('资讯发现配置含未知字段')
    indexes, prefixes = settings.get('index_urls'), settings.get('article_path_prefixes')
    if not isinstance(indexes, list) or not 1 <= len(indexes) <= 4:
        raise ValueError('每日发现需要一至四个已核验目录')
    for value in indexes:
        p = urlsplit(value)
        if p.scheme != 'https' or p.hostname != host or p.username or p.password or p.query or p.fragment or p.port not in (None, 443):
            raise ValueError('目录必须是同一登记站点的 HTTPS 原始页面')
    if not isinstance(prefixes, list) or not prefixes or any(not isinstance(p, str) or not p.startswith('/') or '..' in p or p == '/' for p in prefixes):
        raise ValueError('须限定文章路径，不能允许整站任意 URL')
    for key, default, high in [('max_index_pages', 2, 4), ('max_articles', 6, 12)]:
        value = settings.setdefault(key, default)
        if type(value) is not int or not 1 <= value <= high:
            raise ValueError('目录和文章数量必须处于有限采集范围')
