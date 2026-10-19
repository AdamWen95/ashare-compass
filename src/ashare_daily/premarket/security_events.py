"""Reviewed public listing events; no automatic external source or historical rewrite."""
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from .contracts import aware


def validate_security_event(event):
    url=urlsplit(event.get('source_url',''))
    if (event.get('verification')!='reviewed_primary_publication'
            or event.get('kind')!='delisting_after_merger' or event.get('exchange') not in {'SSE','SZSE'}
            or not re.fullmatch(r'\d{6}',event.get('code','')) or not event.get('security_id')
            or not event.get('event_id') or not event.get('name')
            or url.scheme!='https' or url.hostname not in {'www.sse.com.cn','www.szse.cn','static.cninfo.com.cn'}
            or url.username or url.password):
        raise ValueError('premarket_security_event_unverified')
    published=date.fromisoformat(event['published_date'])
    effective=date.fromisoformat(event['effective_date'])
    observed,verified=aware(event['first_observed_at']),aware(event['verified_at'])
    if published>effective or observed.date()<published or verified<observed:
        raise ValueError('premarket_security_event_dates_invalid')


def load_security_events(project, report_date, cutoff):
    path=Path(project)/'config/premarket_security_events.json'
    if not path.exists():
        return []
    packet=json.loads(path.read_text(encoding='utf-8'))
    if packet.get('schema_version')!='premarket-security-events-v1' or not isinstance(packet.get('events'),list):
        raise ValueError('premarket_security_events_schema_invalid')
    events,seen=[],set()
    for event in packet['events']:
        validate_security_event(event)
        key=(event['exchange'],event['code'])
        if key in seen:
            raise ValueError('premarket_security_event_duplicate')
        seen.add(key)
        if event['effective_date']<=report_date and aware(event['verified_at'])<=aware(cutoff):
            events.append(deepcopy(event))
    return events


def excluded_event_ids(events,members):
    excluded=set()
    by_symbol={(m['exchange'],m['code']):m for m in members.values()}
    for event in events:
        validate_security_event(event)
        member=by_symbol.get((event['exchange'],event['code']))
        if member and member['security_id']!=event['security_id']:
            raise ValueError('premarket_security_event_identity_conflict')
        excluded.add(event['security_id'])
    return excluded
