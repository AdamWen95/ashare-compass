"""Standard F2 results around the existing, unchanged BaoStock SDK client.

The original response is retained verbatim. This layer does not retry, write
storage, infer trading days, or turn an empty successful query into coverage.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ashare_daily.calendar import _verified_rows
from ashare_daily.market_foundation import normalize_baostock_rows
from .base import DailyBarRequest, MarketDataProvider, ProviderResult
from .baostock import BaoStockClient, SHANGHAI, validate_request
from .baostock_f2 import BaoStockF2Client


SDK_ENDPOINT = "baostock://public-api.baostock.com:10030/"


class BaoStockProvider(MarketDataProvider):
    name = "baostock"
    capabilities = frozenset({"daily_bars", "trading_calendar"})

    def __init__(self, client: Any, *, mode: str = "research", owns_client: bool = False):
        if mode not in {"research", "offline_test"}:
            raise ValueError("provider mode must be research or offline_test")
        if mode == "research" and not isinstance(client, BaoStockF2Client):
            raise ValueError("research requires a BaoStockF2Client; injected fakes require offline_test")
        if not callable(getattr(client, "query", None)) or type(owns_client) is not bool:
            raise ValueError("query client and explicit boolean ownership required")
        self.client, self.mode, self.owns_client = client, mode, owns_client

    def _result(self, operation: str, response: dict, *, source_symbol: str | None,
                endpoint: str) -> ProviderResult:
        if not isinstance(response, dict):
            raise ValueError("BaoStock client response must be a dictionary")
        attempts = response.get("attempts", [])
        count = len(attempts) if isinstance(attempts, list) else 0
        return ProviderResult(
            provider=self.name, operation=operation, status=response.get("status", "unknown"),
            response=response, source_symbol=source_symbol, source_endpoint=SDK_ENDPOINT + endpoint,
            source_business_date=None, fetched_at=response.get("fetched_at"),
            metrics={"requests": count, "retries": max(0, count - 1),
                     "elapsed_seconds": response.get("elapsed_seconds", 0.0),
                     "mode": self.mode,
                     "verification_kind": "live_network" if self.mode == "research" else "offline_test"},
        )

    def _provenance_matches(self, response: dict) -> bool:
        accepted = {"online", "live_network"} if self.mode == "research" else {"offline_test"}
        return all(key not in response or (isinstance(response[key], str) and response[key] in accepted)
                   for key in ("provenance_mode", "verification_kind"))

    def fetch_daily_bars(self, request: DailyBarRequest) -> ProviderResult:
        if not isinstance(request, DailyBarRequest):
            raise ValueError("DailyBarRequest required")
        parameters = request.parameters()
        if request.adjustment_mode not in {"unadjusted", "forward_adjusted"}:
            # No SDK login or fabricated SDK response is created for a capability
            # which the legacy client does not offer.
            response = {"ok": False, "status": "unsupported", "error_code": "unsupported_adjustment",
                        "error_msg": "BaoStock wrapper supports unadjusted/forward_adjusted only",
                        "operation": "history_f2", "parameters": parameters, "attempts": []}
            return self._result("daily_bars", response, source_symbol=request.identity.symbol,
                                endpoint="query_history_k_data_plus")
        parameters = validate_request("history_f2", parameters)
        response = self.client.query("history_f2", **parameters)
        result = self._result("daily_bars", response, source_symbol=request.identity.symbol,
                              endpoint="query_history_k_data_plus")
        if not self._provenance_matches(response):
            result.status, result.quality = "schema_changed", {"quote_complete": False, "validation_error": "response_provenance_mode_mismatch"}
            return result
        if response.get("ok") is not True:
            return result
        if not BaoStockClient._valid_worker_result(response, "history_f2", parameters):
            result.status, result.quality = "schema_changed", {"quote_complete": False, "validation_error": "source_response_protocol_invalid"}
            return result
        fetched = datetime.fromisoformat(response["fetched_at"])
        if fetched > datetime.now(SHANGHAI) or request.end_date > fetched.date().isoformat():
            result.status, result.quality = "schema_changed", {"quote_complete": False, "validation_error": "source_market_date_in_future"}
            return result
        result.quality = normalize_baostock_rows(
            response["rows"], security_id=request.identity.security_id, symbol=request.identity.symbol,
            start_date=request.start_date, end_date=request.end_date,
            trading_dates=request.expected_dates, adjustment_mode=request.adjustment_mode,
        )
        result.records = result.quality["records"]
        result.status = "success" if result.quality["quote_complete"] else "partial"
        return result

    def fetch_trading_calendar(self, *, start_date: str, end_date: str) -> ProviderResult:
        parameters = validate_request("calendar", {"start_date": start_date, "end_date": end_date})
        response = self.client.query("calendar", **parameters)
        result = self._result("trading_calendar", response, source_symbol=None, endpoint="query_trade_dates")
        result.quality = {"calendar_verified": False, "calendar": {}}
        if not self._provenance_matches(response):
            result.status = "schema_changed"
            result.quality["validation_error"] = "response_provenance_mode_mismatch"
            return result
        if response.get("ok") is not True:
            return result
        if not BaoStockClient._valid_worker_result(response, "calendar", parameters):
            result.status = "schema_changed"
            result.quality["validation_error"] = "source_response_protocol_invalid"
            return result
        try:
            calendar = _verified_rows(response)
        except (ValueError, KeyError, TypeError) as exc:
            result.status = "partial"
            result.quality["validation_error"] = str(exc)
        else:
            result.status = "success"
            result.quality = {"calendar_verified": True,
                              "calendar": {day.isoformat(): opened for day, opened in calendar.items()}}
        return result

    def close(self) -> None:
        if self.owns_client:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
