"""Finite public source requests, archived before parsing; no retries or redirects."""
from datetime import datetime
import base64
import hashlib
import json
from pathlib import Path
import time
import subprocess
import sys
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from ..operations.daily import atomic_json
from ..research.sources import HttpResponse, USER_AGENT
from .contracts import SHANGHAI
from .observations import record_observation

HOSTS = {"www.pbc.gov.cn", "www.csrc.gov.cn", "www.cninfo.com.cn", "static.cninfo.com.cn", "www.mof.gov.cn", "www.szse.cn"}
HTTP_WORKER = Path(__file__).with_name("source_http_worker.py")


class RequestBudget:
    """One reservation per HTTP attempt, including robots and failed attempts."""
    def __init__(self, maximum, path):
        if type(maximum) is not int or not 1 <= maximum <= 20:
            raise ValueError("source_budget_invalid")
        self.maximum, self.path, self.reservations = maximum, path, []

    @property
    def used(self):
        return len(self.reservations)

    def reserve(self, source_id, role, url):
        if self.used >= self.maximum:
            raise RuntimeError("source_request_budget_exhausted")
        self.reservations.append({"source_id": source_id, "role": role, "url": url,
            "reserved_at": datetime.now(SHANGHAI).isoformat()})
        atomic_json(self.path, self.reservations)

    def get(self, url, timeout, limit):
        return _request(url, None, timeout, limit)


def _request(url, data, timeout, limit):
    try:
        result = subprocess.run([sys.executable, str(HTTP_WORKER)], input=json.dumps({"url": url, "data": data,
            "timeout": timeout, "limit": limit, "user_agent": USER_AGENT}).encode(),
            capture_output=True, timeout=timeout, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("source_request_absolute_deadline") from exc
    if result.returncode or len(result.stdout) > (limit + 1) * 2 + 10_000:
        raise RuntimeError("source_worker_failed")
    value = json.loads(result.stdout)
    if value.get("error_type"):
        raise OSError("source_request_failed")
    return HttpResponse(value["status"], base64.b64decode(value["body"], validate=True), value["content_type"])


class SourceSession:
    def __init__(self, directory, *, max_requests, max_seconds, transport=None, sleep=time.sleep, observation_database=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        if (self.directory / "requests.json").exists() or (self.directory / "budget.json").exists():
            raise ValueError("source_session_directory_already_used")
        if not 0 < max_seconds <= 14400:
            raise ValueError("source_deadline_invalid")
        self.deadline = time.monotonic() + max_seconds
        self.budget = RequestBudget(max_requests, self.directory / "budget.json")
        self.transport, self.sleep = transport or _request, sleep
        self.mode = "offline_test" if transport else "automatic"
        self.requests, self.robots, self.stopped_hosts = [], {}, set()
        self.observation_database = Path(observation_database) if observation_database else self.directory / "observations.sqlite3"

    def observe(self, source_id, url, content_hash, observed_at):
        return record_observation(self.observation_database, source_id, url, content_hash, observed_at)

    @property
    def request_count(self):
        return self.budget.used

    def remaining(self):
        return max(0, self.deadline - time.monotonic())

    def _read(self, url, source_id, role, data=None):
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname not in HOSTS or parts.port not in (None, 443)
                or parts.username or parts.password or parts.fragment or ".." in parts.path or "%" in parts.path):
            raise ValueError("source_url_not_registered")
        if parts.hostname in self.stopped_hosts:
            raise PermissionError("source_stopped")
        robot = self.robots.get(parts.hostname)
        interval = max(1, (robot.crawl_delay(USER_AGENT) or robot.crawl_delay("*") or 0) if robot else 0)
        rate = robot.request_rate(USER_AGENT) if robot else None
        if rate:
            interval = max(interval, rate.seconds / rate.requests)
        if self.remaining() <= interval:
            raise TimeoutError("source_deadline")
        if self.request_count:
            self.sleep(interval)
        timeout = min(15, self.remaining())
        if timeout <= 0:
            raise TimeoutError("source_deadline")
        self.budget.reserve(source_id, role, url)
        row = {"source_id": source_id, "role": role, "url": url, "request_index": self.request_count,
            "requested_at": datetime.now(SHANGHAI).isoformat(), "status": "pending", "acquisition_mode": self.mode}
        if data is not None:
            row["form"] = data
        self.requests.append(row)
        atomic_json(self.directory / "requests.json", self.requests)
        limit = 5_000_000 if role == "pdf" else 2_000_000
        try:
            response = self.transport(url, data, timeout, limit)
            row.update(fetched_at=datetime.now(SHANGHAI).isoformat(), http_status=response.status,
                content_type=response.content_type, byte_count=len(response.body))
            if len(response.body) > limit:
                raise ValueError("source_response_too_large")
            raw = self.directory / f"{self.request_count:03d}-{role}.raw"
            raw.write_bytes(response.body)
            row.update(raw_locator=str(raw.resolve()), raw_sha256=hashlib.sha256(response.body).hexdigest())
            if response.status in (401, 403, 429) or 300 <= response.status < 400:
                self.stopped_hosts.add(parts.hostname)
                raise PermissionError("source_access_denied")
            if response.status != 200 and not (role == "robots" and response.status == 404):
                raise ValueError("source_http_error")
            if role == "pdf" and not response.body.startswith(b"%PDF-"):
                self.stopped_hosts.add(parts.hostname)
                raise PermissionError("pdf_response_not_pdf")
            row["status"] = "ok"
            return response.body, row
        except (OSError, ValueError, RuntimeError) as exc:
            row.update(status="failed", error_type=type(exc).__name__)
            raise
        finally:
            atomic_json(self.directory / "requests.json", self.requests)

    def get(self, url, source_id, role, *, data=None):
        host = urlsplit(url).hostname
        if host in self.stopped_hosts:
            raise PermissionError("source_stopped")
        if host not in self.robots:
            body, receipt = self._read(f"https://{host}/robots.txt", source_id, "robots")
            parser = None
            if receipt["http_status"] == 200:
                text = body.decode("utf-8-sig", errors="strict")
                if "<html" in text.lower() or "<script" in text.lower():
                    self.stopped_hosts.add(host)
                    raise PermissionError("robots_response_not_rules")
                parser = RobotFileParser()
                parser.parse(text.splitlines())
            self.robots[host] = parser
        robot = self.robots[host]
        if robot and not robot.can_fetch(USER_AGENT, url):
            self.stopped_hosts.add(host)
            raise PermissionError("robots_disallowed")
        return self._read(url, source_id, role, data)
