"""The owner-approved universe is a hard ceiling, including legacy configs."""
import copy
import json
from types import SimpleNamespace

from common.config import DEFAULTS, load_config
from common.scanner import APPROVED_INDICES, APPROVED_STOCKS, configured_scanner, scanner_symbols
from common.utils import IST


def test_defaults_and_example_are_the_exact_40_stocks_and_two_indices():
    assert len(APPROVED_STOCKS) == len(set(APPROVED_STOCKS)) == 40
    assert "M&M" in APPROVED_STOCKS and "BSE" in APPROVED_STOCKS and "ETERNAL" in APPROVED_STOCKS
    expected = list(APPROVED_STOCKS) + ["NIFTY", "BANKNIFTY"]
    assert scanner_symbols(DEFAULTS) == expected
    with open("config/config.example.json", encoding="utf-8") as f:
        example = json.load(f)
    assert example["market_data"]["universe_stocks"] == list(APPROVED_STOCKS)
    assert example["market_data"]["universe_indices"] == list(APPROVED_INDICES)


def test_wider_legacy_config_cannot_expand_scanner():
    cfg = {"market_data": {"universe_stocks": ["reliance", " wipro ", "M&M"],
                            "universe_indices": ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]}}
    assert configured_scanner(cfg) == (["RELIANCE", "M&M"], ["NIFTY", "BANKNIFTY"])
    cfg["market_data"]["universe_stocks"] = []
    assert configured_scanner(cfg)[0] == list(APPROVED_STOCKS)


def test_all_excluded_nonempty_config_does_not_reenable_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"market_data": {"universe_stocks": ["WIPRO"], "universe_indices": []}}))
    assert scanner_symbols(load_config(str(path))) == []


def test_bootstrap_applies_ceiling_and_preserves_foreign_position_monitor(tmp_path, monkeypatch):
    from app import TraderApp
    from common.models import OptionContract
    from datetime import datetime
    from dhan.instruments import MasterBundle
    cfg = copy.deepcopy(DEFAULTS)
    cfg["storage"]["db_path"] = str(tmp_path / "app.db")
    cfg["market_data"]["source"] = "mock"
    cfg["market_data"]["universe_stocks"] = ["RELIANCE", "WIPRO"]
    cfg["market_data"]["universe_indices"] = ["NIFTY", "BANKNIFTY", "FINNIFTY"]
    app = TraderApp(cfg)
    symbols = ["RELIANCE", "WIPRO", "NIFTY", "BANKNIFTY", "FINNIFTY"]
    bundle = MasterBundle(contracts=[OptionContract(str(i), u, u, 100, "CE", "2026-09-29", 25,
                                                    "OPTIDX" if "NIFTY" in u else "OPTSTK")
                                     for i, u in enumerate(symbols)])
    monkeypatch.setattr("app.load_master_bundle", lambda *args: bundle)
    visited = []
    monkeypatch.setattr(app, "_bootstrap_underlying_dhan", lambda u, *args: visited.append(u))
    app.universe.add_position_id("LEGACY-WIPRO")
    app._bootstrap_dhan()
    assert set(visited) == {"RELIANCE", "NIFTY", "BANKNIFTY"}
    assert "LEGACY-WIPRO" in app.universe.monitored_ids()
    # Even a queued catch-up entry cannot re-open a contract monitored only
    # for exits. It is blocked before broker execution.
    monkeypatch.setattr(app.broker, "execute_entry", lambda *a: (_ for _ in ()).throw(AssertionError("entry sent")))
    app._handle_entry(SimpleNamespace(security_id="LEGACY-WIPRO", created_at=1, symbol="WIPRO"))
    assert "OUTSIDE_SCANNER" in app.db._query("SELECT detail FROM journal WHERE event='ENTRY_BLOCKED'")[0]["detail"]
    app.db.close()
