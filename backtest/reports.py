"""Portable result JSON plus CSV exports (no changes to the trading database)."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from backtest.data import atomic_json
from common.utils import fmt_ist

EXPORTS = {"result.json", "trades.csv", "equity.csv", "daily.csv", "signals.csv"}


def _cell(value):
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False)
    # Avoid formula execution when exported metadata is opened in a spreadsheet.
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def write_csv(path, rows, fields):
    with Path(path).open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _cell(v) for k, v in row.items()})


def export_result(directory: Path, result: dict):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(directory / "result.json", result)
    trades = []
    for p in result["trades"]:
        row = dict(p)
        row["entry_time_ist"] = fmt_ist(p["entry_time"]) if p["entry_time"] else ""
        row["exit_time_ist"] = fmt_ist(p["exit_time"]) if p["exit_time"] else ""
        trades.append(row)
    write_csv(directory / "trades.csv", trades, [
        "position_id", "security_id", "symbol", "underlying", "strike", "option_type", "expiry",
        "quantity", "status", "entry_time", "entry_time_ist", "entry_price", "entry_avwap", "entry_reason",
        "exit_time", "exit_time_ist", "exit_price", "exit_avwap", "exit_reason",
        "gross_pnl", "costs", "net_pnl", "unrealized_pnl", "last_mark", "last_mark_ts",
    ])
    write_csv(directory / "equity.csv", result["equity_curve"], [
        "ts", "equity", "net_pnl", "realized_pnl", "unrealized_pnl", "costs",
        "open_positions", "drawdown", "drawdown_pct",
    ])
    write_csv(directory / "daily.csv", result["daily_pnl"], ["date", "pnl", "cumulative_pnl", "equity"])
    write_csv(directory / "signals.csv", result["signals"], [
        "signal_key", "security_id", "symbol", "action", "candle_ts", "created_at", "signal_price",
        "avwap", "prev_close", "prev_avwap", "reason", "underlying", "strike", "option_type", "expiry",
    ])
