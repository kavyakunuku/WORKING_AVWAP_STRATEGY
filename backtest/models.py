"""Validated replay inputs. All dates and candle boundaries are in IST."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from common.models import Candle, OptionContract
from common.scanner import scanner_symbols
from common.utils import IST, candle_start_for, epoch, from_epoch, now_ist


class BacktestError(ValueError):
    """An actionable configuration or historical-data error (safe for the UI)."""


class BacktestCancelled(Exception):
    pass


@dataclass(frozen=True)
class BacktestRequest:
    start: date
    end: date
    history_start: date
    symbols: tuple[str, ...]
    source: str = "dhan"
    initial_capital: float = 1_000_000.0
    slippage_bps: float = 0.0
    fee_per_order: float = 0.0
    cost_bps: float = 0.0
    close_at_end: bool = False
    fill_mode: str = "candle_close"

    @classmethod
    def parse(cls, data: dict, cfg: dict, today: date | None = None) -> "BacktestRequest":
        if not isinstance(data, dict):
            raise BacktestError("Backtest request must be a JSON object")
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise BacktestError("Unknown backtest fields: " + ", ".join(sorted(unknown)))
        try:
            start = date.fromisoformat(data["start"])
            end = date.fromisoformat(data["end"])
            history = date.fromisoformat(data.get("history_start") or start.replace(day=1).isoformat())
        except (KeyError, TypeError, ValueError) as e:
            raise BacktestError("start, end and history_start must be YYYY-MM-DD dates") from e
        if not history <= start <= end:
            raise BacktestError("Required: history_start <= start <= end (end date is inclusive)")
        if end > (today or now_ist().date()):
            raise BacktestError("Future end dates are not allowed")
        if (end - start).days + 1 > int(cfg.get("backtest", {}).get("max_days", 366)):
            raise BacktestError("Date range exceeds backtest.max_days; use a smaller range")
        if (start - history).days > 730:
            raise BacktestError("AVWAP warm-up cannot exceed 730 calendar days")
        allowed = scanner_symbols(cfg)
        symbols = data.get("symbols", allowed)
        if not isinstance(symbols, (list, tuple)) or not symbols or not all(isinstance(s, str) for s in symbols):
            raise BacktestError("Select at least one scanner symbol")
        symbols = tuple(dict.fromkeys(s.strip().upper() for s in symbols))
        invalid = set(symbols) - set(allowed)
        if invalid:
            raise BacktestError("Symbols outside the configured approved scanner: " + ", ".join(sorted(invalid)))
        source = data.get("source", "dhan")
        if source not in ("dhan", "demo"):
            raise BacktestError("source must be dhan or demo (no automatic fallback)")
        if source == "demo" and (end - history).days > 93:
            raise BacktestError("Synthetic demos are limited to 93 days including warm-up")
        if data.get("fill_mode", "candle_close") != "candle_close":
            raise BacktestError("This version supports signal-candle-close fills only")
        if not isinstance(data.get("close_at_end", False), bool):
            raise BacktestError("close_at_end must be true or false")
        numbers = {}
        defaults = {"initial_capital": cfg.get("backtest", {}).get("initial_capital", 1_000_000),
                    "slippage_bps": cfg.get("paper", {}).get("slippage_bps", 0),
                    "fee_per_order": 0, "cost_bps": 0}
        for key, default in defaults.items():
            value = data.get(key, default)
            try:
                if isinstance(value, bool):
                    raise ValueError()
                number = float(value)
            except (TypeError, ValueError) as e:
                raise BacktestError(f"{key} must be a finite number") from e
            if not math.isfinite(number) or number < 0 or (key == "initial_capital" and number == 0):
                raise BacktestError(f"Invalid {key}")
            if key.endswith("bps") and number > 1000:
                raise BacktestError(f"{key} must be between 0 and 1000")
            numbers[key] = number
        return cls(start, end, history, symbols, source=source,
                   close_at_end=data.get("close_at_end", False), **numbers)

    def to_dict(self) -> dict:
        from dataclasses import asdict
        out = asdict(self)
        for key in ("start", "end", "history_start"):
            out[key] = out[key].isoformat()
        out["symbols"] = list(self.symbols)
        return out

    @property
    def start_ts(self) -> int:
        return epoch(datetime.combine(self.start, datetime.min.time(), IST))

    @property
    def end_ts(self) -> int:
        return epoch(datetime.combine(self.end + timedelta(days=1), datetime.min.time(), IST))

    @property
    def history_ts(self) -> int:
        return epoch(datetime.combine(self.history_start, datetime.min.time(), IST))


@dataclass
class BacktestDataset:
    contracts: list[OptionContract]
    candles: dict[str, list[Candle]]
    spots: dict[str, list[Candle]]
    metadata: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def validated_bars(bars: list[Candle], security_id: str | None = None) -> list[Candle]:
    """Sort/dedupe exact duplicates, but never quietly repair conflicting bars."""
    result = {}
    for c in bars:
        if security_id is not None and c.security_id != security_id:
            raise BacktestError(f"Candle security ID mismatch for {security_id}")
        try:
            values = (c.open, c.high, c.low, c.close, c.volume, c.ts)
            if any(isinstance(v, bool) for v in values) or not all(math.isfinite(v) for v in values):
                raise ValueError()
            if c.ts != int(c.ts) or c.volume != int(c.volume) or c.volume < 0:
                raise ValueError()
            if not (0 < c.low <= min(c.open, c.close) <= max(c.open, c.close) <= c.high):
                raise ValueError()
            dt = from_epoch(c.ts)
            window = candle_start_for(dt, 15)
            if dt.weekday() >= 5 or window is None or epoch(window) != c.ts:
                raise ValueError()
        except (TypeError, ValueError, OverflowError, OSError) as e:
            raise BacktestError(f"Invalid 15-minute OHLCV candle: {c.security_id} at {c.ts}") from e
        if c.ts in result and result[c.ts] != c:
            raise BacktestError(f"Conflicting duplicate candles: {c.security_id} at {c.ts}")
        result[c.ts] = c
    return [result[ts] for ts in sorted(result)]
