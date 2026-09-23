"""Bounded technical sample. This list is not a recommendation or a universe."""

from datetime import time
from pathlib import Path

SAMPLE_ID = "m1-mainboard-v1"
SCOPE = "小样本验证：1 个指数 + 6 只沪深主板 A 股；仅技术联调，不代表推荐或全市场覆盖。"
SAMPLE_TYPES = {
    "sh.000001": "index",
    "sh.600000": "stock",
    "sh.600036": "stock",
    "sh.601398": "stock",
    "sz.000001": "stock",
    "sz.000333": "stock",
    "sz.000651": "stock",
}
DEFAULT_DATABASE = Path("data/research/market.sqlite3")
DEFAULT_EVIDENCE_DIR = Path("outputs/research/m1")
EARLIEST_DAILY_CHECK = time(18, 15)
MAX_HISTORY_DAYS = 365  # Difference between endpoints: at most 366 calendar dates.
OVERLAP_TRADING_DAYS = 3
MAX_WINDOWS_PER_SYMBOL = 8
