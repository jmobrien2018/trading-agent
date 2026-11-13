# Trading Agent

A Claude-powered agent that paper trades US stocks to test day- and swing-trading strategies.
It is built to real-money standards, but runs on paper only until the go-live gate is passed.

The project plan, decisions log and go-live gate live in the "Trading Agent — Project Plan" doc.

## Status

Phase 0 (foundations) is scaffolded: settings, secrets handling, local storage, logging and a CLI.
Nothing talks to a broker yet. Alpaca integration starts in Phase 1.

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

## Layout

| Path | What it holds |
| --- | --- |
| `config/settings.toml` | Non-secret settings: mode, paths, broker URL, risk-limit placeholders |
| `config/settings.local.toml` | Optional, git-ignored machine overrides |
| `src/trading_agent/config.py` | Loads and validates settings; rejects any mode but `paper` |
| `src/trading_agent/credentials.py` | Reads keys from env or keychain; `Secret` masks values in output |
| `src/trading_agent/storage.py` | SQLite database; append-only audit log |
| `src/trading_agent/logging_setup.py` | Console logs plus JSON-lines file in `logs/` |
| `src/trading_agent/cli.py` | `trading-agent check` and `trading-agent secrets ...` |
| `data/`, `logs/` | Local database and logs (git-ignored) |

## Storage

Phase 0 uses one SQLite file (`data/trading_agent.sqlite3`). The `audit_log` table is
append-only: database triggers reject updates and deletes. Market-data tables arrive in Phase 2.

This folder is inside OneDrive. OneDrive syncing a live SQLite database can cause conflicts.
If that happens, point `data_dir` and `db_file` at a folder outside OneDrive in
`config/settings.local.toml`.
