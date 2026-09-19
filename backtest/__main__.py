"""python -m backtest: a replay runner or a standalone research dashboard.

Neither path constructs TraderApp or DhanBroker, even if config says LIVE.
"""
from __future__ import annotations

import argparse
import json
import logging

from backtest.models import BacktestError
from backtest.service import BacktestService
from common.config import load_config


def main(argv=None):
    parser = argparse.ArgumentParser(description="AVWAP backtesting (no live orders)")
    parser.add_argument("--config", default="config/config.json")
    parser.add_argument("--start", help="first trading date, YYYY-MM-DD (IST)")
    parser.add_argument("--end", help="last trading date, inclusive, YYYY-MM-DD")
    parser.add_argument("--history-start", help="AVWAP warm-up start; defaults to start month's first day")
    parser.add_argument("--symbols", nargs="+", help="subset of the approved configured scanner")
    parser.add_argument("--initial-capital", type=float)
    parser.add_argument("--slippage-bps", type=float)
    parser.add_argument("--fee-per-order", type=float)
    parser.add_argument("--cost-bps", type=float)
    parser.add_argument("--close-at-end", action="store_true")
    parser.add_argument("--demo", action="store_true", help="explicit SYNTHETIC data, never a Dhan fallback")
    parser.add_argument("--output-dir", help="isolated backtest storage, default data/backtests")
    parser.add_argument("--serve", action="store_true", help="standalone dashboard WITHOUT a trading engine")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    if args.output_dir:
        cfg.setdefault("backtest", {})["output_dir"] = args.output_dir
    logging.basicConfig(level=logging.WARNING)
    if args.serve:
        from dashboard.backtesting import create_backtest_dashboard
        create_backtest_dashboard(cfg).run(host=args.host, port=args.port, debug=False, use_reloader=False)
        return 0
    if not args.start or not args.end:
        parser.error("--start and --end are required unless --serve is used")
    payload = {key: value for key, value in vars(args).items() if value is not None and key in
               ("start", "end", "history_start", "symbols", "initial_capital", "slippage_bps",
                "fee_per_order", "cost_bps", "close_at_end")}
    payload["source"] = "demo" if args.demo else "dhan"
    try:
        service = BacktestService(cfg)
        state = service.run_sync(payload)
    except BacktestError as e:
        print(f"Backtest refused: {e}")
        return 2
    print(f"Backtest {state['id']}: {state['status']}")
    if state["status"] != "completed":
        print(state["detail"])
        return 130 if state["status"] == "cancelled" else 2
    result = service.result(state["id"])
    print(json.dumps(result["metrics"], indent=2, allow_nan=False))
    for warning in result["warnings"]:
        print("WARNING:", warning)
    print(f"Results: {service.root / 'runs' / state['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
