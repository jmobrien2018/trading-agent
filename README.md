# Trading Agent

A Claude-powered agent that paper trades US stocks to test day- and swing-trading strategies.
It is built to real-money standards, but runs on paper only until the go-live gate is passed.

The project plan, decisions log and go-live gate live in the "Trading Agent — Project Plan" doc.

## Status

- Phase 0 (foundations): done. Settings, secrets handling, local storage, logging and a CLI.
- Phase 1 (broker and data): built. Read-only Alpaca client, bar storage, data-quality report.
- Phase 2 (data pipeline): built. Backfill, incremental refresh, live quotes, quality warnings,
  and a script to run the refresh on a schedule.

No orders can be placed; that waits for the risk engine (Phase 4).

## Setup (Windows, PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock   # exact pinned versions
python -m pip install -e ".[dev]"
pytest
trading-agent check
```

## Secrets

API keys never go in code, Git, config files or chat. They live in the OS keychain
(Windows Credential Manager), under the service name `trading-agent`:

```powershell
trading-agent secrets set ALPACA_PAPER_API_KEY      # prompts with hidden input
trading-agent secrets set ALPACA_PAPER_SECRET_KEY
trading-agent secrets status                        # shows set / MISSING, never values
```

Environment variables override the keychain. A git-ignored `.env` file (copy `.env.example`)
also works as a fallback, but the keychain is preferred.

Broker keys should be paper-only, with 2FA on the Alpaca account.

## Keeping data up to date (Phase 2)

```powershell
trading-agent data refresh     # backfill new symbols, fetch new bars, save quotes, check quality
trading-agent data status      # what is stored per symbol and how fresh (no API calls)
trading-agent data latest      # latest quote and trade per symbol
```

`data refresh` is safe to run any time:

- The first time it sees a symbol it backfills `[history]`: 90 days of 1-minute bars and
  5 years of daily bars by default. Making `[history]` deeper triggers one more backfill.
- After that it only fetches bars newer than the last one stored (with a small overlap).
- While the market is open it saves a quote snapshot per symbol and warns about any symbol whose
  newest minute bar is older than `stale_minutes`.
- Once per completed session it warns about any symbol whose minute coverage was below
  `min_coverage`.
- Only one refresh runs at a time. Warnings go to the screen, `logs/trading-agent.log` and the
  audit log.

Add or remove symbols in `[watchlist]`; the next refresh picks them up.

### Run it automatically

Deferred to Phase 5, when Claude starts using live data. Until then, run `trading-agent data refresh`
by hand before a work session; it catches up everything since the last run.

```powershell
.\scripts\schedule-refresh.ps1                 # Windows Task Scheduler, every 5 minutes
.\scripts\schedule-refresh.ps1 -EveryMinutes 2
.\scripts\schedule-refresh.ps1 -Remove
```

The task runs as you, only while you're logged on, with no console window. Outside market hours
each run finds nothing new and exits in a second or two. Check on it with `trading-agent data status`.

## Backtesting (Phase 3)

Strategies follow the standard format in `src/trading_agent/strategy.py` (spec: the plan doc's
"Strategy format spec" tab). Each lives in its own file under `src/trading_agent/strategies/`.

```powershell
trading-agent backtest run                               # every strategy, every parameter set, at 5 and 10 bp
trading-agent backtest run --strategy trend_ma
trading-agent backtest export 23                         # a run's trades to CSV (data folder\exports)
trading-agent backtest run --strategy trend_ma --period holdout   # final check only; don't tune on it
```

- Daily bars come from the full-market (SIP) feed, adjusted for splits and dividends, back to 2016.
- Settings live in `[backtest]` in `config/settings.toml`: $10,000 per strategy, costs at 5 and 10 bp per side,
  and the hold-out year that starts on 2025-11-14.
- Orders fill at the next open. Fractional shares are allowed, and each position is capped at `max_position_usd`.
- Before running, each strategy is checked for look-ahead: its signals must not change when future bars are removed.
- Every run and trade is saved to the database. Hold-out runs are counted, so it's visible how often the unseen
  year has been used.

### Day strategies (1-minute bars)

```powershell
# one-time: 10 years of full-market minute bars, regular hours only (~3 million bars, a few minutes)
trading-agent data pull --feed sip --timeframe 1Min --days 3650 --regular-hours --symbols SPY QQQ TQQQ
trading-agent data spreads                               # measure real bid/ask spreads from sampled quotes
trading-agent backtest run --strategy intraday_momentum  # runs at each cost in [backtest] intraday_costs_bp
```

- Day strategies set `timeframe = "1Min"` and are flat every night (`flat_by_minute`).
- Each symbol trades an equal, fixed slice of the capital. The output shows each symbol's result under the total.
- Orders fill at the next minute's open. Stops and targets are checked against each minute's high and low.

## Ad hoc pulls and reports (Phase 1)

```powershell
trading-agent broker account                     # keys work? account status (read-only, no orders)
trading-agent data pull                          # last 7 days of 1-minute bars, watchlist, IEX feed
trading-agent data pull --feed sip               # same window from all exchanges (free plan: >15 min old)
trading-agent data report                        # coverage, longest gap, worst day per symbol and feed
trading-agent data report --days 90
trading-agent data report --timeframe 1Day --days 365
```

Pulling both feeds and running `data report` shows how thin the free IEX feed is next to
the full market (SIP).

## Layout

| Path | What it holds |
| --- | --- |
| `config/settings.toml` | Non-secret settings: mode, paths, data feed, history depth, refresh checks, watchlist, risk-limit placeholders |
| `config/settings.local.toml` | Optional, git-ignored machine overrides |
| `src/trading_agent/config.py` | Loads and validates settings; rejects any mode but `paper` |
| `src/trading_agent/credentials.py` | Reads keys from env or keychain; `Secret` masks values in output |
| `src/trading_agent/storage.py` | SQLite database: append-only audit log, price bars, quote snapshots |
| `src/trading_agent/alpaca.py` | Paper-only Alpaca REST client: account, clock, calendar, bars, snapshots |
| `src/trading_agent/pipeline.py` | Backfill, incremental refresh, snapshots, quality warnings, run lock |
| `src/trading_agent/data_quality.py` | Coverage, gap and freshness checks for stored bars |
| `src/trading_agent/strategy.py` | The standard strategy format: header, intents, exit and sizing menus, look-ahead check |
| `src/trading_agent/indicators.py` | Moving average, RSI, ATR (all causal) |
| `src/trading_agent/strategies/` | One file per strategy, plus the registry |
| `src/trading_agent/backtest.py` | Daily backtest engine, metrics, buy-and-hold benchmark |
| `src/trading_agent/logging_setup.py` | Console logs plus JSON-lines file in `logs/` |
| `src/trading_agent/cli.py` | All `trading-agent` commands |
| `scripts/schedule-refresh.ps1` | Registers or removes the scheduled refresh |
| `data/`, `logs/` | Local database and logs (git-ignored) |

## Storage

One SQLite file (`data/trading_agent.sqlite3`), about 20 MB with the default watchlist and history.
The `audit_log` table is append-only: database triggers reject updates and deletes.

This folder is inside OneDrive. OneDrive syncing a live SQLite database can cause conflicts, and
a scheduled refresh changes it every few minutes. Before turning on the schedule, consider moving
the data outside OneDrive in `config/settings.local.toml`, for example:

```toml
[paths]
data_dir = "C:/Users/<you>/trading-agent-data"
db_file = "C:/Users/<you>/trading-agent-data/trading_agent.sqlite3"
```
