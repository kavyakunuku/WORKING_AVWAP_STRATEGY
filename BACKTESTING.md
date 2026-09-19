# AVWAP backtesting

The backtester reuses `SignalEngine`, `AvwapStore`, `PaperBroker`, `RiskManager`
and the existing expiry/ATM/ITM selection functions. It never constructs a
`TraderApp` or `DhanBroker`. Every run gets a **new, isolated SQLite database**;
trading positions, signals, AVWAP states and emergency flags are not loaded or
changed, even if the configuration's trading mode is LIVE.

## Important: data scope of this first version

This is a **fixed-contract, coverage-aware research engine**, not a claim of
verified lifetime-AVWAP results across arbitrary expired periods.

- Dhan requests use `/v2/charts/intraday`: stock spots use `NSE_EQ/EQUITY`,
  index spots use `IDX_I/INDEX`, and fixed options use `NSE_FNO/OPTSTK` or
  `NSE_FNO/OPTIDX`. Requests are paced, retried, cached and chunked at 90 days.
- Dhan's expired-options endpoint supplies **rolling ATM-relative data**.
  A rolling stream can change strike; it cannot be accumulated as one
  contract's AVWAP. This implementation deliberately does **not** stitch
  those streams together. [1](https://dhanhq.co/docs/v2/expired-options-data/)
- The catalog is the **current Dhan instrument master**, not an archived
  point-in-time option chain. Every Dhan result flags possible historical
  strike-listing, lot-size and survivorship differences. A missing historical
  monthly expiry is an error, not permission to substitute a future series.
  Weekly selection requires enough known legs and a nearest expiry within
  seven days: a conservative coverage guard, not an invented expiry weekday.
- `history_start` defaults to the trading start month's first day, matching
  the application's default history window. All supplied earlier candles
  warm up AVWAP without trading. The provider cannot certify that the first
  returned candle is the contract's first-ever tradable candle: **every Dhan
  run is labelled WINDOW ANCHOR / lifetime coverage unverified**. Choose an
  earlier history start where available; this is not itself proof of inception
  coverage. There is no silent short-history fallback.
- Selected symbols or contracts with no history cause the run to fail. Missing
  individual bars are counted, never synthesized: they may be illiquidity or
  data gaps. Missing underlying bars disable entries for that underlying at
  that timestamp; option exits still work. Stale position marks are disclosed.
- Broad expired-period/lifetime validation needs a verified **contract-level
  archive**, including historical expiry calendars, strike listings, lot sizes
  and inception candles. `BacktestDataset` / the source interface is the
  extension point; a rolling-data approximation is not enabled by default.

See also the official intraday request reference:
https://dhanhq.co/docs/v2/historical-data/

## Run from the dashboard

The normal dashboard has a **Research → Backtesting** page. It allows dates,
AVWAP history start, a subset of the approved symbols, capital baseline,
slippage, per-order fees, turnover-based costs and optional terminal liquidation.
Strategy and risk parameters are displayed read-only and inherited from config.

You can also start **only** the research dashboard, without starting a trading
engine, fetching a live scanner or creating a trading database:

```bash
pip install -r requirements.txt
python -m backtest --serve --port 8001
# Open http://localhost:8001 (server binds to 0.0.0.0 by default).
```

Set Dhan credentials in the existing ignored `config/config.json` or the server's
`DHAN_CLIENT_ID` / `DHAN_ACCESS_TOKEN` environment variables. Never enter them in
the backtest form. The optional dashboard **control token** guards start/cancel
requests; it is distinct from the Dhan token and is never saved by the page.

Results include net MTM P&L, realized/unrealized breakdown, configured costs,
closed-trade win rate/profit factor, drawdown, an equity curve, daily MTM P&L,
trade history, coverage diagnostics and JSON/CSV exports. Jobs run in a background
thread, show progress and can be cancelled. Completed reports survive restarts;
abandoned jobs are shown as interrupted, never as completed.

## Command line

```bash
# Dhan, for a period whose fixed-contract metadata/history is available:
python -m backtest --start 2026-09-15 --end 2026-09-18 \
  --history-start 2026-09-01 --symbols RELIANCE HDFCBANK NIFTY BANKNIFTY \
  --slippage-bps 2 --fee-per-order 20 --cost-bps 1

# Explicitly synthetic example, no Dhan credentials or network needed:
python -m backtest --demo --start 2026-09-14 --end 2026-09-18 \
  --history-start 2026-09-01 --symbols RELIANCE NIFTY

# Optional terminal close; off by default:
python -m backtest --demo --start 2026-09-14 --end 2026-09-18 \
  --symbols RELIANCE --close-at-end
```

End dates are **inclusive, IST**. Future end dates are rejected. Current-day
Dhan downloads include only candles completed at the download snapshot and are
not cached as complete days. The example Dhan dates are illustrative: they will
be refused once the necessary series are no longer in the current master.
Use `--config`, `--output-dir`, and `--help` as needed. Exit codes: `0` complete,
`2` invalid/failed, `130` cancelled. Synthetic runs are prominently labelled and
are never evidence of strategy performance. Missing credentials never trigger
an automatic synthetic fallback.

## Execution semantics

1. **Signal-candle-close fills only** in this version, as requested. These are
   idealized fills, not next-bar execution or guaranteed executable quotes.
   SELL slippage reduces the fill; BUY slippage increases it.
2. At each completed bar, the scanner is selected using that bar's historical
   underlying **close**, the available catalog and the configured expiry rules.
   All available option marks at that timestamp are updated before processing
   contracts in ascending security-ID order. There are no future quotes or
   live-chain calls in the replay loop.
3. Per-contract AVWAP advances even outside the scanner. Scanner membership
   gates entries, not exits. Open positions on rolled/off-scanner contracts
   continue receiving candle-based exit checks. Re-entry requires a fresh cross.
4. Production risk limits are reused: quantity, maximum open positions, entries
   per IST day, and daily-loss gate. The loss gate uses production's realized
   today + current unrealized formula, minus fees incurred that day. Live
   database control flags are intentionally not inherited.
5. Fees per fill = `fee_per_order + fill_price × quantity × cost_bps / 10,000`.
   Equity = capital baseline + realized + marked unrealized − all paid fees.
   Win rate/profit factor use **closed trades net of their entry/exit costs**.
   Profit factor is `null` when no losing closed trades exist, never infinity.
6. `candle_ts` remains the candle **start**. Replay fill timestamps are the
   candle **close**; the final 15:15–15:30 candle is included. This reporting
   correction is confined to the replay database and does not rewrite trades.
7. Open positions remain open and marked by default. `close_at_end` creates
   separately labelled `BACKTEST_END` exits only if a candle exists at the
   final replay timestamp. It never invents a stale-price exit. An open position
   reaching expiry is flagged **UNRESOLVED EXPIRY**—last marks are provisional,
   not cash/physical settlement proceeds.
8. No margin/exposure/capital gate, order-book liquidity, bid/ask spread, market
   impact, or automatic STT/tax/settlement model is implemented. Capital is a
   reporting baseline only. Daily P&L/drawdown are mark-to-market, not merely
   sums of closed trades.

## Files and configuration

```
data/backtests/                  # ignored by Git
  cache/instruments/             # separate Dhan master cache
  cache/candles/                 # exact request + full contract identity keys
  runs/<random-run-id>/
    status.json                  # sanitized inputs, progress, summary
    replay.db                    # isolated strategy journal/AVWAP/positions
    result.json
    trades.csv
    equity.csv
    daily.csv
    signals.csv
```

Full identity cache keys include expiry, strike, side and underlying, not just
an exchange security ID that may be reused. Invalid/conflicting candles are
rejected; corrupted cache files are re-fetched. API credentials are not placed
in run metadata or reports. Export endpoints whitelist filenames and run IDs.

| Config | Default | Purpose |
|---|---:|---|
| `backtest.output_dir` | `data/backtests` | separate caches and results |
| `backtest.history_request_gap_seconds` | `1.0` | minimum gap between history calls |
| `backtest.max_days` | `366` | maximum requested trading date span |
| `backtest.max_contracts` | `2000` | cap on contracts selected across the period |
| `backtest.initial_capital` | `1000000` | reporting baseline |

One worker is allowed per server process. Prefer a standalone backtest process
outside trading hours: storage and sessions are isolated, but Dhan API limits
may still be shared by the account. Cancellation waits for an in-flight HTTP
request/retry to return. Cached data and old runs are retained until removed by
the operator; plan disk space accordingly. The built-in Flask server is for a
trusted single-user environment; use appropriate network access controls.

## HTTP API

- `GET /api/backtests/options` — safe defaults/approved symbols, credential-presence boolean
- `GET /api/backtests` — latest 50 saved runs
- `POST /api/backtests` — validated replay request, returns `202`; no paths or strategy/risk overrides accepted
- `GET /api/backtests/<id>` — status/progress
- `POST /api/backtests/<id>/cancel` — cancellation
- `GET /api/backtests/<id>/result` — completed report only
- `GET /api/backtests/<id>/export/{result.json,trades.csv,equity.csv,daily.csv,signals.csv}`

Start/cancel use `X-Control-Token` (or the existing JSON `token` convention)
when `dashboard.control_token` is configured. Read-only results follow the
existing dashboard's read-access model. Running/failed/cancelled/interrupted
runs cannot be downloaded as completed results.

## Validation

```bash
python -m pytest tests/ -q
python tools/dashboard_render_check/gen_payload.py /path/to/fresh/temp-directory
node tools/dashboard_render_check/render_check.js /path/to/fresh/temp-directory
```

The renderer needs the development-only `jsdom` npm package. Python tests cover
warm-up/no look-ahead, cross/re-entry/hold/equality, day/expiry rolls, multi-weekly
legs, risk gates, costs, missing data, cancellation, private storage, live-broker
exclusion, Dhan request shapes/cache/boundaries, API controls and CLI exports.
Dhan HTTP calls are mocked in tests; a successful suite is not verification of
real account entitlements or data completeness.
