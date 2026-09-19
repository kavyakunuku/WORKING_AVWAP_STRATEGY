"""Deterministic replay, shared rules, costs, clocks, and isolation regressions."""
import copy
import json
import threading
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from backtest.engine import BacktestEngine
from backtest.models import BacktestCancelled, BacktestDataset, BacktestError, BacktestRequest, validated_bars
from common.config import DEFAULTS
from common.models import Candle, OptionContract
from common.utils import IST, epoch
from storage.database import Database


def config(tmp_path):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["market_data"].update(universe_stocks=["RELIANCE"], universe_indices=["NIFTY", "BANKNIFTY"])
    cfg["strategy"]["itm_strikes_per_side"] = 0
    cfg["storage"]["db_path"] = str(tmp_path / "trading.db")
    cfg["backtest"].update(output_dir=str(tmp_path / "backtests"), history_request_gap_seconds=0)
    cfg["risk"].update(max_open_positions=8, max_trades_per_day=20, max_daily_loss=100000)
    return cfg


def bar(sid, index, close, day=15, volume=100):
    ts = epoch(datetime(2026, 9, day, 9, 15, tzinfo=IST) + timedelta(minutes=index * 15))
    return Candle(sid, ts, close, close, close, close, volume)


def request(cfg, **changes):
    payload = dict(start="2026-09-15", end="2026-09-15", history_start="2026-09-14",
                   symbols=["RELIANCE"], initial_capital=10000)
    payload.update(changes)
    return BacktestRequest.parse(payload, cfg, today=date(2026, 9, 19))


def dataset(prices=(120, 90, 80, 120)):
    c = OptionContract("01", "RELIANCE 100 CE", "RELIANCE", 100, "CE", "2026-09-29", 25)
    candles = [bar("01", 0, 100, day=14)] + [bar("01", i, p) for i, p in enumerate(prices)]
    return BacktestDataset([c], {"01": candles}, {"RELIANCE": [bar("SPOT", i, 100) for i in range(len(prices))]},
                           metadata={"source": "TEST_FIXTURE"})


def replay(tmp_path, data=None, req=None, cfg=None, name="replay.db"):
    cfg = cfg or config(tmp_path)
    return BacktestEngine(cfg).run(req or request(cfg), data or dataset(), tmp_path / name)


def test_shared_entry_hold_exit_and_close_time(tmp_path):
    result = replay(tmp_path)
    assert [s["action"] for s in result["signals"]] == ["ENTRY_SELL", "EXIT_BUY"]
    trade = result["trades"][0]
    assert trade["entry_price"] == 90 and trade["exit_price"] == 120
    assert trade["entry_time"] == bar("01", 1, 90).ts + 900
    assert trade["exit_time"] == bar("01", 3, 120).ts + 900
    assert result["metrics"]["net_pnl"] == -750
    assert result["metrics"]["max_drawdown"] == 1000
    assert result["metrics"]["closed_trades"] == 1
    assert result["metrics"]["win_rate_pct"] == 0
    assert result["metrics"]["profit_factor"] == 0
    json.dumps(result, allow_nan=False)


def test_warmup_has_no_trades_or_first_candle_manufactured_entry(tmp_path):
    data = dataset((70, 110, 60))
    data.candles["01"] = [bar("01", 0, 120, 14), bar("01", 1, 80, 14)] + data.candles["01"][1:]
    result = replay(tmp_path, data)
    assert len(result["signals"]) == 1
    assert result["signals"][0]["candle_ts"] == bar("01", 2, 60).ts
    assert result["trades"][0]["status"] == "OPEN"


def test_fresh_cross_required_for_reentry(tmp_path):
    result = replay(tmp_path, dataset((120, 90, 80, 120, 130, 90)))
    assert [s["action"] for s in result["signals"]] == ["ENTRY_SELL", "EXIT_BUY", "ENTRY_SELL"]
    assert result["metrics"]["total_trades"] == 2
    assert result["metrics"]["open_trades"] == 1


