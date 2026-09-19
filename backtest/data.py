"""Read-only Dhan fixed-contract history with an isolated, identity-aware cache.

No rollingoption calls: a rolling ATM stream changes contracts and cannot be
fed into this strategy's per-contract AVWAP. Old expiry periods absent from
the master are refused, not replaced with the nearest currently listed series.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import asdict
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

from backtest.models import BacktestCancelled, BacktestDataset, BacktestError, validated_bars
from backtest.universe import ReplayUniverse
from common.models import Candle
from common.scanner import APPROVED_INDICES
from common.utils import IST, candle_start_for, epoch, from_epoch, now_ist, parse_epoch
from dhan.client import DhanAPIError, DhanREST
from dhan.instruments import load_master_bundle


def check_cancel(cancel):
    if cancel.is_set():
        raise BacktestCancelled()


def atomic_json(path: Path, data):
    """Unique temporary name also makes simultaneous cache readers safe."""
    import uuid
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def parse_history(security_id: str, response) -> list[Candle]:
    """Dhan's columnar shape, strictly checked; no truncated/invalid rows lost.

    As in the live parser, a first-trade timestamp (e.g. 09:17) belongs to
    the 09:15 candle. Non-session auction bars are not strategy candles.
    """
    data = response.get("data", response) if isinstance(response, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("timestamp"), list):
        raise BacktestError(f"Dhan returned no valid historical candle arrays for {security_id}")
    count = len(data["timestamp"])
    for key in ("open", "high", "low", "close", "volume"):
        if not isinstance(data.get(key), list) or len(data[key]) != count:
            raise BacktestError(f"Dhan returned inconsistent {key} history for {security_id}")
    bars = []
    for i, raw_ts in enumerate(data["timestamp"]):
        try:
            ts = parse_epoch(raw_ts)
            if ts is None:
                raise ValueError()
            window = candle_start_for(from_epoch(ts))
            if window is None:
                continue
            volume = float(data["volume"][i])
            bars.append(Candle(security_id, epoch(window),
                               *[float(data[key][i]) for key in ("open", "high", "low", "close")],
                               volume=volume))
        except (ValueError, TypeError, OverflowError, OSError) as e:
            raise BacktestError(f"Invalid Dhan candle for {security_id}") from e
    return validated_bars(bars, security_id)


class DhanHistorySource:
    def __init__(self, cfg: dict, cache_dir: Path, *, rest=None, master_loader=None,
                 now_fn=now_ist):
        self.cfg = cfg
        self.cache_dir = Path(cache_dir)
        creds = cfg.get("dhan", {})
        if rest is None and not (creds.get("client_id") and creds.get("access_token")):
            raise BacktestError(
                "Dhan credentials are not configured. Set DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN "
                "on the server or in config/config.json. No synthetic fallback was used."
            )
        self.rest = rest or DhanREST(creds["client_id"], creds["access_token"],
                                    base_url=creds.get("rest_base_url", "https://api.dhan.co"),
                                    timeout=float(creds.get("http_timeout_seconds", 10)))
        self.master_loader = master_loader or load_master_bundle
        self.as_of = now_fn()
        self.gap = max(0.0, float(cfg.get("backtest", {}).get("history_request_gap_seconds", 1)))
        self._last_request = 0.0
        self.requests = 0
        self.cache_hits = 0

    def _history(self, identity: dict, start, end, cancel) -> list[Candle]:
        """[start, end), <=90-day chunks. Cache key includes full option identity
        (expiry / strike / side / underlying), NOT just a reusable security ID.
        Only fully elapsed chunks are cached; a partial current day never is.
        """
        result = []
        cursor = start
        while cursor < end:
            check_cancel(cancel)
            stop = min(cursor + timedelta(days=90), end)
            spec = {"schema": 1, "identity": identity, "from": str(cursor), "to": str(stop), "interval": 15}
            key = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
            path = self.cache_dir / "candles" / (key + ".json")
            cached = None
            if path.exists():
                try:
                    obj = json.loads(path.read_text(encoding="utf-8"))
                    if obj["request"] == spec:
                        cached = validated_bars([Candle(**r) for r in obj["candles"]], identity["security_id"])
                except (OSError, ValueError, KeyError, TypeError):
                    # Corrupt cache is never accepted as history. Re-fetch it.
                    cached = None
            if cached is not None:
                self.cache_hits += 1
                bars = cached
            else:
                if cancel.wait(max(0.0, self.gap - (time.monotonic() - self._last_request))):
                    raise BacktestCancelled()
                try:
                    self.requests += 1
                    response = self.rest.post("/v2/charts/intraday", payload={
                        "securityId": str(identity["security_id"]),
                        "exchangeSegment": identity["segment"],
                        "instrument": identity["instrument"],
                        "interval": "15", "oi": False,
                        "fromDate": f"{cursor} 00:00:00",
                        "toDate": f"{stop} 00:00:00",
                    }, retries=4)
                except DhanAPIError as e:
                    if e.status in (401, 403):
                        raise BacktestError("Dhan history access was refused; check the server's token and data subscription") from e
                    raise BacktestError(f"Dhan history fetch failed for {identity['security_id']} (HTTP {e.status or 'network'}); no partial fallback used") from e
                finally:
                    self._last_request = time.monotonic()
                check_cancel(cancel)
                bars = parse_history(identity["security_id"], response)
                lower = epoch(datetime.combine(cursor, dtime.min, IST))
                upper = min(epoch(datetime.combine(stop, dtime.min, IST)), epoch(self.as_of))
                bars = [b for b in bars if lower <= b.ts and b.ts + 900 <= upper]
                if datetime.combine(stop, dtime.min, IST) <= self.as_of:
                    atomic_json(path, {"request": spec, "candles": [asdict(b) for b in bars]})
            result.extend(bars)
            cursor = stop
        return validated_bars(result, identity["security_id"])

    def load(self, request, progress=None, cancel=None) -> BacktestDataset:
        cancel = cancel or threading.Event()
        progress = progress or (lambda *args: None)
        check_cancel(cancel)
        progress("catalog", "Loading Dhan instrument metadata", 0, 0)
        try:
            bundle = self.master_loader(self.rest, str(self.cache_dir / "instruments"), 12,
                                        int(self.cfg.get("risk", {}).get("default_lot_size", 250)))
        except Exception as e:
            raise BacktestError("Could not load Dhan's instrument master; check connectivity and the server log") from e
        check_cancel(cancel)
        contracts = [c for c in bundle.contracts if c.underlying in request.symbols]
        universe = ReplayUniverse(contracts, self.cfg, request.symbols)
        missing = [u for u in request.symbols if not universe.calendar[u] or not bundle.underlying_ids.get(u)]
        if missing:
            raise BacktestError("Requested symbols unavailable in Dhan's current master: " + ", ".join(missing))
        # Check the requested calendar before making hundreds of candle calls.
        day = request.start
        while day <= request.end:
            if day.weekday() < 5:
                for u in request.symbols:
                    universe.expiries(u, day)
            day += timedelta(days=1)
        spots = {}
        needed = set()
        end_exclusive = request.end + timedelta(days=1)
        for i, u in enumerate(request.symbols):
            check_cancel(cancel)
            progress("spots", f"Historical underlying prices: {u}", i, len(request.symbols))
            index = u in APPROVED_INDICES
            identity = {"security_id": str(bundle.underlying_ids[u]), "underlying": u,
                        "segment": "IDX_I" if index else "NSE_EQ",
                        "instrument": "INDEX" if index else "EQUITY"}
            spots[u] = self._history(identity, request.start, end_exclusive, cancel)
            if not spots[u]:
                raise BacktestError(f"No completed underlying candles for {u} in this period")
            for b in spots[u]:
                needed.update(universe.scanner(u, from_epoch(b.ts).date(), b.close))
        limit = int(self.cfg.get("backtest", {}).get("max_contracts", 2000))
        if len(needed) > limit:
            raise BacktestError(f"Historical ATM movement requires {len(needed)} contracts (limit {limit}); narrow the dates or symbols")
        if not needed:
            raise BacktestError("No historical scanner contracts could be selected")
        candles = {}
        warnings = [
            "CURRENT-MASTER UNIVERSE: strike grids, lot sizes and expiry availability come from today's Dhan master, not a point-in-time chain archive. Historical listing/lot changes and survivorship can affect results.",
            "WINDOW ANCHOR: AVWAP starts at each contract's first returned candle on/after history_start. Dhan does not certify contract-inception coverage; this is not a verified lifetime-AVWAP backtest.",
        ]
        if request.end >= self.as_of.date() and self.as_of.time().replace(tzinfo=None) < dtime(15, 30):
            warnings.append("PARTIAL CURRENT DAY: only candles completed by the download snapshot are included; the requested end day is not a full trading session yet.")
        for i, sid in enumerate(sorted(needed)):
            check_cancel(cancel)
            c = universe.contracts[sid]
            progress("options", f"Fixed-contract history: {c.name}", i, len(needed))
            identity = {**asdict(c), "segment": "NSE_FNO"}
            through = min(end_exclusive, datetime.fromisoformat(c.expiry).date() + timedelta(days=1))
            candles[sid] = self._history(identity, request.history_start, through, cancel)
            if not candles[sid]:
                raise BacktestError(f"No fixed-contract history for {c.name}. Cannot initialize its AVWAP; no rolling-series substitute used")
        progress("options", "Historical download complete", len(needed), len(needed))
        return BacktestDataset(contracts, candles, spots, metadata={
            "source": "DHAN_FIXED_CONTRACTS", "catalog_as_of": self.as_of.isoformat(),
            "universe_mode": "current_master_snapshot", "lifetime_history_verified": False,
            "history_start": str(request.history_start), "data_as_of": self.as_of.isoformat(),
            "download_requests": self.requests, "cache_hits": self.cache_hits,
            "contracts_requested": len(needed),
        }, warnings=warnings)

    def close(self):
        session = getattr(self.rest, "_session", None)
        if session is not None:
            session.close()
