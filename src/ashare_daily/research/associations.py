"""Versioned literal subject/business links; never infer a beneficiary from a name."""
import re
from .evidence import build_evidence
from .contracts import Evidence

BUSINESS = re.compile(r'主营|从事|生产|制造|研发|经营|提供.{0,16}服务|金融供给|发放贷款|销售|供应链|出口|进口')
NEGATED = re.compile(r'不(?:再|涉及|从事|生产|经营)|尚未|未开展|没有.{0,8}业务')
GROUP_ONLY = re.compile(r'集团(?:公司|有限公司|有限责任公司)|控股股东|母公司')


def link_subjects(item: dict, identities: dict[str, str]) -> dict:
    """Exact frozen security name/code in a paragraph is a mention, not a benefit.

    Ambiguous short names and parent-group-only passages are excluded. A business
    link requires an activity grammatically following the name in the same
    sentence, without another known company subject; it carries no effect,
    revenue or sentiment assertion. Existing imported links retain their contract.
    """
    values = {key: item[key] for key in Evidence.model_fields if key in item}
    links = list(values.get('security_associations', []))
    seen = {(a['symbol'], a['basis_quote']) for a in links}
    paragraphs = [p.strip() for p in values['content'].splitlines() if p.strip()]
    if not paragraphs:
        return item
    for symbol, name in sorted(identities.items()):
        if not isinstance(name, str) or name in {'stock', 'index'} or len(name) < 4:
            continue
        code = symbol.split('.')[-1]
        for paragraph in paragraphs:
            # Names with punctuation/links are not accepted as identity aliases.
            if name not in paragraph or len(paragraph) > 1200:
                continue
            if GROUP_ONLY.search(paragraph) and code not in paragraph:
                continue
            if (symbol, paragraph) in seen:
                continue
            business_sentence = None
            for sentence in re.findall(r'[^。！？\n]+[。！？]?', paragraph):
                if name not in sentence or GROUP_ONLY.search(sentence):
                    continue
                after_name = sentence.split(name, 1)[1]
                other_subject = any(other != name and len(other) >= 4 and other in sentence for other in identities.values())
                # A separate clause may introduce an unnamed other company.
                if other_subject or re.search(r'另一|其他公司|该公司|对方|合作方', after_name):
                    continue
                if BUSINESS.search(after_name[:100]) and not NEGATED.search(sentence):
                    business_sentence = sentence.strip()
                    break
            if not business_sentence and any(a['symbol'] == symbol for a in links):
                continue
            quote = business_sentence or paragraph
            links.append({'symbol': symbol, 'name': name, 'basis_quote': quote,
                          'association_type': 'business_relationship' if business_sentence else 'explicit_subject'})
            seen.add((symbol, paragraph))
            if business_sentence:
                break
    if links == values.get('security_associations', []):
        return item
    values['security_associations'] = links
    for key in ('evidence_id', 'content_hash', 'content_version'):
        values.pop(key, None)
    return build_evidence(**values)
