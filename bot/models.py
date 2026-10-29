from decimal import Decimal
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SessionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capital: Decimal = Field(default=Decimal("500"), gt=0, le=1_000_000_000)
    target: Decimal = Field(default=Decimal("1000"), gt=0, le=1_000_000_000)
    max_loss: Decimal = Field(default=Decimal("300"), gt=0)
    symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"], min_length=1, max_length=10)
    order_budget: Decimal = Field(default=Decimal("50"), gt=0)
    leverage: int = Field(default=5, ge=1, le=20, strict=True)
    stop_loss_pct: Decimal = Field(default=Decimal("2"), gt=0, le=50)
    take_profit_pct: Decimal = Field(default=Decimal("4"), gt=0, le=100)
    fee_bps: Decimal = Field(default=Decimal("5"), ge=0, le=100)
    slippage_bps: Decimal = Field(default=Decimal("5"), ge=0, le=100)
    funding_bps: Decimal = Field(default=Decimal("1"), ge=-100, le=100)

    @field_validator("symbols")
    @classmethod
    def valid_symbols(cls, values):
        values = [s.strip().upper() for s in values]
        if any(not re.fullmatch(r"[A-Z0-9]{2,16}USDT", s) for s in values):
            raise ValueError("Nhập cặp Futures dạng BTCUSDT, ETHUSDT; phiên này dùng USDT.")
        if len(set(values)) != len(values):
            raise ValueError("Danh sách mã không được trùng lặp.")
        return values

    @model_validator(mode="after")
    def limits(self):
        if self.target <= self.capital:
            raise ValueError("Mục tiêu phải lớn hơn vốn ban đầu.")
        if self.max_loss >= self.capital:
            raise ValueError("Mức lỗ tối đa phải nhỏ hơn vốn ban đầu.")
        if self.order_budget > self.capital:
            raise ValueError("Ngân sách mỗi lệnh không được vượt vốn ban đầu.")
        return self
