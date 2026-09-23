"""Full-list SDK pagination regression tests; all records are synthetic."""

import pytest
from baostock.common.contants import BAOSTOCK_PER_PAGE_COUNT

from ashare_daily.providers.baostock import BASIC_FIELDS, BaoStockClient, validate_request
from ashare_daily.providers.baostock_worker import execute_request


class PagedResult:
    error_code, error_msg = "0", "success"
    fields = BASIC_FIELDS
    per_page_count = str(BAOSTOCK_PER_PAGE_COUNT)

    def __init__(self, count, *, truncated=False, fail_code=False, skip_page=False):
        rows = [[f"sh.{i:06d}", f"synthetic-{i}", "2000-01-01", "", "1", "1"] for i in range(count)]
        self.pages = [rows[i:i + BAOSTOCK_PER_PAGE_COUNT] for i in range(0, count, BAOSTOCK_PER_PAGE_COUNT)]
        self.pages += [[]] if count % BAOSTOCK_PER_PAGE_COUNT == 0 else []
        self.data, self.cur_page_num, self.cur_row_num = self.pages[0], "1", 0
        self.truncated, self.fail_code, self.skip_page = truncated, fail_code, skip_page

    def next(self):
        if self.cur_row_num < len(self.data):
            return True
        if len(self.data) < BAOSTOCK_PER_PAGE_COUNT:
            return False
        if self.truncated:
            return False  # reproduces real SDK's silent lost-page failure
        if self.fail_code:
            self.error_code = "10002007"
            return False
        self.cur_page_num = str(int(self.cur_page_num) + (2 if self.skip_page else 1))
        self.data = self.pages[int(self.cur_page_num) - 1]
        self.cur_row_num = 0
        return bool(self.data)

    def get_row_data(self):
        row = self.data[self.cur_row_num]
        self.cur_row_num += 1
        return row


class SDK:
    def __init__(self, result):
        self.result, self.calls = result, []

    def login(self):
        return PagedResult(0)

    logout = login

    def query_stock_basic(self, **parameters):
        self.calls.append(parameters)
        return self.result


@pytest.mark.parametrize("count", [0, 101, 501, 2000, 2001, 10000])
def test_discovery_consumes_all_pages_without_fixed_sample_cap(count):
    sdk = SDK(PagedResult(count))
    result = execute_request({"operation": "basic_all", "parameters": {}}, sdk=sdk)
    assert result["ok"] is True
    assert len(result["rows"]) == count
    assert result["pagination"]["observed_records"] == count
    assert result["pagination"]["exhausted"] is True
    assert BaoStockClient._valid_worker_result(result, "basic_all", {})
    assert sdk.calls == [{"code": ""}]


@pytest.mark.parametrize("options", [{"truncated": True}, {"fail_code": True}, {"skip_page": True}])
def test_page_loss_or_failure_never_returns_partial_success(options):
    result = execute_request({"operation": "basic_all", "parameters": {}}, sdk=SDK(PagedResult(5000, **options)))
    assert result["ok"] is False
    assert result["rows"] == []
    assert result["pagination"]["exhausted"] is False
    assert result["pagination"]["observed_records"] == 2000


def test_original_single_basic_contract_still_rejects_empty_or_implicit_market_code():
    for code in ("", "600000", "bj.920163"):
        with pytest.raises(ValueError):
            validate_request("basic", {"code": code})


def test_universe_requires_explicit_valid_date_and_no_symbol_limit_parameter():
    assert validate_request("universe", {"day": "2026-09-11"}) == {"day": "2026-09-11"}
    for parameters in ({"day": "20260911"}, {"day": "2026-02-30"}, {"day": "2026-09-11", "limit": "100"}):
        with pytest.raises(ValueError):
            validate_request("universe", parameters)
