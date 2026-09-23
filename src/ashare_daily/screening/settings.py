"""M2 runtime settings, separate from the preserved v2 DEMO example."""

from decimal import Decimal
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrategyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    strategy_version: str = Field(min_length=1)
    sample_source: Literal["m1_config"] = "m1_config"
    benchmark_id: str = Field(pattern=r"^(sh|sz)\.[0-9]{6}$")
    benchmark_name: str = Field(min_length=1)
    benchmark_selection_note: str = ""
    min_history_trading_days: int = Field(default=120, ge=120, le=240)
    ma_short_days: int = Field(default=20, ge=2, le=120)
    ma_long_days: int = Field(default=60, ge=3, le=240)
    return_days: int = Field(default=20, ge=1, le=239)
    amount_days: int = Field(default=20, ge=1, le=240)
    min_avg_amount_cny: Decimal = Field(default=Decimal("50000000"), ge=0, allow_inf_nan=False)
    max_candidates: int = Field(default=20, ge=1, le=20)
    trend_adjustment_mode: Literal["forward_adjusted"] = "forward_adjusted"
    display_adjustment_mode: Literal["unadjusted"] = "unadjusted"
    allowed_exchanges: list[Literal["SH", "SZ"]] = Field(default_factory=lambda: ["SH", "SZ"])
    allowed_board: Literal["mainboard"] = "mainboard"

    @model_validator(mode="after")
    def check_windows(self):
        if self.ma_short_days >= self.ma_long_days:
            raise ValueError("短均线窗口必须小于长均线窗口")
        if not self.allowed_exchanges or len(set(self.allowed_exchanges)) != len(self.allowed_exchanges):
            raise ValueError("交易所配置不得为空或重复")
        return self

    @property
    def history_days(self) -> int:
        return max(self.min_history_trading_days, self.ma_long_days, self.return_days + 1, self.amount_days)


def load_config(path: Path | str = Path("config/m2.json")) -> StrategyConfig:
    return StrategyConfig.model_validate(json.loads(Path(path).read_text(encoding="utf-8-sig")))
