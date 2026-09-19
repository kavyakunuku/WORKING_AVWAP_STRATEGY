"""Chronological replay of the production SignalEngine + PaperBroker + risk gate.

Warm-up accumulates AVWAP but cannot trade. Every later option candle advances
its own state, including while outside the scanner. Only historical scanner
members may enter; existing positions can exit regardless of scanner membership.
All marks at a timestamp are updated before deterministic security-ID ordering.
"""
from __future__ import annotations

import copy
import threading
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

from backtest.data import check_cancel
from backtest.models import BacktestError, validated_bars
from backtest.universe import ReplayUniverse
from common.models import ENTRY_SELL
from common.utils import epoch, from_epoch
from execution.paper import PaperBroker
from portfolio.positions import PositionManager
from portfolio.risk import RiskManager
from storage.database import Database
from storage.journal import Journal
from strategy.avwap import AvwapStore
from strategy.engine import SignalEngine


class BacktestEngine:
    def __init__(self, cfg: dict):
        self.cfg = copy.deepcopy(cfg)
        if int(cfg.get("strategy", {}).get("candle_interval_minutes", 15)) != 15:
            raise BacktestError("This strategy backtester requires 15-minute candles")

    def run(self, request, data, db_path, *, progress=None, cancel=None) -> dict:
        self.request = request
        self.data = data
        self.cancel = cancel or threading.Event()
        self.progress = progress or (lambda *args: None)
        path = Path(db_path)
        trading_db = Path(self.cfg.get("storage", {}).get("db_path", "data/trader.db"))
        if path.resolve() == trading_db.resolve() or (path.exists() and path.stat().st_size):
            raise BacktestError("Replay requires a new, isolated database, never the trading database")
        self.universe = ReplayUniverse(data.contracts, self.cfg, request.symbols)
        self.db = Database(str(path))
        try:
            self.journal = Journal(self.db)
            self.positions = PositionManager(self.db)
            self.avwap = AvwapStore(self.db)
            self.risk = RiskManager(self.db, self.cfg)
            self.broker = PaperBroker(self.db, self.positions, self.journal,
                                      fill_mode="candle_close", slippage_bps=request.slippage_bps)
            self.clock = from_epoch(request.start_ts)
            self.engine = SignalEngine(self.db, self.avwap, self.positions, self.journal,
                                        on_signal=self._on_signal, now_fn=lambda: self.clock)
            self.engine.register_contracts(list(self.universe.contracts.values()))
            self.scanner = set()
            self.marks = {}
            self.mark_ts = {}
            self.fees = defaultdict(float)
            self.day_fees = defaultdict(float)
            self.fees_total = 0.0
            self.realized = 0.0
            self.blocked = []
            self.callback_error = None
            self.warnings = list(data.warnings)
            self.curve = [{"ts": request.start_ts, "equity": request.initial_capital,
                           "net_pnl": 0.0, "realized_pnl": 0.0, "unrealized_pnl": 0.0,
                           "costs": 0.0, "open_positions": 0}]
            return self._replay()
        finally:
            self.db.close()

    def _open(self):
        return [dict(p) for p in self.db.get_positions(status="OPEN", limit=1_000_000)]

    def _charge(self, position_id, price, quantity):
        fee = self.request.fee_per_order + price * quantity * self.request.cost_bps / 10_000
        self.fees[position_id] += fee
        self.fees_total += fee
        self.day_fees[self.clock.date().isoformat()] += fee

    def _on_signal(self, signal):
        # SignalEngine catches callback failures in production. A backtest
        # must instead fail loudly rather than return a deceptively good run.
        try:
            if signal.action == ENTRY_SELL:
                if signal.security_id not in self.scanner:
                    return
                day = self.clock.date().isoformat()
                positions = self._open()
                pnl = self.risk.daily_pnl(day, positions, self.marks.get) - self.day_fees[day]
                reasons = self.risk.entry_block_reasons(
                    len(positions), self.db.trades_today_count(day), pnl)
                if reasons:
                    self.blocked.append({"ts": epoch(self.clock), "security_id": signal.security_id,
                                         "symbol": signal.symbol, "reasons": reasons})
                    self.journal.write("ENTRY_BLOCKED", ts=epoch(self.clock),
                                       security_id=signal.security_id, detail={"reasons": reasons})
                    return
                contract = self.universe.contracts[signal.security_id]
                qty = self.risk.quantity_for(contract)
                fill = self.broker.execute_entry(contract, qty, signal)
                if not fill.is_filled:
                    raise BacktestError("Paper replay entry failed")
                pos = self.positions.find_by_security(signal.security_id)
                # Shared paper code stores candle-start times. Replay reports
                # the actual completed-candle FILL time; signal.candle_ts stays
                # the candle START, so no 15-minute look-ahead is implied.
                self.positions.update(pos["position_id"], entry_time=epoch(self.clock))
                self.db.update_order(fill.order_id, placed_at=epoch(self.clock), updated_at=epoch(self.clock))
                self._charge(pos["position_id"], fill.avg_price, qty)
            else:
                pos = self.positions.find_by_security(signal.security_id)
                if pos:
                    self._exit(pos, signal=signal)
        except Exception as e:
            self.callback_error = e

    def _exit(self, pos, signal=None, price=None, reason="CLOSE_ABOVE_AVWAP"):
        fill = self.broker.execute_exit(pos, signal, fill_price=price, reason=reason)
        if not fill.is_filled:
            raise BacktestError("Paper replay exit failed")
        self.positions.update(pos["position_id"], exit_time=epoch(self.clock))
        self._charge(pos["position_id"], fill.avg_price, pos["quantity"])
        self.realized += (pos["entry_price"] - fill.avg_price) * pos["quantity"]
        self.db.save_order({"order_id": fill.order_id, "position_id": pos["position_id"],
                            "security_id": pos["security_id"], "action": "EXIT_BUY", "side": "BUY",
                            "quantity": pos["quantity"], "order_type": "BACKTEST", "status": "COMPLETE",
                            "filled_qty": pos["quantity"], "avg_price": fill.avg_price,
                            "placed_at": epoch(self.clock), "updated_at": epoch(self.clock),
                            "raw": {"reason": reason, "fill_mode": "candle_close"}})

    def _point(self):
        opened = self._open()
        unrealized = sum((p["entry_price"] - self.marks[p["security_id"]]) * p["quantity"] for p in opened)
        net = self.realized + unrealized - self.fees_total
        return {"ts": epoch(self.clock), "equity": self.request.initial_capital + net,
                "net_pnl": net, "realized_pnl": self.realized, "unrealized_pnl": unrealized,
                "costs": self.fees_total, "open_positions": len(opened)}

    def _replay(self):
        req = self.request
        events = defaultdict(dict)
        spots = defaultdict(dict)
        warmup_counts = {}
        option_count = 0
        for sid, raw in self.data.candles.items():
            check_cancel(self.cancel)
            if sid not in self.universe.contracts:
                raise BacktestError(f"Unknown option contract in history: {sid}")
            c = self.universe.contracts[sid]
            bars = [b for b in validated_bars(raw, sid)
                    if req.history_ts <= b.ts and b.ts + 900 <= req.end_ts]
            if any(from_epoch(b.ts).date().isoformat() > c.expiry for b in bars):
                raise BacktestError(f"History contains post-expiry candles for {c.name}")
            warm = [b for b in bars if b.ts < req.start_ts]
            warmup_counts[sid] = len(warm)
            if warm:
                self.avwap.initialize_from_candles(sid, warm, symbol=c.name)
            for bar in bars:
                if bar.ts >= req.start_ts:
                    events[bar.ts][sid] = bar
                    option_count += 1
        for u in req.symbols:
            check_cancel(self.cancel)
            raw = self.data.spots.get(u, [])
            bars = [b for b in validated_bars(raw) if req.start_ts <= b.ts and b.ts + 900 <= req.end_ts]
            if not bars:
                raise BacktestError(f"No underlying candles for requested symbol {u}")
            for bar in bars:
                spots[bar.ts][u] = bar.close
        times = sorted(set(events) | set(spots))
        if not times or not option_count:
            raise BacktestError("No option candles in the requested trading range")
        missing_options, missing_spots = Counter(), Counter()
        stale_marks = 0
        for i, ts in enumerate(times):
            check_cancel(self.cancel)
            self.clock = from_epoch(ts + 900)
            bars = events.get(ts, {})
            for sid, bar in bars.items():
                self.marks[sid] = bar.close
                self.mark_ts[sid] = ts
            self.scanner = set()
            for u in req.symbols:
                if u in spots.get(ts, {}):
                    self.scanner.update(self.universe.scanner(u, from_epoch(ts).date(), spots[ts][u]))
                else:
                    missing_spots[u] += 1
            # A selected contract with no supplied series is a hard data error,
            # not an excuse to silently shrink the scanner.
            unavailable = self.scanner - set(self.data.candles)
            if unavailable:
                raise BacktestError("Missing option history for selected contract(s): " + ", ".join(sorted(unavailable)[:8]))
            monitored = self.scanner | {p["security_id"] for p in self._open()}
            missing_options.update(monitored - set(bars))
            for sid in sorted(bars):
                bar = bars[sid]
                if sid in self.scanner or self.positions.has_open(sid):
                    self.engine.on_candle(bar)
                    if self.callback_error:
                        raise self.callback_error
                else:
                    state = self.avwap.get(sid)
                    state.update(bar)
                    self.avwap.save(state)
            stale_marks += sum(self.mark_ts.get(p["security_id"]) != ts for p in self._open())
            self.curve.append(self._point())
            if i % 25 == 0 or i == len(times) - 1:
                self.progress("replay", f"Replaying {self.clock.strftime('%Y-%m-%d %H:%M')} IST", i + 1, len(times))
        if req.close_at_end:
            for pos in self._open():
                check_cancel(self.cancel)
                sid = pos["security_id"]
                if self.mark_ts[sid] == times[-1]:
                    self._exit(pos, price=self.marks[sid], reason="BACKTEST_END")
                else:
                    self.warnings.append(f"End liquidation skipped for {sid}: no candle at the final replay timestamp (no stale-price fill invented).")
            self.curve[-1] = self._point()
        if missing_options:
            self.warnings.append("Missing monitored option bars were not synthesized or filled forward. They may be illiquidity or data gaps; AVWAP/exit coverage can be incomplete. See coverage counts.")
        if missing_spots:
            self.warnings.append("Missing underlying bars disable new entries for that underlying at those timestamps; existing positions remain exit-monitored.")
        no_warmup = [sid for sid, n in warmup_counts.items() if n == 0]
        if no_warmup:
            self.warnings.append(f"{len(no_warmup)} contract(s) have no pre-start warm-up candles. Their AVWAP begins at the first supplied candle; no entry is manufactured on the first candle.")
        expired_open = [p["security_id"] for p in self._open() if p["expiry"] <= str(req.end)]
        if expired_open:
            self.warnings.append("UNRESOLVED EXPIRY: open positions reached expiry. Cash/physical settlement is not modeled; their last marks are provisional, not settlement P&L.")
        if stale_marks:
            self.warnings.append("Some equity points use a position's last known close because its current candle is missing; stale marks are counted in coverage.")
        days_seen = {from_epoch(t).date() for t in spots}
        days_without_data = []
        day = req.start
        while day <= req.end:
            if day.weekday() < 5 and day not in days_seen:
                days_without_data.append(str(day))
            day += timedelta(days=1)
        if days_without_data:
            self.warnings.append("Some weekdays have no underlying data (possible exchange holidays or missing data); see coverage. No sessions were fabricated.")
        coverage = {"option_candles": option_count, "replay_windows": len(times),
                    "contracts": len(self.data.candles), "warmup_candles": warmup_counts,
                    "missing_option_windows": dict(missing_options), "missing_spot_windows": dict(missing_spots),
                    "stale_position_marks": stale_marks, "expired_open_positions": expired_open,
                    "weekdays_without_data": days_without_data,
                    "first_candle_ts": times[0], "last_candle_ts": times[-1]}
        check_cancel(self.cancel)
        return self._report(coverage)

    def _report(self, coverage):
        positions = sorted((dict(p) for p in self.db.get_positions(limit=1_000_000)),
                           key=lambda p: (p["entry_time"], p["security_id"]))
        for p in positions:
            sid = p["security_id"]
            p["costs"] = self.fees[p["position_id"]]
            p["gross_pnl"] = p["pnl"]
            p["net_pnl"] = p["pnl"] - p["costs"] if p["pnl"] is not None else None
            p["last_mark"] = self.marks.get(sid)
            p["last_mark_ts"] = self.mark_ts.get(sid)
            p["unrealized_pnl"] = ((p["entry_price"] - self.marks[sid]) * p["quantity"]
                                     if p["status"] == "OPEN" else 0.0)
        closed = [p for p in positions if p["status"] == "CLOSED"]
        wins = [p["net_pnl"] for p in closed if p["net_pnl"] > 0]
        losses = [p["net_pnl"] for p in closed if p["net_pnl"] < 0]
        peak = self.request.initial_capital
        max_dd, max_dd_pct = 0.0, 0.0
        daily = {}
        for point in self.curve:
            peak = max(peak, point["equity"])
            point["drawdown"] = peak - point["equity"]
            point["drawdown_pct"] = point["drawdown"] / peak * 100
            max_dd = max(max_dd, point["drawdown"])
            max_dd_pct = max(max_dd_pct, point["drawdown_pct"])
            if point is not self.curve[0]:
                daily[from_epoch(point["ts"]).date().isoformat()] = point
        daily_rows = []
        previous = 0.0
        for day, point in sorted(daily.items()):
            daily_rows.append({"date": day, "pnl": point["net_pnl"] - previous,
                               "cumulative_pnl": point["net_pnl"], "equity": point["equity"]})
            previous = point["net_pnl"]
        last = self.curve[-1]
        return {
            "mode": "BACKTEST", "request": self.request.to_dict(), "data": self.data.metadata,
            "settings": {"strategy": copy.deepcopy(self.cfg.get("strategy", {})),
                         "risk": copy.deepcopy(self.cfg.get("risk", {})),
                         "weekly_expiries": copy.deepcopy(self.cfg.get("market_data", {}).get("weekly_expiries", {}))},
            "assumptions": [
                "Idealized signal-candle-close fills; all signal/position fill times are candle CLOSE times in IST (candle_ts remains the START). Not executable-price guarantees.",
                "Historical ATM uses the completed underlying bar's close. No future quotes or live option chains are consulted during replay.",
                "Warm-up cannot trade. Option AVWAP continues even outside the scanner; only scanner contracts may enter, open positions always receive exit checks.",
                "Same-timestamp competition is resolved by ascending security ID after marking all available closes. Fees count against daily-loss entry gates.",
                "No margin, market impact, bid/ask, taxes beyond configured costs, or expiry settlement model. Capital is a reporting baseline, not a broker margin limit.",
                "Open trades are marked, not silently liquidated. Optional BACKTEST_END closes are separately identified and require a current final candle.",
            ],
            "warnings": list(dict.fromkeys(self.warnings)), "coverage": coverage,
            "metrics": {
                "total_trades": len(positions), "closed_trades": len(closed),
                "open_trades": len(positions) - len(closed), "winning_trades": len(wins),
                "losing_trades": len(losses), "win_rate_pct": len(wins) / len(closed) * 100 if closed else 0.0,
                "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
                "gross_realized_pnl": self.realized, "unrealized_pnl": last["unrealized_pnl"],
                "total_costs": self.fees_total, "net_pnl": last["net_pnl"],
                "final_equity": last["equity"], "return_pct": last["net_pnl"] / self.request.initial_capital * 100,
                "max_drawdown": max_dd, "max_drawdown_pct": max_dd_pct,
                "blocked_entries": len(self.blocked),
            },
            "trades": positions, "equity_curve": self.curve, "daily_pnl": daily_rows,
            "signals": [dict(r) for r in self.db._query("SELECT * FROM signals ORDER BY candle_ts, security_id")],
            "blocked_entries": self.blocked,
            "contracts": [asdict(c) for c in self.data.contracts if c.security_id in self.data.candles],
        }
