"""Owner-approved entry universe. Open-position monitoring is NOT restricted.

Configuration may narrow this list, but may never expand it. In particular,
legacy configurations with [] or a wider stock list cannot re-enable the full
F&O master. BSE here is the NSE-listed BSE Ltd stock, not the BSE exchange.
"""
from __future__ import annotations

import logging

APPROVED_STOCKS = (
    "RELIANCE", "HDFCBANK", "SBIN", "ICICIBANK", "INFY", "TCS", "BEL", "HAL",
    "BSE", "DIXON", "AXISBANK", "BHARTIARTL", "BAJFINANCE", "ETERNAL",
    "KOTAKBANK", "VEDL", "HINDZINC", "ITC", "TATASTEEL", "TATAMOTORS", "LT",
    "M&M", "ADANIENT", "ADANIPORTS", "MARUTI", "HCLTECH", "SUNPHARMA", "TRENT",
    "JIOFIN", "COALINDIA", "IOC", "CANBK", "INDUSINDBK", "PFC", "RECLTD",
    "HINDALCO", "NATIONALUM", "JINDALSTEL", "ONGC", "BPCL",
)
APPROVED_INDICES = ("NIFTY", "BANKNIFTY")


def configured_scanner(cfg: dict, *, warn: bool = False) -> tuple[list[str], list[str]]:
    md = cfg.get("market_data", {})
    stocks = md.get("universe_stocks") or list(APPROVED_STOCKS)
    indices = md.get("universe_indices", list(APPROVED_INDICES))
    result = []
    for requested, approved, name in (
        (stocks, APPROVED_STOCKS, "universe_stocks"),
        (indices, APPROVED_INDICES, "universe_indices"),
    ):
        if not isinstance(requested, (list, tuple)) or not all(isinstance(s, str) for s in requested):
            raise ValueError(f"market_data.{name} must be a list of symbols")
        wanted = {s.strip().upper() for s in requested if s.strip()}
        excluded = wanted - set(approved)
        if warn and excluded:
            logging.getLogger("avwap.scanner").warning(
                "%s: ignoring symbols outside the approved scanner: %s", name, sorted(excluded)
            )
        result.append([s for s in approved if s in wanted])
    return result[0], result[1]


def scanner_symbols(cfg: dict) -> list[str]:
    stocks, indices = configured_scanner(cfg)
    return stocks + indices
