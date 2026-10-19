"""Inspect rendered morning HTML in Streamlit's actual HTML elements."""
from html.parser import HTMLParser


class MorningContent(HTMLParser):
    def __init__(self):
        super().__init__();self.parts=[];self.headings=[];self.heading=None;self.stock_cards=0

    def handle_starttag(self,tag,attrs):
        if tag in {'h1','h2','h3','h4'}:self.heading=[]
        if tag=='details' and 'pmr-stock' in dict(attrs).get('class','').split():self.stock_cards+=1

    def handle_endtag(self,tag):
        if tag in {'h1','h2','h3','h4'} and self.heading is not None:
            self.headings.append(''.join(self.heading));self.heading=None

    def handle_data(self,value):
        self.parts.append(value)
        if self.heading is not None:self.heading.append(value)


def morning_content(app):
    result=MorningContent()
    for node in app.get('html'):
        if 'id="premarket-reader"' in node.proto.body:result.feed(node.proto.body)
    return result
