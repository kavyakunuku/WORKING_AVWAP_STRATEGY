"""Dhan history requests are mocked: these tests never contact a broker."""
import copy
import threading
from dataclasses import asdict, replace
from datetime import date, datetime

import pytest

from backtest.data import DhanHistorySource, parse_history
from backtest.models import BacktestCancelled, BacktestError, BacktestRequest
from backtest.universe import ReplayUniverse
from common.models import OptionContract
from common.utils import IST
from dhan.client import DhanAPIError
from dhan.instruments import MasterBundle
from test_backtest_engine import bar, config, request


def columnar(bars):
    return {key: [getattr(b, "ts" if key == "timestamp" else key) for b in bars]
            for key in ("timestamp", "open", "high", "low", "close", "volume")}


class Rest:
    def __init__(self, values=None):
        self.calls = []
        self.values = values or {}

    def post(self, path, payload, retries):
        self.calls.append((path, payload, retries))
        return columnar(self.values.get(payload["securityId"], []))


def source(tmp_path, cfg=None, rest=None, contract=None, now=None):
    cfg = cfg or config(tmp_path)
    contract = contract or OptionContract("111", "RELIANCE 100 CE", "RELIANCE", 100, "CE", "2026-09-29", 25)
    bundle = MasterBundle(contracts=[contract], underlying_ids={contract.underlying: "1000"})
    return DhanHistorySource(cfg, tmp_path / "cache", rest=rest or Rest(),
                             master_loader=lambda *a: bundle,
                             now_fn=lambda: now or datetime(2026, 9, 19, 10, tzinfo=IST))


def test_dhan_load_inclusive_end_spots_and_cache(tmp_path):
    cfg = config(tmp_path)
    rest = Rest({"1000": [bar("1000", 0, 100)],
                 "111": [bar("111", 0, 100, 14), bar("111", 0, 90)]})
    provider = source(tmp_path, cfg, rest)
    result = provider.load(request(cfg))
    assert len(result.candles["111"]) == 2
    assert rest.calls[0][1]["exchangeSegment"] == "NSE_EQ"
    assert rest.calls[0][1]["instrument"] == "EQUITY"
    assert rest.calls[1][1]["exchangeSegment"] == "NSE_FNO"
    assert rest.calls[1][1]["instrument"] == "OPTSTK"
    assert all(call[1]["toDate"] == "2026-09-16 00:00:00" for call in rest.calls)
    assert all(call[0] == "/v2/charts/intraday" for call in rest.calls)
    assert result.metadata["download_requests"] == 2
    assert any("WINDOW ANCHOR" in w for w in result.warnings)
    assert any("CURRENT-MASTER" in w for w in result.warnings)
    cached = source(tmp_path, cfg, Rest()).load(request(cfg))
    assert cached.metadata["cache_hits"] == 2
    assert cached.metadata["download_requests"] == 0
    assert cached.candles == result.candles


def test_index_history_uses_idx_i_and_optidx(tmp_path):
    cfg = config(tmp_path)
    c = OptionContract("111", "NIFTY 100 CE", "NIFTY", 100, "CE", "2026-09-29", 75, "OPTIDX")
    rest = Rest({"1000": [bar("1000", 0, 100)], "111": [bar("111", 0, 100)]})
    provider = source(tmp_path, cfg, rest, c)
    provider.load(request(cfg, symbols=["NIFTY"]))
    assert rest.calls[0][1]["exchangeSegment"] == "IDX_I"
    assert rest.calls[0][1]["instrument"] == "INDEX"
    assert rest.calls[1][1]["instrument"] == "OPTIDX"


def test_history_requests_chunk_at_90_days(tmp_path):
    rest = Rest()
    provider = source(tmp_path, rest=rest)
    provider._history({"security_id": "1", "segment": "NSE_FNO", "instrument": "OPTSTK"},
                      date(2026, 1, 1), date(2026, 8, 1), threading.Event())
    assert len(rest.calls) == 3
    for _, payload, _ in rest.calls:
        start = datetime.fromisoformat(payload["fromDate"])
        end = datetime.fromisoformat(payload["toDate"])
        assert 0 < (end - start).days <= 90


def test_cache_identity_includes_contract_expiry(tmp_path):
    rest = Rest({"111": [bar("111", 0, 100)]})
    provider = source(tmp_path, rest=rest)
    identity = {"security_id": "111", "segment": "NSE_FNO", "instrument": "OPTSTK", "expiry": "2026-09-29"}
    provider._history(identity, date(2026, 9, 15), date(2026, 9, 16), threading.Event())
    identity["expiry"] = "2026-10-27"
    provider._history(identity, date(2026, 9, 15), date(2026, 9, 16), threading.Event())
    assert len(rest.calls) == 2


