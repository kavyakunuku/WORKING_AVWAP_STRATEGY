"""Tests for the optional index universe (market_data.universe_indices)."""
from common.models import OptionContract
from dhan.instruments import fo_index_universe, fo_stock_universe
from app import filter_configured_universe


def _mk(sid, underlying, instrument="OPTSTK"):
    return OptionContract(
        security_id=sid, symbol=f"{underlying} 100 CE", underlying=underlying,
        strike=100.0, option_type="CE", expiry="2026-09-29", lot_size=10,
        instrument=instrument,
    )


def test_index_universe_derivation():
    contracts = [
        _mk("1", "RELIANCE"),
        _mk("2", "INFY"),
        _mk("3", "NIFTY", "OPTIDX"),
        _mk("4", "BANKNIFTY", "OPTIDX"),
    ]
    assert fo_stock_universe(contracts) == ["INFY", "RELIANCE"]
    assert fo_index_universe(contracts) == ["BANKNIFTY", "NIFTY"]


def test_index_filter_configured():
    available = ["BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTY"]
    kept, skipped = filter_configured_universe(available, ["NIFTY", "BANKNIFTY", "SENSEX"])
    assert kept == ["BANKNIFTY", "NIFTY"]
    assert skipped == ["SENSEX"]


def test_index_filter_empty_allowed_means_no_restriction():
    # empty `allowed` = no restriction (the app itself only adds indices when
    # market_data.universe_indices is non-empty - opt-in)
    kept, skipped = filter_configured_universe(["NIFTY", "BANKNIFTY"], [])
    assert kept == ["NIFTY", "BANKNIFTY"]
    assert skipped == []
