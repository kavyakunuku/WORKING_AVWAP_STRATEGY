"""Historical scanner selection using the SAME expiry / ATM / ITM functions.

Dhan's current master is not a point-in-time chain archive. The provider labels
that limitation, and refuses missing expiry periods instead of letting the
live selector's nearest-future fallback substitute a different series.
"""
from __future__ import annotations

import math
from datetime import date

from backtest.models import BacktestError
from common.models import OptionContract
from market.universe import (
    ce_universe_strikes, find_atm, pe_universe_strikes,
    select_monthly_expiry, select_weekly_expiries,
)


class ReplayUniverse:
    def __init__(self, contracts: list[OptionContract], cfg: dict, symbols):
        self.symbols = tuple(symbols)
        self.width = int(cfg.get("strategy", {}).get("itm_strikes_per_side", 6))
        self.switch = int(cfg.get("strategy", {}).get("expiry_switch_day", 24))
        self.weeklies = cfg.get("market_data", {}).get("weekly_expiries", {}) or {}
        if not 0 <= self.width <= 20 or not 1 <= self.switch <= 31:
            raise BacktestError("Invalid scanner width or expiry switch day")
        self.legs = {}
        self.calendar = {u: set() for u in self.symbols}
        self.contracts = {}
        self._selected = {}
        for c in contracts:
            if c.underlying not in self.calendar:
                continue
            if c.security_id in self.contracts and self.contracts[c.security_id] != c:
                raise BacktestError(f"Security ID reused across contracts: {c.security_id}")
            if (c.option_type not in ("CE", "PE") or not c.security_id or
                    not math.isfinite(c.strike) or c.strike <= 0 or
                    c.lot_size <= 0 or c.lot_size != int(c.lot_size)):
                raise BacktestError(f"Invalid contract metadata: {c.security_id}")
            self.contracts[c.security_id] = c
            expiry = date.fromisoformat(c.expiry)
            self.calendar[c.underlying].add(expiry)
            leg = self.legs.setdefault((c.underlying, expiry), {})
            key = (c.strike, c.option_type)
            if key in leg and leg[key] != c:
                raise BacktestError(f"Ambiguous contract mapping: {c.underlying} {c.expiry} {key}")
            leg[key] = c

    def expiries(self, underlying: str, day: date) -> list[date]:
        key = underlying, day
        if key in self._selected:
            return self._selected[key]
        known = sorted(self.calendar[underlying])
        n = int(self.weeklies.get(underlying, 0) or 0)
        if n > 0:
            chosen = select_weekly_expiries(day, known, n)
            # A conservative coverage guard, NOT an assumed expiry weekday.
            # Without the prior weekly dates we cannot replay older weeks.
            if len(chosen) != n or (chosen[0] - day).days > 7:
                raise BacktestError(
                    f"{underlying}: weekly expiry coverage unavailable on {day}. "
                    "Dhan's current master cannot reconstruct expired weekly ladders; "
                    "choose a recent covered period or supply a verified contract-level archive."
                )
        else:
            month = (day.year, day.month)
            next_month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
            target = month if day.day < self.switch else next_month
            in_month = [e for e in known if (e.year, e.month) == target]
            if not in_month:
                raise BacktestError(
                    f"{underlying}: no contract metadata for monthly expiry {target[0]}-{target[1]:02d}. "
                    "Expired periods cannot be substituted with current contracts."
                )
            chosen_expiry = select_monthly_expiry(day, known, self.switch)
            if chosen_expiry is None or (chosen_expiry.year, chosen_expiry.month) not in (target, next_month):
                raise BacktestError(f"{underlying}: monthly expiry coverage unavailable on {day}")
            # A 24th switch must not silently fall back to the current month.
            if day.day >= self.switch and (chosen_expiry.year, chosen_expiry.month) != target:
                raise BacktestError(f"{underlying}: next month's monthly expiry is unavailable on {day}")
            chosen = [chosen_expiry]
        self._selected[key] = chosen
        return chosen

    def scanner(self, underlying: str, day: date, spot: float) -> set[str]:
        out = set()
        for expiry in self.expiries(underlying, day):
            leg = self.legs[(underlying, expiry)]
            strikes = sorted({strike for strike, _ in leg})
            atm = find_atm(spot, strikes)
            if atm is None:
                continue
            for side, selected in (
                ("CE", ce_universe_strikes(atm, strikes, self.width)),
                ("PE", pe_universe_strikes(atm, strikes, self.width)),
            ):
                out.update(leg[s, side].security_id for s in selected if (s, side) in leg)
        return out