def test_current_day_candles_must_be_complete_and_are_not_cached(tmp_path):
    rest = Rest({"111": [bar("111", 0, 100), bar("111", 1, 90)]})
    now = datetime(2026, 9, 15, 9, 31, tzinfo=IST)
    provider = source(tmp_path, rest=rest, now=now)
    identity = {"security_id": "111", "segment": "NSE_FNO", "instrument": "OPTSTK"}
    bars = provider._history(identity, date(2026, 9, 15), date(2026, 9, 16), threading.Event())
    assert len(bars) == 1 and bars[0].close == 100
    assert not list((tmp_path / "cache").glob("candles/*.json"))


def test_missing_monthly_expiry_is_refused_before_history_fetch(tmp_path):
    cfg = config(tmp_path)
    provider = source(tmp_path, cfg)
    with pytest.raises(BacktestError, match="no contract metadata"):
        provider.load(request(cfg, start="2026-08-10", end="2026-08-11", history_start="2026-08-01"))
    assert not provider.rest.calls


def test_missing_weekly_history_is_not_replaced_by_future_weeklies(tmp_path):
    cfg = config(tmp_path)
    cfg["market_data"]["weekly_expiries"] = {"NIFTY": 2}
    cs = [OptionContract(str(i), "NIFTY", "NIFTY", 100, "CE", exp, 75, "OPTIDX")
          for i, exp in enumerate(("2026-09-22", "2026-09-29"))]
    universe = ReplayUniverse(cs, cfg, ["NIFTY"])
    with pytest.raises(BacktestError, match="weekly expiry coverage"):
        universe.expiries("NIFTY", date(2026, 9, 1))
    assert universe.scanner("NIFTY", date(2026, 9, 18), 100) == {"0", "1"}


def test_monthly_24th_switch_and_per_expiry_atm(tmp_path):
    cfg = config(tmp_path)
    cs = [OptionContract(str(i), "RELIANCE", "RELIANCE", strike, "CE", exp, 25)
          for i, (exp, strike) in enumerate((("2026-09-29", 100), ("2026-10-27", 110)))]
    universe = ReplayUniverse(cs, cfg, ["RELIANCE"])
    assert universe.scanner("RELIANCE", date(2026, 9, 23), 105) == {"0"}
    assert universe.scanner("RELIANCE", date(2026, 9, 24), 105) == {"1"}
    missing_next = ReplayUniverse(cs[:1], cfg, ["RELIANCE"])
    with pytest.raises(BacktestError):
        missing_next.scanner("RELIANCE", date(2026, 9, 24), 105)


def test_empty_selected_contract_history_fails_instead_of_partial_results(tmp_path):
    provider = source(tmp_path, rest=Rest({"1000": [bar("1000", 0, 100)]}))
    with pytest.raises(BacktestError, match="No fixed-contract history"):
        provider.load(request(config(tmp_path)))


def test_cancel_before_download(tmp_path):
    provider = source(tmp_path)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(BacktestCancelled):
        provider.load(request(config(tmp_path)), cancel=cancel)
    assert not provider.rest.calls


def test_bad_dhan_arrays_fail_loudly_and_first_trade_timestamp_snaps():
    data = columnar([bar("111", 0, 100)])
    data["timestamp"][0] += 120
    assert parse_history("111", data)[0].ts == bar("111", 0, 100).ts
    data["volume"] = []
    with pytest.raises(BacktestError, match="inconsistent volume"):
        parse_history("111", data)
    with pytest.raises(BacktestError):
        parse_history("111", {"status": "failure", "data": {}})


def test_no_credentials_never_means_demo(tmp_path):
    with pytest.raises(BacktestError, match="No synthetic fallback"):
        DhanHistorySource(config(tmp_path), tmp_path / "cache")


def test_auth_error_is_actionable_and_does_not_leak_response(tmp_path):
    class Forbidden(Rest):
        def post(self, *a, **k):
            raise DhanAPIError("SECRET RESPONSE", status=401)
    provider = source(tmp_path, rest=Forbidden())
    with pytest.raises(BacktestError) as error:
        provider.load(request(config(tmp_path)))
    assert "token" in str(error.value) and "SECRET RESPONSE" not in str(error.value)


@pytest.mark.parametrize("changes", [
    {"symbols": ["WIPRO"]}, {"symbols": []}, {"symbols": "RELIANCE"},
    {"source": "unknown"}, {"initial_capital": 0}, {"initial_capital": float("nan")},
    {"initial_capital": True}, {"slippage_bps": -1}, {"cost_bps": float("inf")},
    {"slippage_bps": 1001}, {"close_at_end": "false"}, {"fill_mode": "next_open"},
    {"start": "nonsense"}, {"end": "2026-09-20"}, {"history_start": "2026-09-16"},
    {"risk": {"max_open_positions": 999}},
])
def test_request_validation(tmp_path, changes):
    with pytest.raises(BacktestError):
        request(config(tmp_path), **changes)
