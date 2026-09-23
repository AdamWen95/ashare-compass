import json

from ashare_daily.cli import main


def test_provider_cli_normalizes_date_and_preserves_permission_gate_exit(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("ashare_daily.operations.daily.PROJECT", tmp_path)
    captured = {}

    def check(**kwargs):
        captured.update(kwargs)
        return {"status": "unavailable", "reason": "permission_required", "network_requests": 0}, 2

    monkeypatch.setattr("ashare_daily.provider_diagnostics.provider_check", check)
    code = main(["market", "provider-check", "--provider", "eastmoney", "--date", "2026-09-11", "--online"])
    assert code == 2 and captured["target_date"] == "2026-09-11"
    assert captured["online"] is True and captured["config_path"] == "config/sse_szse_market_providers.json"
    assert json.loads(capsys.readouterr().out)["network_requests"] == 0
