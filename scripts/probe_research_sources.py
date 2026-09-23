"""Small, separately recorded read-only source verification; never reads secrets."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ALLOWED = {'www.sse.com.cn', 'query.sse.com.cn', 'www.szse.cn', 'res.szse.cn', 'www.mofcom.gov.cn'}


def main():
    target = Path('outputs/verification/enhancement/source-probes')
    target.mkdir(parents=True, exist_ok=True)
    for url in sys.argv[1:]:
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname not in ALLOWED or parsed.username or parsed.password:
            raise ValueError('Unregistered verification host')
        now = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
        row = {'url': url, 'observed_at': now, 'mode': 'live_source_verification'}
        identity = hashlib.sha256((url + now).encode()).hexdigest()[:20]
        try:
            headers = {'User-Agent': 'ashare-daily-research/0.5 (source verification)'}
            if parsed.hostname == 'query.sse.com.cn':
                headers['Referer'] = 'https://www.sse.com.cn/disclosure/listedinfo/riskplate/'
            request = Request(url, headers=headers)
            with urlopen(request, timeout=18) as response:
                content = response.read(2000001)
                if len(content) > 2000000:
                    raise ValueError('Response exceeds small verification budget')
                if urlsplit(response.url).hostname != parsed.hostname:
                    raise ValueError('Unexpected redirect host')
                path = target / (identity + '.raw')
                path.write_bytes(content)
                row.update(status='ok', http_status=response.status, bytes=len(content),
                           raw_path=str(path), sha256=hashlib.sha256(content).hexdigest())
        except Exception as exc:
            row.update(status='failed', error_type=type(exc).__name__, http_status=getattr(exc, 'code', None))
        (target / (identity + '.json')).write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(row, ensure_ascii=False))


if __name__ == '__main__':
    main()
