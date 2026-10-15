"""Private bounded HTTP worker; stdin carries only registered public requests."""
import base64
import json
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def main():
    spec = json.loads(sys.stdin.buffer.read(20_000))
    url, limit = spec["url"], spec["limit"]
    host = urlsplit(url).hostname
    data = spec["data"]
    req = Request(url, data=urlencode(data).encode() if data is not None else None,
        headers={"User-Agent": spec["user_agent"], "Accept": "application/json,text/html,application/pdf,text/plain",
                 "Referer": "https://www.cninfo.com.cn/" if host.endswith("cninfo.com.cn") else f"https://{host}/"})
    try:
        response = build_opener(NoRedirect()).open(req, timeout=spec["timeout"])
    except HTTPError as exc:
        response = exc
    with response:
        body = response.read(limit + 1)
        return {"status": response.status, "body": base64.b64encode(body).decode("ascii"),
                "content_type": response.headers.get("Content-Type", "")}


if __name__ == "__main__":
    try:
        result = main()
    except Exception as exc:
        result = {"error_type": type(exc).__name__}
    sys.stdout.buffer.write(json.dumps(result).encode("utf-8"))
