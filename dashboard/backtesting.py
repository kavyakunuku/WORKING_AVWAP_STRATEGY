"""Backtest HTTP surface, reusable without starting the trading engine."""
from __future__ import annotations

from pathlib import Path

from flask import Blueprint, Flask, jsonify, request, send_file

from backtest.models import BacktestError
from backtest.service import BacktestBusy, BacktestService

PANEL_HTML = (Path(__file__).parent / "templates" / "backtest_panel.html").read_text(encoding="utf-8")


def register_backtests(flask, cfg, control_token="", service=None):
    service = service or BacktestService(cfg)
    flask.extensions["backtests"] = service
    bp = Blueprint("backtests", __name__, static_folder="static", static_url_path="/backtesting/assets")

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise BacktestError("Send a JSON object")
        data = dict(data)
        token = request.headers.get("X-Control-Token", "") or data.pop("token", "")
        data.pop("token", None)
        if control_token and token != control_token:
            from flask import abort
            abort(403)
        return data

    @bp.errorhandler(403)
    def forbidden(error):
        return jsonify(ok=False, message="Bad control token"), 403

    @bp.errorhandler(BacktestBusy)
    def busy(error):
        return jsonify(ok=False, message=str(error)), 409

    @bp.errorhandler(BacktestError)
    def bad_request(error):
        return jsonify(ok=False, message=str(error)), 400

    @bp.errorhandler(KeyError)
    def not_found(error):
        return jsonify(ok=False, message="Unknown backtest or export"), 404

    @bp.get("/backtesting")
    def page():
        return ("<!doctype html><html><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>AVWAP Backtesting</title>"
                "<link rel='stylesheet' href='/backtesting/assets/backtest.css'>"
                "</head><body class='bt-standalone'><main>" + PANEL_HTML +
                "</main><script src='/backtesting/assets/backtest.js' defer></script></body></html>")

    @bp.get("/api/backtests/options")
    def options():
        return jsonify({**service.options(), "control_token_required": bool(control_token)})

    @bp.get("/api/backtests")
    def runs():
        return jsonify(runs=service.list_runs())

    @bp.post("/api/backtests")
    def start():
        return jsonify(service.submit(body())), 202

    @bp.get("/api/backtests/<run_id>")
    def status(run_id):
        return jsonify(service.get(run_id))

    @bp.post("/api/backtests/<run_id>/cancel")
    def cancel(run_id):
        body()
        return jsonify(service.cancel(run_id)), 202

    @bp.get("/api/backtests/<run_id>/result")
    def result(run_id):
        return jsonify(service.result(run_id))

    @bp.get("/api/backtests/<run_id>/export/<filename>")
    def export(run_id, filename):
        return send_file(service.export_path(run_id, filename), as_attachment=True,
                         download_name=f"backtest-{run_id[:8]}-{filename}")

    flask.register_blueprint(bp)
    return service


def create_backtest_dashboard(cfg):
    """No TraderApp, feed loop, broker connection, or live-mode confirmation."""
    flask = Flask(__name__)
    register_backtests(flask, cfg, cfg.get("dashboard", {}).get("control_token", ""))

    @flask.get("/")
    def index():
        from flask import redirect
        return redirect("/backtesting")

    return flask
