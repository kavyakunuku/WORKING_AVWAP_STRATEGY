"""Background jobs, HTTP controls/exports, recovery and CLI are isolated."""
import json
import threading

import pytest
from flask import Flask

from backtest.data import atomic_json, check_cancel
from backtest.models import BacktestError
from backtest.service import BacktestBusy, BacktestService
from dashboard.backtesting import create_backtest_dashboard, register_backtests
from test_backtest_engine import config, dataset


def payload(**extra):
    return {"start": "2026-09-15", "end": "2026-09-15", "history_start": "2026-09-14",
            "symbols": ["RELIANCE"], **extra}


class FixtureSource:
    closed = False

    def load(self, request, progress, cancel):
        progress("fixture", "Using fixture", 1, 1)
        return dataset()

    def close(self):
        self.closed = True


def test_completed_result_exports_survive_service_restart_without_credentials(tmp_path):
    cfg = config(tmp_path)
    cfg["dhan"].update(client_id="PRIVATE_CLIENT", access_token="PRIVATE_TOKEN")
    src = FixtureSource()
    service = BacktestService(cfg, source_factory=lambda req: src)
    run = service.run_sync(payload())
    assert run["status"] == "completed" and src.closed
    assert run["summary"]["closed_trades"] == 1
    restarted = BacktestService(cfg)
    assert restarted.get(run["id"])["status"] == "completed"
    assert restarted.result(run["id"])["metrics"]["net_pnl"] == -750
    assert restarted.list_runs()[0]["id"] == run["id"]
    for filename in ("result.json", "trades.csv", "equity.csv", "daily.csv", "signals.csv"):
        text = restarted.export_path(run["id"], filename).read_text(encoding="utf-8-sig")
        assert "PRIVATE_TOKEN" not in text and "PRIVATE_CLIENT" not in text
    assert "entry_time_ist" in restarted.export_path(run["id"], "trades.csv").read_text()
    assert not (tmp_path / "trading.db").exists()


def test_http_validation_token_gate_background_result_and_exports(tmp_path):
    cfg = config(tmp_path)
    service = BacktestService(cfg, source_factory=lambda req: FixtureSource())
    flask = Flask(__name__)
    register_backtests(flask, cfg, "test-token", service)
    client = flask.test_client()
    options = client.get("/api/backtests/options").get_json()
    assert options["control_token_required"] is True
    assert options["fill_mode"] == "candle_close"
    assert options["symbols"] == ["RELIANCE", "NIFTY", "BANKNIFTY"]
    assert client.post("/api/backtests", json=payload()).status_code == 403
    headers = {"X-Control-Token": "test-token"}
    assert client.post("/api/backtests", json=payload(symbols=["WIPRO"]), headers=headers).status_code == 400
    assert client.post("/api/backtests", json=[], headers=headers).status_code == 400
    response = client.post("/api/backtests", json=payload(), headers=headers)
    assert response.status_code == 202
    run_id = response.get_json()["id"]
    service._thread.join(timeout=5)
    assert not service._thread.is_alive()
    assert client.get(f"/api/backtests/{run_id}").get_json()["status"] == "completed"
    assert client.get(f"/api/backtests/{run_id}/result").get_json()["metrics"]["net_pnl"] == -750
    export = client.get(f"/api/backtests/{run_id}/export/trades.csv")
    assert export.status_code == 200
    assert "attachment" in export.headers["Content-Disposition"]
    assert client.get(f"/api/backtests/{run_id}/export/replay.db").status_code == 404
    assert client.get("/api/backtests/not-a-run").status_code == 404
    assert client.get("/backtesting").status_code == 200
    assert client.get("/backtesting/assets/backtest.js").status_code == 200
    assert client.get("/backtesting/assets/backtest.css").status_code == 200


def test_one_worker_cancel_and_no_incomplete_result(tmp_path):
    entered = threading.Event()
    class WaitingSource(FixtureSource):
        def load(self, request, progress, cancel):
            entered.set()
            cancel.wait(5)
            check_cancel(cancel)
            return dataset()
    service = BacktestService(config(tmp_path), source_factory=lambda req: WaitingSource())
    run = service.submit(payload())
    assert entered.wait(2)
    with pytest.raises(BacktestBusy):
        service.submit(payload())
    service.cancel(run["id"])
    service._thread.join(5)
    assert service.get(run["id"])["status"] == "cancelled"
    with pytest.raises(BacktestError, match="completed result"):
        service.result(run["id"])
    with pytest.raises(BacktestError, match="completed runs"):
        service.export_path(run["id"], "trades.csv")
    with pytest.raises(BacktestError, match="not active"):
        service.cancel(run["id"])


def test_missing_credentials_is_failed_dhan_not_a_synthetic_fallback(tmp_path):
    service = BacktestService(config(tmp_path))
    run = service.run_sync(payload())
    assert run["status"] == "failed"
    assert "credentials" in run["error"]
    assert run["request"]["source"] == "dhan"
    assert not (service._directory(run["id"]) / "result.json").exists()


def test_stopped_worker_is_marked_interrupted_not_complete(tmp_path):
    service = BacktestService(config(tmp_path))
    run_id = "f" * 32
    atomic_json(service._directory(run_id) / "status.json", {
        "id": run_id, "status": "running", "pid": -999999,
        "process_created": 0, "request": payload(),
    })
    assert service.get(run_id)["status"] == "interrupted"
    with pytest.raises(BacktestError):
        service.result(run_id)


def test_run_paths_and_export_names_are_not_user_controlled(tmp_path):
    service = BacktestService(config(tmp_path))
    for name in ("../trader", "/etc/passwd", "a" * 33, "", None):
        with pytest.raises(KeyError):
            service.get(name)
    with pytest.raises(KeyError):
        service.export_path("a" * 32, "../trader.db")


def test_standalone_server_never_constructs_trading_app(tmp_path, monkeypatch):
    import app
    monkeypatch.setattr(app.TraderApp, "__init__", lambda *a, **k: pytest.fail("Trading app constructed"))
    cfg = config(tmp_path)
    cfg["trading_mode"] = "LIVE"
    client = create_backtest_dashboard(cfg).test_client()
    assert client.get("/").status_code == 302
    assert b"Strategy backtesting" in client.get("/backtesting").data
    assert client.get("/api/backtests/options").status_code == 200
    assert not (tmp_path / "trading.db").exists()


def test_cli_explicit_demo_generates_results(tmp_path, capsys):
    from backtest.__main__ import main
    cfg = config(tmp_path)
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(cfg))
    result = main(["--config", str(path), "--demo", "--start", "2026-09-15", "--end", "2026-09-15",
                   "--history-start", "2026-09-14", "--symbols", "RELIANCE", "--close-at-end"])
    assert result == 0
    out = capsys.readouterr().out
    assert "SYNTHETIC DEMO" in out and "Results:" in out
    service = BacktestService(cfg)
    run = service.list_runs()[0]
    assert service.result(run["id"])["data"]["source"] == "SYNTHETIC_DEMO"
    assert not (tmp_path / "trading.db").exists()
