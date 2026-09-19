"""One cancellable background replay per process; persistent, isolated runs."""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path

import psutil

from backtest.data import DhanHistorySource, atomic_json, check_cancel
from backtest.demo import DemoHistorySource
from backtest.engine import BacktestEngine
from backtest.models import BacktestCancelled, BacktestError, BacktestRequest
from backtest.reports import EXPORTS, export_result
from common.scanner import APPROVED_INDICES, scanner_symbols
from common.utils import now_ist
from common.version import APP_VERSION

log = logging.getLogger("avwap.backtest")
ACTIVE = {"queued", "running", "cancelling"}


class BacktestBusy(BacktestError):
    pass


class BacktestService:
    def __init__(self, cfg, *, source_factory=None):
        self.cfg = copy.deepcopy(cfg)
        self.root = Path(cfg.get("backtest", {}).get("output_dir", "data/backtests")).resolve()
        self.source_factory = source_factory
        self._lock = threading.RLock()
        self._current = None
        self._cancel = threading.Event()
        self._thread = None
        self._state = None

    def options(self):
        today = now_ist().date()
        return {"symbols": scanner_symbols(self.cfg), "indices": list(APPROVED_INDICES),
                "today": str(today), "strategy": self.cfg.get("strategy", {}),
                "risk": self.cfg.get("risk", {}),
                "weekly_expiries": self.cfg.get("market_data", {}).get("weekly_expiries", {}),
                "initial_capital": self.cfg.get("backtest", {}).get("initial_capital", 1_000_000),
                "slippage_bps": self.cfg.get("paper", {}).get("slippage_bps", 0),
                "max_days": self.cfg.get("backtest", {}).get("max_days", 366),
                "dhan_configured": bool(self.cfg.get("dhan", {}).get("client_id") and
                                        self.cfg.get("dhan", {}).get("access_token")),
                "fill_mode": "candle_close"}

    def _directory(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise KeyError("Unknown backtest")
        return self.root / "runs" / run_id

    def _begin(self, payload):
        request = BacktestRequest.parse(payload, self.cfg)
        with self._lock:
            if self._current:
                raise BacktestBusy("A backtest is already running; cancel it or wait for completion")
            run_id = uuid.uuid4().hex
            directory = self._directory(run_id)
            directory.mkdir(parents=True, exist_ok=False)
            self._current = run_id
            self._cancel = threading.Event()
            self._state = {"id": run_id, "status": "queued", "request": request.to_dict(),
                           "created_at": time.time(), "updated_at": time.time(),
                           "phase": "queued", "detail": "Waiting for worker", "progress": 0, "total": 0,
                           "pid": os.getpid(), "process_created": psutil.Process().create_time()}
            atomic_json(directory / "status.json", self._state)
            return run_id, request

    def submit(self, payload):
        with self._lock:
            run_id, request = self._begin(payload)
            self._thread = threading.Thread(target=self._run, args=(run_id, request),
                                            name="backtest-worker", daemon=True)
            self._thread.start()
            return self.get(run_id)

    def run_sync(self, payload):
        run_id, request = self._begin(payload)
        self._run(run_id, request)
        return self.get(run_id)

    def _update(self, **fields):
        with self._lock:
            self._state.update(fields, updated_at=time.time())
            atomic_json(self._directory(self._state["id"]) / "status.json", self._state)

    def _progress(self, phase, detail, current, total):
        self._update(phase=phase, detail=detail, progress=current, total=total)

    def _run(self, run_id, request):
        source = None
        try:
            self._update(status="running")
            if self.source_factory:
                source = self.source_factory(request)
            elif request.source == "demo":
                source = DemoHistorySource(self.cfg)
            else:
                source = DhanHistorySource(self.cfg, self.root / "cache")
            data = source.load(request, self._progress, self._cancel)
            check_cancel(self._cancel)
            directory = self._directory(run_id)
            result = BacktestEngine(self.cfg).run(request, data, directory / "replay.db",
                                                  progress=self._progress, cancel=self._cancel)
            check_cancel(self._cancel)
            result.update(run_id=run_id, version=APP_VERSION)
            export_result(directory, result)
            self._update(status="completed", phase="complete", detail="Replay complete",
                         completed_at=time.time(), summary=result["metrics"], warning_count=len(result["warnings"]))
        except (BacktestCancelled, KeyboardInterrupt):
            self._update(status="cancelled", phase="cancelled", detail="Cancelled; no completed result",
                         completed_at=time.time())
        except BacktestError as e:
            self._update(status="failed", phase="failed", detail=str(e), error=str(e), completed_at=time.time())
        except Exception:
            log.exception("Backtest %s failed", run_id)
            self._update(status="failed", phase="failed", detail="Backtest failed; check the server log",
                         error="Backtest failed; check the server log", completed_at=time.time())
        finally:
            try:
                if source and hasattr(source, "close"):
                    source.close()
            finally:
                with self._lock:
                    self._current = None

    def get(self, run_id):
        path = self._directory(run_id) / "status.json"
        try:
            out = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError) as e:
            raise KeyError("Unknown backtest") from e
        if out["status"] in ACTIVE:
            try:
                alive = abs(psutil.Process(out["pid"]).create_time() - out["process_created"]) < 1
            except (psutil.Error, KeyError, TypeError, ValueError):
                alive = False
            if out.get("pid") == os.getpid() and self._current != run_id:
                alive = False
            if not alive:
                out.update(status="interrupted", detail="Worker stopped before completion. Start a new run; partial results are not presented as complete.")
        return out

    def list_runs(self):
        paths = list((self.root / "runs").glob("*/status.json"))
        out = []
        for p in sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)[:50]:
            try:
                out.append(self.get(p.parent.name))
            except (KeyError, OSError):
                continue
        return out

    def cancel(self, run_id):
        self.get(run_id)
        with self._lock:
            if self._current != run_id or self._state["status"] not in ACTIVE:
                raise BacktestError("This run is not active in this server process")
            self._cancel.set()
            self._update(status="cancelling", detail="Cancellation requested; waiting for the current data request")
        return self.get(run_id)

    def result(self, run_id):
        if self.get(run_id)["status"] != "completed":
            raise BacktestError("A completed result is not available for this run")
        return json.loads((self._directory(run_id) / "result.json").read_text(encoding="utf-8"))

    def export_path(self, run_id, filename):
        if filename not in EXPORTS:
            raise KeyError("Unknown export")
        if self.get(run_id)["status"] != "completed":
            raise BacktestError("Exports are available only for completed runs")
        return self._directory(run_id) / filename
