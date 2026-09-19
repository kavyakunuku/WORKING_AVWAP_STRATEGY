"""Multi-expiry universe legs + weekly expiry selection (NIFTY weeklies)."""
from __future__ import annotations

from datetime import date

from common.models import OptionContract
from market.universe import UniverseManager, select_weekly_expiries

WEEKLY = [date(2026, 9, 15), date(2026, 9, 22), date(2026, 9, 29), date(2026, 10, 6)]


def test_weekly_selection_current_and_next():
    assert select_weekly_expiries(date(2026, 9, 15), WEEKLY, 2) == [
        date(2026, 9, 15), date(2026, 9, 22),
    ]
    assert select_weekly_expiries(date(2026, 9, 16), WEEKLY, 2) == [
        date(2026, 9, 22), date(2026, 9, 29),
    ]
    assert select_weekly_expiries(date(2026, 9, 1), WEEKLY, 1) == [date(2026, 9, 15)]
    assert select_weekly_expiries(date(2026, 9, 1), WEEKLY, 0) == []


def test_multi_leg_universe():
    u = UniverseManager(it_count=1)

    def mk(sid, strike, expiry):
        return OptionContract(sid, f"N {strike:g} CE", "N", strike, "CE",
                              expiry, 75, instrument="OPTIDX")

    leg1 = {(23100.0, "CE"): mk("A1", 23100, "2026-09-15"),
            (23050.0, "CE"): mk("A2", 23050, "2026-09-15")}
    leg2 = {(23100.0, "CE"): mk("B1", 23100, "2026-09-22"),
            (23050.0, "CE"): mk("B2", 23050, "2026-09-22")}

    u.update("NIFTY", date(2026, 9, 15), 23110.0, [23050.0, 23100.0], leg1)
    u.update("NIFTY", date(2026, 9, 22), 23110.0, [23050.0, 23100.0], leg2)

    # ONE underlying entry, TWO legs
    assert list(u.by_underlying) == ["NIFTY"]
    assert sorted(u.by_underlying["NIFTY"].expiries) == ["2026-09-15", "2026-09-22"]

    # scanner = ATM±1 of EACH leg (ATM 23100 -> 23100 + 23050 per leg)
    assert u.scanner_ids() == {"A1", "A2", "B1", "B2"}

    # info() = one row per leg
    rows = u.info()
    assert [r["expiry"] for r in rows] == ["2026-09-15", "2026-09-22"]
    assert all(r["underlying"] == "NIFTY" for r in rows)


def test_refresh_atm_updates_every_leg():
    u = UniverseManager(it_count=1)

    def mk(sid, strike, expiry):
        return OptionContract(sid, f"N {strike:g} PE", "N", strike, "PE",
                              expiry, 75, instrument="OPTIDX")

    # different strike grids per leg (realistic: grids extend over time)
    u.update("NIFTY", date(2026, 9, 15), 23100.0, [23000.0, 23100.0],
             {(23000.0, "PE"): mk("P1", 23000, "2026-09-15"),
              (23100.0, "PE"): mk("P2", 23100, "2026-09-15")})
    u.update("NIFTY", date(2026, 9, 22), 23100.0, [23000.0, 23100.0, 23200.0],
             {(23000.0, "PE"): mk("Q1", 23000, "2026-09-22"),
              (23100.0, "PE"): mk("Q2", 23100, "2026-09-22"),
              (23200.0, "PE"): mk("Q3", 23200, "2026-09-22")})

    e1 = u.by_underlying["NIFTY"].expiries["2026-09-15"]
    e2 = u.by_underlying["NIFTY"].expiries["2026-09-22"]
    assert e1.atm == 23100.0 and e2.atm == 23100.0
    # PE side = ATM + 1 above: leg1 has no strike above 23100 -> ATM only;
    # leg2 has 23200
    assert e1.pe_strikes == [23100.0]
    assert e2.pe_strikes == [23100.0, 23200.0]

    # spot rallies: both legs recompute from their OWN grids
    u.refresh_atm("NIFTY", 23190.0)
    assert e1.atm == 23100.0          # closest in leg1's grid
    assert e2.atm == 23200.0          # closest in leg2's grid


def test_drop_expiries_removes_rolled_legs():
    u = UniverseManager(it_count=1)

    def mk(sid, strike, expiry):
        return OptionContract(sid, f"N {strike:g} CE", "N", strike, "CE",
                              expiry, 75, instrument="OPTIDX")

    u.update("NIFTY", date(2026, 9, 15), 23100.0, [23100.0],
             {(23100.0, "CE"): mk("A1", 23100, "2026-09-15")})
    u.update("NIFTY", date(2026, 9, 22), 23100.0, [23100.0],
             {(23100.0, "CE"): mk("B1", 23100, "2026-09-22")})
    assert sorted(u.by_underlying["NIFTY"].expiries) == ["2026-09-15", "2026-09-22"]

    # weekly roll: 09-15 leg is gone, 09-29 arrives
    removed = u.drop_expiries("NIFTY", {"2026-09-22", "2026-09-29"})
    assert removed == ["2026-09-15"]
    assert sorted(u.by_underlying["NIFTY"].expiries) == ["2026-09-22"]
    assert u.scanner_ids() == {"B1"}

    # unknown underlying is a no-op
    assert u.drop_expiries("NOPE", {"2026-09-22"}) == []
