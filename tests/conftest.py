"""M0 tests always run with outbound socket access blocked."""

from datetime import date, datetime
import socket
from zoneinfo import ZoneInfo

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("M0 must not attempt network access")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


@pytest.fixture
def scenario_date():
    return date(2026, 9, 9)


@pytest.fixture
def generated_at():
    return datetime(2026, 9, 10, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def demo_report(scenario_date, generated_at):
    from ashare_daily.demo import build_demo_report

    return build_demo_report(scenario_date, generated_at=generated_at)
