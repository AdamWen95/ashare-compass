"""Append-only local PDF/text versions, never a fabricated fresh download."""
from datetime import timedelta
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from ..sector_selection import digest
from .contracts import aware


def _key(candidate, item):
    return digest({k: v for k, v in {'security_id': candidate['security_id'], 'symbol': candidate['symbol'],
        'url': item['original_url'], 'directory_version': item.get('directory_version')}.items()})


def _safe(path):
    if any(p.is_symlink() or p.is_junction() for p in (path, *path.parents)):
        raise ValueError('announcement_cache_link_rejected')


def load_body(directory, candidate, item, *, cutoff):
    if directory is None:
        return None
    root = Path(directory).absolute()/_key(candidate, item); _safe(root)
    choices = []
    for path in sorted(root.glob('*/metadata.json'))[:20]:
        _safe(path)
        try:
            if path.stat().st_size > 32768:
                continue
            meta = json.loads(path.read_text(encoding='utf-8'))
            expected={'schema_version','key','content_hash','text_hash','first_seen_at','fetched_at','pages','metadata_hash'}
            if set(meta)!=expected or meta.get('schema_version') != 'announcement-body-cache-v1' or meta.get('metadata_hash') != digest({k:v for k,v in meta.items() if k!='metadata_hash'}):
                continue
            if meta.get('key') != _key(candidate, item):
                continue
            fetched, end = aware(meta['fetched_at']), aware(cutoff)
            if not aware(meta['first_seen_at']) <= fetched <= end or end-fetched > timedelta(days=14):
                continue
            raw, text = path.parent/'body.pdf', path.parent/'body.txt'
            _safe(raw); _safe(text)
            if raw.stat().st_size > 5_000_000 or text.stat().st_size > 1_000_000:
                continue
            if hashlib.sha256(raw.read_bytes()).hexdigest()!=meta['content_hash'] or hashlib.sha256(text.read_bytes()).hexdigest()!=meta['text_hash']:
                continue
            choices.append((fetched, meta, path.parent, text.read_text(encoding='utf-8')))
        except (ValueError, KeyError, TypeError, OSError):
            continue  # Not a substitute for data: caller must fetch or retain a gap.
    if not choices:
        return None
    _, meta, path, text = max(choices, key=lambda r:(r[0],r[1]['content_hash']))
    return {**meta, 'raw_locator':str(path/'body.pdf'), 'text_locator':str(path/'body.txt'),
        'cache_reused':True, 'body_text':text,
        'cache_notice':'复用已取得的同一目录元数据对应正文版本；保留原观察时间，非本期重新下载'}


def store_body(directory, candidate, item, raw, text, receipt, observation, pages):
    if directory is None:
        return
    key = _key(candidate,item)
    if hashlib.sha256(raw).hexdigest()!=receipt['raw_sha256']:
        raise ValueError('announcement_cache_source_hash_mismatch')
    root=Path(directory).absolute()/key/receipt['raw_sha256'];_safe(root)
    meta={'schema_version':'announcement-body-cache-v1','key':key,'content_hash':receipt['raw_sha256'],
        'text_hash':hashlib.sha256(text.encode()).hexdigest(),'first_seen_at':observation['first_seen_at'],
        'fetched_at':receipt['fetched_at'],'pages':pages}
    meta['metadata_hash']=digest(meta)
    # Preserve damaged/older artifacts. A newly verified fetch can repair the
    # usable cache by appending a new copy, without overwriting prior evidence.
    if (root/'metadata.json').exists():
        root=root.with_name(root.name+'-'+uuid4().hex[:12]);_safe(root)
    root.mkdir(parents=True,exist_ok=True)
    for name, body in [('body.pdf',raw),('body.txt',text.encode())]:
        path=root/name
        if path.exists():
            if path.read_bytes()!=body:raise ValueError('announcement_cache_partial_conflict')
        else:
            with path.open('xb') as stream:stream.write(body)
    with (root/'metadata.json').open('x',encoding='utf-8') as stream:
        json.dump(meta,stream,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)