def test_position_exits_outside_scanner_but_cannot_reenter(tmp_path):
    data = dataset((120, 90, 120, 80))
    second = replace(data.contracts[0], security_id="02", strike=110, symbol="RELIANCE 110 CE")
    data.contracts.append(second)
    data.candles["02"] = [bar("02", 0, 100, 14)] + [bar("02", i, 100) for i in range(4)]
    data.spots["RELIANCE"] = [bar("SPOT", i, p) for i, p in enumerate((100, 100, 110, 110))]
    result = replay(tmp_path, data)
    assert len(result["trades"]) == 1
    assert result["trades"][0]["security_id"] == "01"
    assert result["trades"][0]["status"] == "CLOSED"
    assert len(result["signals"]) == 2


def test_avwap_continues_before_contract_joins_scanner(tmp_path):
    data = dataset((200, 90))
    second = replace(data.contracts[0], security_id="02", strike=110, symbol="RELIANCE 110 CE")
    data.contracts.append(second)
    data.candles["02"] = [bar("02", 0, 100, 14), bar("02", 0, 100), bar("02", 1, 100)]
    data.spots["RELIANCE"] = [bar("SPOT", 0, 110), bar("SPOT", 1, 100)]
    result = replay(tmp_path, data)
    assert result["trades"][0]["entry_avwap"] == pytest.approx(130)


def test_slippage_costs_equity_and_net_trade_pnl(tmp_path):
    cfg = config(tmp_path)
    req = request(cfg, slippage_bps=100, fee_per_order=2, cost_bps=10)
    result = replay(tmp_path, req=req, cfg=cfg)
    p = result["trades"][0]
    assert p["entry_price"] == pytest.approx(89.1)
    assert p["exit_price"] == pytest.approx(121.2)
    assert p["gross_pnl"] == pytest.approx(-802.5)
    assert p["costs"] == pytest.approx(9.2575)
    assert p["net_pnl"] == pytest.approx(-811.7575)
    assert result["metrics"]["net_pnl"] == pytest.approx(p["net_pnl"])
    assert result["equity_curve"][-1]["equity"] == pytest.approx(10000 + p["net_pnl"])
    assert sum(d["pnl"] for d in result["daily_pnl"]) == pytest.approx(p["net_pnl"])


@pytest.mark.parametrize("gate,value,reason", [("max_trades_per_day", 1, "MAX_TRADES_PER_DAY"),
                                                ("max_daily_loss", 10, "MAX_DAILY_LOSS")])
def test_risk_limits_block_fresh_reentry(tmp_path, gate, value, reason):
    cfg = config(tmp_path)
    cfg["risk"][gate] = value
    result = replay(tmp_path, dataset((120, 90, 80, 120, 130, 90)), cfg=cfg)
    assert result["metrics"]["total_trades"] == 1
    assert any(reason in r for b in result["blocked_entries"] for r in b["reasons"])


def test_same_timestamp_ordering_and_entry_fees_gate_other_entries(tmp_path):
    data = dataset()
    pe = replace(data.contracts[0], security_id="02", option_type="PE", symbol="RELIANCE 100 PE")
    data.contracts.append(pe)
    data.candles["02"] = [replace(b, security_id="02") for b in data.candles["01"]]
    cfg = config(tmp_path)
    cfg["risk"]["max_daily_loss"] = 5
    result = replay(tmp_path, data, cfg=cfg, req=request(cfg, fee_per_order=10))
    assert len(result["trades"]) == 1 and result["trades"][0]["security_id"] == "01"
    assert "MAX_DAILY_LOSS" in result["blocked_entries"][0]["reasons"][0]


def test_max_positions_gate_and_ce_pe_independence(tmp_path):
    data = dataset()
    pe = replace(data.contracts[0], security_id="02", option_type="PE", symbol="RELIANCE 100 PE")
    data.contracts.append(pe)
    data.candles["02"] = [replace(b, security_id="02") for b in data.candles["01"]]
    cfg = config(tmp_path)
    cfg["risk"]["max_open_positions"] = 1
    result = replay(tmp_path, data, cfg=cfg)
    assert result["metrics"]["total_trades"] == 1
    assert any("MAX_OPEN_POSITIONS" in r for b in result["blocked_entries"] for r in b["reasons"])


