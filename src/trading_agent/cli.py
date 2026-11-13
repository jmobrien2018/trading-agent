"""Command line: `check`, `secrets ...`, `broker account`, `data pull`, `data report`."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from collections import defaultdict
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from . import __version__, alpaca, credentials, data_quality, storage
from .config import PROJECT_ROOT, ConfigError, Settings, load_settings, parse_symbols
from .logging_setup import setup_logging
from .models import TIMEFRAMES

log = logging.getLogger("trading_agent")

# Free plan: SIP data from the last 15 minutes is off limits; stay a minute clear of the edge.
SIP_DELAY = timedelta(minutes=16)
UPSERT_BATCH = 5_000


def _cmd_check(args: argparse.Namespace) -> int:
    try:
        settings = load_settings(args.root)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    log_file = setup_logging(settings.log_dir)

    status = {name: credentials.secret_source(name) for name in credentials.KNOWN_SECRETS}
    missing = [name for name, src in status.items() if src is None]

    conn = storage.connect(settings.db_file)
    try:
        event_id = storage.record_event(
            conn,
            "startup_check",
            {"version": __version__, "mode": settings.mode, "secrets_set": {k: v is not None for k, v in status.items()}},
        )
    finally:
        conn.close()

    print(f"trading-agent {__version__}")
    print(f"  mode       : {settings.mode}")
    print(f"  broker     : {settings.broker_name} ({settings.broker_base_url})")
    print(f"  database   : {settings.db_file}  (audit event #{event_id})")
    print(f"  log file   : {log_file}")
    print("  secrets    :")
    for name, src in status.items():
        print(f"    {name:<24} {src or 'MISSING'}")

    log.info("startup check ok; %d secret(s) missing", len(missing))
    if missing:
        print(f"\n{len(missing)} secret(s) not set yet. Add one with: trading-agent secrets set <NAME>")
        return 1 if args.strict else 0
    return 0


def _cmd_secrets_status(args: argparse.Namespace) -> int:
    for name, desc in credentials.KNOWN_SECRETS.items():
        src = credentials.secret_source(name)
        print(f"{name:<24} {src or 'MISSING':<9} {desc}")
    return 0


def _cmd_secrets_set(args: argparse.Namespace) -> int:
    # Typed at a hidden prompt so the value never lands in shell history or on screen.
    value = getpass.getpass(f"Paste {args.name} (input hidden): ")
    try:
        credentials.set_secret(args.name, value)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    print(f"Saved {args.name} to the OS keychain (service '{credentials.SERVICE}').")
    return 0


def _cmd_secrets_delete(args: argparse.Namespace) -> int:
    if credentials.delete_secret(args.name):
        print(f"Deleted {args.name} from the OS keychain.")
    else:
        print(f"{args.name} was not in the OS keychain.")
    return 0


def _positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


def _load(args: argparse.Namespace) -> Settings | None:
    try:
        settings = load_settings(args.root)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return None
    setup_logging(settings.log_dir)
    return settings


def _client(settings: Settings) -> alpaca.AlpacaClient | None:
    try:
        return alpaca.client_from_settings(settings)
    except credentials.MissingSecretError as e:
        print(e, file=sys.stderr)
        return None


def _symbols(args: argparse.Namespace, settings: Settings) -> tuple[str, ...]:
    return parse_symbols(args.symbols) if args.symbols else settings.watchlist


def _fmt_age(minutes: float | None) -> str:
    if minutes is None:
        return "-"
    if minutes < 120:
        return f"{minutes:.0f} min"
    if minutes < 48 * 60:
        return f"{minutes / 60:.1f} h"
    return f"{minutes / 1440:.1f} d"


def _cmd_broker_account(args: argparse.Namespace) -> int:
    settings = _load(args)
    if settings is None:
        return 2
    client = _client(settings)
    if client is None:
        return 2
    try:
        with client:
            acct = client.get_account()
            clock = client.get_clock()
    except alpaca.AlpacaError as e:
        print(e, file=sys.stderr)
        return 1

    fields = ["status", "currency", "equity", "cash", "buying_power", "pattern_day_trader",
              "trading_blocked", "account_blocked", "shorting_enabled", "multiplier"]
    print(f"Alpaca paper account ({settings.broker_base_url})")
    for f in fields:
        print(f"  {f:<20} {acct.get(f, '-')}")
    print(f"  {'market open now':<20} {clock.get('is_open')}")

    conn = storage.connect(settings.db_file)
    try:
        storage.record_event(conn, "broker_account_check", {"status": acct.get("status"), "is_open": clock.get("is_open")})
    finally:
        conn.close()
    return 0


def pull_window(now: datetime, days: int, feed: str) -> tuple[datetime, datetime]:
    """Start at New York midnight `days` days ago, so every session in the window is pulled whole
    and lines up with `data report --days N`. End now (SIP: 16 minutes ago, the free-plan limit)."""
    today = now.astimezone(data_quality.NEW_YORK).date()
    start = datetime.combine(today - timedelta(days=days), dtime(0), data_quality.NEW_YORK)
    end = now - SIP_DELAY if feed == "sip" else now
    return start.astimezone(timezone.utc), end


def _cmd_data_pull(args: argparse.Namespace) -> int:
    settings = _load(args)
    if settings is None:
        return 2
    try:
        symbols = _symbols(args, settings)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    feed = args.feed or settings.data_feed
    client = _client(settings)
    if client is None:
        return 2

    now = datetime.now(timezone.utc)
    start, end = pull_window(now, args.days, feed)

    counts: dict[str, int] = defaultdict(int)
    newest: dict[str, datetime] = {}
    conn = storage.connect(settings.db_file)
    try:
        with client:
            is_open = bool(client.get_clock().get("is_open"))
            batch = []
            for bar in client.get_bars(symbols, args.timeframe, start, end, feed=feed, adjustment=settings.data_adjustment):
                batch.append(bar)
                counts[bar.symbol] += 1
                if bar.symbol not in newest or bar.ts_utc > newest[bar.symbol]:
                    newest[bar.symbol] = bar.ts_utc
                if len(batch) >= UPSERT_BATCH:
                    storage.upsert_bars(conn, batch, feed)
                    batch.clear()
            storage.upsert_bars(conn, batch, feed)
        total = sum(counts.values())
        storage.record_event(conn, "data_pull", {
            "feed": feed, "timeframe": args.timeframe, "start": start.isoformat(), "end": end.isoformat(),
            "symbols": list(symbols), "bars": dict(counts), "total": total,
        })
    except alpaca.AlpacaError as e:
        print(e, file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(f"Pulled {total:,} {args.timeframe} bars ({feed.upper()} feed), {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} UTC")
    print(f"Market is {'OPEN' if is_open else 'closed'}; 'newest bar age' shows feed delay only while it is open.\n")
    print(f"  {'SYMBOL':<7} {'BARS':>8}  {'NEWEST BAR (UTC)':<17} {'AGE':>8}")
    for sym in symbols:
        ts = newest.get(sym)
        print(f"  {sym:<7} {counts.get(sym, 0):>8,}  {ts.strftime('%Y-%m-%d %H:%M') if ts else '-':<17} "
              f"{_fmt_age(data_quality.age_minutes(ts, now)):>8}")
    missing = [s for s in symbols if not counts.get(s)]
    if missing:
        print(f"\nNo bars returned for: {', '.join(missing)}")
    return 0


def _cmd_data_report(args: argparse.Namespace) -> int:
    settings = _load(args)
    if settings is None:
        return 2
    try:
        symbols = _symbols(args, settings)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    client = _client(settings)
    if client is None:
        return 2

    now = datetime.now(timezone.utc)
    today = now.astimezone(data_quality.NEW_YORK).date()
    try:
        with client:
            sessions = client.get_calendar(today - timedelta(days=args.days), today)
    except alpaca.AlpacaError as e:
        print(e, file=sys.stderr)
        return 1
    sessions = data_quality.completed_sessions(sessions, now)
    if not sessions:
        print(f"No completed trading sessions in the last {args.days} days.")
        return 0

    # Window covers daily bars (stamped at NY midnight) and every session's minutes.
    win_start = datetime.combine(sessions[0].date, dtime(0), data_quality.NEW_YORK)
    win_end = datetime.combine(sessions[-1].date + timedelta(days=1), dtime(0), data_quality.NEW_YORK)

    conn = storage.connect(settings.db_file)
    try:
        feeds = [args.feed] if args.feed else storage.stored_feeds(conn, args.timeframe)
        if not feeds:
            print(f"No {args.timeframe} bars stored yet. Run: trading-agent data pull --timeframe {args.timeframe}")
            return 1
        summarize = data_quality.summarize_minutes if args.timeframe == "1Min" else data_quality.summarize_days
        summaries = [
            summarize(sym, feed, sessions, storage.bar_times(conn, sym, args.timeframe, feed, win_start, win_end))
            for feed in feeds
            for sym in symbols
        ]
        storage.record_event(conn, "data_report", {
            "timeframe": args.timeframe, "sessions": [s.date.isoformat() for s in sessions],
            "rows": [{"symbol": s.symbol, "feed": s.feed, "coverage": round(s.coverage, 4),
                      "max_gap_min": s.max_gap_min, "missing_days": [d.isoformat() for d in s.missing_days]}
                     for s in summaries],
        })
    finally:
        conn.close()

    print(f"{args.timeframe} coverage, {len(sessions)} completed sessions "
          f"{sessions[0].date} to {sessions[-1].date} (regular hours only)\n")
    header = f"  {'SYMBOL':<7} {'FEED':<4} {'BARS':>13} {'COVERAGE':>9}"
    if args.timeframe == "1Min":
        header += f" {'MAX GAP':>8} {'WORST DAY':>11}"
    header += f" {'MISSING DAYS':>13}"
    print(header)
    for s in summaries:
        line = f"  {s.symbol:<7} {s.feed:<4} {f'{s.present:,}/{s.expected:,}':>13} {s.coverage:>8.1%}"
        if args.timeframe == "1Min":
            line += f" {f'{s.max_gap_min} min':>8} {str(s.worst_day or '-'):>11}"
        line += f" {len(s.missing_days):>13}"
        print(line)
    if args.timeframe == "1Min":
        print("\nA minute with no trades on that feed has no bar. Low IEX coverage = thin free data.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trading-agent")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--root", type=Path, default=None, help="project root (default: this checkout)")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="verify config, storage, logging and which secrets are set")
    check.add_argument("--strict", action="store_true", help="exit 1 if any secret is missing")
    check.set_defaults(func=_cmd_check)

    secrets = sub.add_parser("secrets", help="manage API keys in the OS keychain")
    secrets_sub = secrets.add_subparsers(dest="secrets_command", required=True)

    s_status = secrets_sub.add_parser("status", help="show which secrets are set (never their values)")
    s_status.set_defaults(func=_cmd_secrets_status)

    names = list(credentials.KNOWN_SECRETS)
    s_set = secrets_sub.add_parser("set", help="store a secret (prompts with hidden input)")
    s_set.add_argument("name", choices=names)
    s_set.set_defaults(func=_cmd_secrets_set)

    s_del = secrets_sub.add_parser("delete", help="remove a secret from the keychain")
    s_del.add_argument("name", choices=names)
    s_del.set_defaults(func=_cmd_secrets_delete)

    broker = sub.add_parser("broker", help="read-only checks against the Alpaca paper account")
    broker_sub = broker.add_subparsers(dest="broker_command", required=True)
    b_acct = broker_sub.add_parser("account", help="verify keys work; show account status (no orders)")
    b_acct.set_defaults(func=_cmd_broker_account)

    data = sub.add_parser("data", help="pull market data and check its quality")
    data_sub = data.add_subparsers(dest="data_command", required=True)

    d_pull = data_sub.add_parser("pull", help="download bars into the local database")
    d_pull.add_argument("--days", type=_positive_int, default=7, help="today plus this many previous calendar days, whole days in New York time (default 7)")
    d_pull.add_argument("--timeframe", choices=TIMEFRAMES, default="1Min")
    d_pull.add_argument("--feed", choices=["iex", "sip"], default=None, help="default: [data] feed in settings")
    d_pull.add_argument("--symbols", nargs="+", default=None, help="default: [watchlist] symbols in settings")
    d_pull.set_defaults(func=_cmd_data_pull)

    d_rep = data_sub.add_parser("report", help="coverage and gaps of stored bars per symbol and feed")
    d_rep.add_argument("--days", type=_positive_int, default=7)
    d_rep.add_argument("--timeframe", choices=data_quality.REPORT_TIMEFRAMES, default="1Min")
    d_rep.add_argument("--feed", choices=["iex", "sip"], default=None, help="default: every feed stored")
    d_rep.add_argument("--symbols", nargs="+", default=None)
    d_rep.set_defaults(func=_cmd_data_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = (args.root or PROJECT_ROOT).resolve()
    load_dotenv(root / ".env", override=False)  # optional fallback; real env vars win
    args.root = root
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
