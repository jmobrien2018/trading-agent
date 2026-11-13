"""Command line: `check`, `secrets ...`, `broker account`, and `data pull|report|refresh|latest|status`."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from . import __version__, alpaca, credentials, data_quality, pipeline, storage
from .config import PROJECT_ROOT, ConfigError, Settings, load_settings, parse_symbols
from .logging_setup import setup_logging
from .models import TIMEFRAMES
from .pipeline import pull_window

log = logging.getLogger("trading_agent")


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

    conn = storage.connect(settings.db_file)
    try:
        with client:
            is_open = bool(client.get_clock().get("is_open"))
            counts, newest = pipeline.ingest(
                client, conn, symbols, args.timeframe, start, end, feed=feed, adjustment=settings.data_adjustment
            )
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


def _cmd_data_refresh(args: argparse.Namespace) -> int:
    settings = _load(args)
    if settings is None:
        return 2
    with pipeline.refresh_lock(settings.data_dir / "refresh.lock") as acquired:
        if not acquired:
            log.info("refresh skipped: another refresh is running")
            print("Another refresh is already running; skipped.")
            return 0
        client = _client(settings)
        if client is None:
            return 2
        conn = storage.connect(settings.db_file)
        try:
            with client:
                result = pipeline.refresh(client, conn, settings)
            if result.total_bars or result.snapshots or result.warnings or result.checked_session:
                storage.record_event(conn, "data_refresh", {
                    "feed": settings.data_feed, "market_open": result.market_open, "written": result.written,
                    "backfilled": result.backfilled, "snapshots": result.snapshots,
                    "checked_session": result.checked_session, "warnings": result.warnings,
                })
        except alpaca.AlpacaError as e:
            log.error("refresh failed: %s", e)
            print(e, file=sys.stderr)
            return 1
        except Exception:
            # Scheduled runs have no console; make sure the traceback reaches the log file.
            log.exception("refresh crashed")
            raise
        finally:
            conn.close()

    print(f"Refresh done ({settings.data_feed.upper()} feed). Market is {'OPEN' if result.market_open else 'closed'}.")
    for tf, counts in result.written.items():
        new = result.backfilled.get(tf, [])
        note = f"  (backfilled {', '.join(new)})" if new else ""
        print(f"  {tf:<5} {sum(counts.values()):>9,} bars written for {len(counts)} symbol(s){note}")
    if result.market_open:
        print(f"  quotes {result.snapshots:>8} snapshots saved")
    if result.checked_session:
        print(f"  quality check of {result.checked_session}: "
              f"{'see warnings below' if result.warnings else 'all symbols above minimum coverage'}")
    if result.warnings:
        print("\nWarnings:")
        for w in result.warnings:
            print(f"  - {w}")
    return 0


def _cmd_data_latest(args: argparse.Namespace) -> int:
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
    conn = storage.connect(settings.db_file)
    try:
        with client:
            is_open = bool(client.get_clock().get("is_open"))
            snaps = client.get_snapshots(symbols, feed=settings.data_feed)
        storage.insert_snapshots(conn, snaps, settings.data_feed)
    except alpaca.AlpacaError as e:
        print(e, file=sys.stderr)
        return 1
    finally:
        conn.close()

    now = datetime.now(timezone.utc)
    print(f"Latest quotes ({settings.data_feed.upper()} feed). Market is {'OPEN' if is_open else 'closed'}.")
    if settings.data_feed == "iex":
        print("IEX bid/ask is IEX's own book, not the national best, so spreads can look wider.")
    print(f"\n  {'SYMBOL':<7} {'LAST':>10} {'CHG':>7} {'BID':>10} {'ASK':>10} {'SPREAD':>9} {'QUOTE AGE':>10}")
    by_symbol = {s.symbol: s for s in snaps}
    for sym in symbols:
        s = by_symbol.get(sym)
        if s is None:
            print(f"  {sym:<7} {'no data':>10}")
            continue
        chg = f"{(s.last_price / s.prev_close - 1):+.2%}" if s.last_price and s.prev_close else "-"
        spread = f"{s.spread_bps:.1f} bp" if s.spread_bps is not None else "-"
        print(f"  {sym:<7} {_fmt_px(s.last_price):>10} {chg:>7} {_fmt_px(s.bid):>10} {_fmt_px(s.ask):>10} "
              f"{spread:>9} {_fmt_age(data_quality.age_minutes(s.quote_ts_utc, now)):>10}")
    return 0


def _cmd_data_status(args: argparse.Namespace) -> int:
    settings = _load(args)
    if settings is None:
        return 2
    conn = storage.connect(settings.db_file)
    try:
        inventory = storage.bar_inventory(conn)
        last_refresh = storage.recent_events(conn, "data_refresh", limit=1)
    finally:
        conn.close()

    now = datetime.now(timezone.utc)
    if last_refresh:
        ts = datetime.fromisoformat(last_refresh[0]["ts_utc"])
        print(f"Last refresh with new data: {ts:%Y-%m-%d %H:%M} UTC ({_fmt_age(data_quality.age_minutes(ts, now))} ago)")
    else:
        print("No refresh has run yet. Run: trading-agent data refresh")
    if (settings.data_dir / "refresh.lock").exists():
        print("A refresh is running right now.")
    if not inventory:
        print("No bars stored yet.")
        return 0

    watch = set(settings.watchlist)
    print(f"\n  {'FEED':<4} {'TF':<5} {'SYMBOL':<7} {'BARS':>9}  {'FIRST':<16} {'LAST':<16} {'AGE':>8}")
    for row in inventory:
        fmt = "%Y-%m-%d" if row["timeframe"] == "1Day" else "%Y-%m-%d %H:%M"
        mark = "" if row["symbol"] in watch else "  (not on watchlist)"
        print(f"  {row['feed']:<4} {row['timeframe']:<5} {row['symbol']:<7} {row['bars']:>9,}  "
              f"{row['first'].strftime(fmt):<16} {row['last'].strftime(fmt):<16} "
              f"{_fmt_age(data_quality.age_minutes(row['last'], now)):>8}{mark}")
    print("\nTimes are UTC. Daily bars are stamped at midnight New York time.")
    return 0


def _fmt_px(value: float | None) -> str:
    return "-" if value is None else f"{value:,.2f}"


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

    d_ref = data_sub.add_parser(
        "refresh", help="backfill new symbols, fetch new bars, save quotes, check quality (run on a schedule)"
    )
    d_ref.set_defaults(func=_cmd_data_refresh)

    d_lat = data_sub.add_parser("latest", help="latest quote and trade per symbol (also saved)")
    d_lat.add_argument("--symbols", nargs="+", default=None)
    d_lat.set_defaults(func=_cmd_data_latest)

    d_stat = data_sub.add_parser("status", help="what is stored per symbol and how fresh it is (no API calls)")
    d_stat.set_defaults(func=_cmd_data_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = (args.root or PROJECT_ROOT).resolve()
    load_dotenv(root / ".env", override=False)  # optional fallback; real env vars win
    args.root = root
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