def test_open_positions_are_marked_and_end_liquidation_is_explicit(tmp_path):
    data = dataset((120, 90, 80))
    cfg = config(tmp_path)
    open_result = replay(tmp_path, data, cfg=cfg, name="open.db")
    assert open_result["metrics"]["open_trades"] == 1
    assert open_result["metrics"]["unrealized_pnl"] == 250
    closed = replay(tmp_path, data, cfg=cfg, req=request(cfg, close_at_end=True), name="closed.db")
    assert closed["trades"][0]["exit_reason"] == "BACKTEST_END"
    assert closed["trades"][0]["exit_time"] == bar("01", 2, 80).ts + 900
    assert closed["metrics"]["net_pnl"] == open_result["metrics"]["net_pnl"]


def test_no_stale_end_fill_and_no_fabricated_missing_candles(tmp_path):
    data = dataset((120, 90, 80))
    data.candles["01"].pop()  # underlying has a later bar, option does not
    cfg = config(tmp_path)
    result = replay(tmp_path, data, cfg=cfg, req=request(cfg, close_at_end=True))
    assert result["metrics"]["open_trades"] == 1
    assert result["coverage"]["missing_option_windows"]["01"] == 1
    assert result["coverage"]["stale_position_marks"] == 1
    assert any("liquidation skipped" in w for w in result["warnings"])


def test_missing_spot_disables_entry_but_not_exit(tmp_path):
    data = dataset()
    data.spots["RELIANCE"].pop()  # exit still evaluated without underlying data
    result = replay(tmp_path, data)
    assert result["metrics"]["closed_trades"] == 1
    assert result["coverage"]["missing_spot_windows"]["RELIANCE"] == 1


def test_final_1515_candle_is_processed(tmp_path):
    data = dataset((120, 90))
    data.candles["01"].append(bar("01", 24, 150))
    data.spots["RELIANCE"].append(bar("SPOT", 24, 100))
    result = replay(tmp_path, data)
    assert result["trades"][0]["exit_time"] == epoch(datetime(2026, 9, 15, 15, 30, tzinfo=IST))


def test_no_lookahead_from_future_bars_and_duplicate_idempotence(tmp_path):
    original = dataset((120, 90, 80, 120))
    baseline = replay(tmp_path, original, name="base.db")
    data = copy.deepcopy(original)
    data.candles["01"] += [bar("01", 0, 999, 16), data.candles["01"][2]]
    data.candles["01"].reverse()
    data.spots["RELIANCE"].append(bar("SPOT", 0, 999, 16))
    again = replay(tmp_path, data, name="again.db")
    assert again["signals"] == baseline["signals"]
    assert again["equity_curve"] == baseline["equity_curve"]


def test_conflicting_duplicate_and_nonfinite_data_are_rejected():
    c = bar("01", 0, 100)
    with pytest.raises(BacktestError, match="Conflicting duplicate"):
        validated_bars([c, replace(c, volume=200)])
    with pytest.raises(BacktestError, match="Invalid"):
        validated_bars([replace(c, high=float("nan"))])
    with pytest.raises(BacktestError, match="Invalid"):
        validated_bars([replace(c, volume=-1)])


def test_missing_contract_series_is_not_silently_dropped(tmp_path):
    data = dataset()
    c = replace(data.contracts[0], security_id="02", option_type="PE")
    data.contracts.append(c)
    with pytest.raises(BacktestError, match="Missing option history"):
        replay(tmp_path, data)


