"""Explicit, deterministic synthetic demonstration. NEVER a Dhan fallback."""
from __future__ import annotations

import math
import threading
import zlib
from datetime import date, datetime, timedelta

from backtest.data import check_cancel
from backtest.models import BacktestDataset, BacktestError
from backtest.universe import ReplayUniverse
from common.models import Candle, OptionContract
from common.scanner import APPROVED_INDICES
from common.utils import IST, all_candle_starts, epoch, from_epoch


class DemoHistorySource:
    def __init__(self, cfg):
        self.cfg = cfg

    def load(self, request, progress=None, cancel=None):
        progress = progress or (lambda *args: None)
        cancel = cancel or threading.Event()
        progress("demo", "Generating clearly labelled synthetic data (no Dhan calls)", 0, 0)
        dates = []
        day = request.history_start
        while day <= request.end:
            if day.weekday() < 5:
                dates.extend(epoch(t) for t in all_candle_starts(datetime.combine(day, datetime.min.time(), IST)))
            day += timedelta(days=1)
        if not dates:
            raise BacktestError("Demo range has no weekday sessions")
        expiries = set()
        day = request.history_start.replace(day=1)
        while day <= request.end + timedelta(days=90):
            if day.weekday() == 1:  # synthetic Tuesday calendar, not exchange data
                expiries.add(day)
            day += timedelta(days=1)
        weekly = self.cfg.get("market_data", {}).get("weekly_expiries", {})
        monthly = {max(e for e in expiries if (e.year, e.month) == ym)
                   for ym in {(e.year, e.month) for e in expiries}}
        width = int(self.cfg.get("strategy", {}).get("itm_strikes_per_side", 6))
        contracts, spots, spot_values = [], {}, {}
        for u in request.symbols:
            check_cancel(cancel)
            seed = zlib.crc32(u.encode())
            index = u in APPROVED_INDICES
            base = (23000 if u == "NIFTY" else 51000) if index else 1000 + (seed % 40) * 50
            step = 100 if index else 20
            lot = (75 if u == "NIFTY" else 30) if index else 250
            values = {ts: base + math.sin(ts / 900 / 8 + seed % 11) * step * 1.6 for ts in dates}
            spot_values[u] = values
            spots[u] = [Candle("SPOT-" + u, ts, v, v + step / 8, v - step / 8, v, 1000)
                        for ts, v in values.items() if ts >= request.start_ts]
            for exp in sorted(expiries if weekly.get(u, 0) else monthly):
                for offset in range(-width - 3, width + 4):
                    strike = base + offset * step
                    for side in ("CE", "PE"):
                        sid = f"DEMO-{u}-{exp}-{strike}-{side}"
                        contracts.append(OptionContract(sid, f"{u} {strike} {side}", u, strike,
                                                        side, str(exp), lot, "OPTIDX" if index else "OPTSTK"))
        universe = ReplayUniverse(contracts, self.cfg, request.symbols)
        needed = set()
        for u, bars in spots.items():
            for bar in bars:
                needed.update(universe.scanner(u, from_epoch(bar.ts).date(), bar.close))
        if len(needed) * len(dates) > 500_000:
            raise BacktestError("Demo would exceed 500,000 synthetic candles; choose fewer symbols or days")
        candles = {}
        for i, sid in enumerate(sorted(needed)):
            check_cancel(cancel)
            c = universe.contracts[sid]
            phase = zlib.crc32(sid.encode()) % 17
            bars = []
            for ts in dates:
                if from_epoch(ts).date().isoformat() > c.expiry:
                    continue
                spot = spot_values[c.underlying][ts]
                intrinsic = max(0, spot - c.strike) if c.option_type == "CE" else max(0, c.strike - spot)
                price = 45 + intrinsic * .5 + 18 * math.sin(ts / 900 / 3 + phase)
                bars.append(Candle(sid, ts, price + 1, price + 3, price - 3, price, 500 + phase * 20))
            candles[sid] = bars
            progress("demo", f"Synthetic contract {i + 1}/{len(needed)}", i + 1, len(needed))
        return BacktestDataset(contracts, candles, spots,
                               metadata={"source": "SYNTHETIC_DEMO", "lifetime_history_verified": False,
                                         "history_start": str(request.history_start)},
                               warnings=["SYNTHETIC DEMO — generated prices, lots and expiry dates, not Dhan data. These results are not evidence of market performance."])