def test_live_state_is_never_read_or_modified_and_live_broker_never_built(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    cfg["trading_mode"] = "LIVE"
    from execution.live import DhanBroker
    monkeypatch.setattr(DhanBroker, "__init__", lambda *a, **k: pytest.fail("Live broker constructed!"))
    db = Database(cfg["storage"]["db_path"])
    db.kv_set("risk.emergency_stop", True)
    result = replay(tmp_path, cfg=cfg)
    assert result["metrics"]["closed_trades"] == 1  # did not inherit live emergency state
    assert db.kv_get("risk.emergency_stop") is True
    assert not db.get_positions()
    assert not db._query("SELECT * FROM signals")
    with pytest.raises(BacktestError, match="isolated database"):
        BacktestEngine(cfg).run(request(cfg), dataset(), cfg["storage"]["db_path"])
    db.close()


def test_cancelled_replay_does_not_return_results(tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(BacktestCancelled):
        BacktestEngine(config(tmp_path)).run(request(config(tmp_path)), dataset(), tmp_path / "cancel.db", cancel=cancel)


def test_expired_open_positions_are_flagged_not_silently_settled(tmp_path):
    data = dataset((120, 90))
    data.contracts[0] = replace(data.contracts[0], expiry="2026-09-15")
    result = replay(tmp_path, data)
    assert result["coverage"]["expired_open_positions"] == ["01"]
    assert any("UNRESOLVED EXPIRY" in w for w in result["warnings"])


def test_equal_avwap_holds_and_first_closed_bar_can_use_prior_day(tmp_path):
    result = replay(tmp_path, dataset((90, 95, 96)))
    assert [s["action"] for s in result["signals"]] == ["ENTRY_SELL", "EXIT_BUY"]
    assert result["signals"][0]["created_at"] == bar("01", 0, 90).ts + 900
    assert result["signals"][1]["candle_ts"] == bar("01", 2, 96).ts


def test_multi_day_avwap_persists_and_daily_trade_limit_resets(tmp_path):
    data = dataset((120, 90))
    data.candles["01"] += [bar("01", 0, 200, 16), bar("01", 1, 80, 16)]
    data.spots["RELIANCE"] += [bar("SPOT", 0, 100, 16), bar("SPOT", 1, 100, 16)]
    cfg = config(tmp_path)
    cfg["risk"]["max_trades_per_day"] = 1
    result = replay(tmp_path, data, cfg=cfg, req=request(cfg, end="2026-09-16"))
    assert result["signals"][1]["action"] == "EXIT_BUY"
    assert result["signals"][1]["avwap"] == pytest.approx((100 + 120 + 90 + 200) / 4)
    assert result["metrics"]["total_trades"] == 2
    assert not result["blocked_entries"]
    assert len(result["daily_pnl"]) == 2
    assert sum(d["pnl"] for d in result["daily_pnl"]) == result["metrics"]["net_pnl"]


def test_monthly_roll_keeps_old_position_monitored(tmp_path):
    old = dataset().contracts[0]
    new = replace(old, security_id="02", expiry="2026-10-27")
    data = BacktestDataset([old, new], {
        "01": [bar("01", 0, 100, 22), bar("01", 0, 90, 23), bar("01", 0, 120, 24)],
        "02": [bar("02", 0, 100, 22), bar("02", 0, 100, 23), bar("02", 0, 90, 24)],
    }, {"RELIANCE": [bar("SPOT", 0, 100, 23), bar("SPOT", 0, 100, 24)]})
    cfg = config(tmp_path)
    # Explicit historical request construction: dates are synthetic fixture
    # timestamps, not a request to a provider for future real market data.
    req = BacktestRequest(date(2026, 9, 23), date(2026, 9, 24), date(2026, 9, 22), ("RELIANCE",))
    result = replay(tmp_path, data, cfg=cfg, req=req)
    assert result["trades"][0]["status"] == "CLOSED"
    assert result["trades"][0]["expiry"] == "2026-09-29"
    assert result["trades"][1]["status"] == "OPEN"
    assert result["trades"][1]["expiry"] == "2026-10-27"


def test_multi_weekly_legs_have_independent_avwap_and_positions(tmp_path):
    cfg = config(tmp_path)
    cfg["market_data"]["weekly_expiries"] = {"NIFTY": 2}
    a = OptionContract("01", "NIFTY 100 CE", "NIFTY", 100, "CE", "2026-09-15", 75, "OPTIDX")
    b = replace(a, security_id="02", strike=105, expiry="2026-09-22")
    data = BacktestDataset([a, b], {
        "01": [bar("01", 0, 100, 14)] + [bar("01", i, p) for i, p in enumerate((120, 90, 80, 120))],
        "02": [bar("02", 0, 200, 14)] + [bar("02", i, p) for i, p in enumerate((200, 180, 160, 240))],
    }, {"NIFTY": [bar("SPOT", i, 100) for i in range(4)]})
    result = replay(tmp_path, data, cfg=cfg, req=request(cfg, symbols=["NIFTY"]))
    assert result["metrics"]["closed_trades"] == 2
    entries = {s["security_id"]: s["avwap"] for s in result["signals"] if s["action"] == "ENTRY_SELL"}
    assert entries["01"] == pytest.approx(310 / 3)
    assert entries["02"] == pytest.approx(580 / 3)
