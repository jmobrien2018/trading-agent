"""Command line: `check`, `secrets ...`, `broker account`, and `data pull|report|refresh|latest|status`."""

from __future__ import annotations

import argparse
import getpass
import logging
import math
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

# Strategies added together in the "combined account" line of `backtest run` (each keeps its own slice).
COMBINED = ("trend_ma", "rsi2_pullback")


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
    feed = args.feed or pipeline.feed_for(args.timeframe, settings)
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
                client, conn, symbols, args.timeframe, start, end, feed=feed,
                adjustment=pipeline.adjustment_for(args.timeframe, settings),
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
        if tf in result.full_repull:
            note = "  (full history re-pulled for dividend adjustment, once a day)"
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


def _pct(x: float | None, digits: int = 1) -> str:
    return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:.{digits}f}%"


def _num(x: float | None, digits: int = 2) -> str:
    return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{digits}f}"


def _bt_row(label: str, cost: float, m: dict) -> str:
    return (f"  {label:<34} {cost:>4.0f}bp {_pct(m.get('total_return')):>8} {_pct(m.get('cagr')):>7} "
            f"{_pct(m.get('max_drawdown')):>7} {_num(m.get('sharpe')):>6} {m.get('trades', '-'):>6} "
            f"{_pct(m.get('win_rate'), 0):>5} {_pct(m.get('exposure'), 0):>6}")


def _cmd_backtest_run(args: argparse.Namespace) -> int:
    from . import backtest
    from .strategies import REGISTRY
    from .strategy import no_lookahead

    settings = _load(args)
    if settings is None:
        return 2
    try:
        symbols = list(_symbols(args, settings))
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    names = list(REGISTRY) if args.strategy == "all" else [args.strategy]

    conn = storage.connect(settings.db_file)
    try:
        feed = settings.daily_feed
        bars = {s: storage.load_daily_bars(conn, s, feed) for s in symbols}
        missing = [s for s, df in bars.items() if df.empty]
        if missing:
            print(f"No daily bars ({feed.upper()} feed) for {', '.join(missing)}. Run: trading-agent data refresh",
                  file=sys.stderr)
            return 1
        spy = bars.get("SPY")
        if spy is None:
            spy = storage.load_daily_bars(conn, "SPY", feed)

        holdout = settings.holdout_start
        last_date = max(df.index[-1] for df in bars.values())
        batch = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_ids: list[int] = []
        defaults_runs: dict[float, list] = {}  # cost -> [(strategy class, result)] at default settings

        for name in names:
            cls = REGISTRY[name]
            strat = cls()
            # Symbols used only as signal sources (e.g. QQQ when trading TQQQ alone).
            sources = {strat.signal_symbol(s, cls.defaults()) for s in symbols} - set(bars)
            signal_bars = {s: storage.load_daily_bars(conn, s, feed) for s in sources}
            if any(df.empty for df in signal_bars.values()):
                print(f"{cls.label()} needs daily bars for {', '.join(sorted(sources))}. "
                      f"Add them to [watchlist] and run: trading-agent data refresh", file=sys.stderr)
                return 1
            for s, df in bars.items():  # refuse to run a strategy that peeks at the future
                report = no_lookahead(strat, df.loc[: holdout - timedelta(days=1)], cls.defaults())
                if not report.ok:
                    print(f"{cls.label()} looks ahead on {s}: {report.problems[0]}", file=sys.stderr)
                    return 1
            if args.period == "holdout":
                prior = storage.count_backtest_runs(conn, cls.name, cls.version, "holdout")
                start, end = holdout, last_date
                print(f"HOLD-OUT RUN for {cls.label()}: data from {holdout} was kept unseen. "
                      f"Previous hold-out runs of this version: {prior}. Use this result as final; don't tune on it.")
            else:
                start = backtest.first_tradable_date(bars, cls.warmup_bars)
                end = holdout - timedelta(days=1)

            print(f"\n{cls.label()}: {cls.description}")
            print(f"{args.period}, {start} to {end}, ${settings.backtest_capital:,.0f}, "
                  f"{len(symbols)} symbols ({feed.upper()} daily bars, split- and dividend-adjusted)\n")
            varying = [k for k, v in cls.params.items() if len(v.test) > 1]
            fixed = {k: v.test[0] for k, v in cls.params.items() if len(v.test) == 1}
            if fixed:
                print("Fixed: " + ", ".join(f"{k}={v}" for k, v in fixed.items()) + "\n")
            print(f"  {'RUN / PARAMETERS':<34} {'COST':>6} {'RETURN':>8} {'CAGR':>7} {'MAX DD':>7} "
                  f"{'SHARPE':>6} {'TRADES':>6} {'WIN':>5} {'IN MKT':>6}")
            fills = ["open", "close"] if args.fill == "both" else [args.fill]
            for p in cls.param_grid():
                label = ", ".join(f"{k}={p[k]}" for k in varying) or "defaults"
                for fill in fills:
                    shown = f"{label}, fill at {fill}" if len(fills) > 1 else label
                    for cost in settings.backtest_costs_bp:
                        cfg = backtest.BacktestConfig(start, end, settings.backtest_capital, cost,
                                                      position_cap_pct=settings.backtest_position_cap_pct, fill=fill)
                        result = backtest.run(strat, p, bars, cfg, signal_bars=signal_bars)
                        run_id = storage.save_backtest(conn, batch, args.period, result, symbols)
                        run_ids.append(run_id)
                        print(_bt_row(f"#{run_id} {shown}", cost, result.metrics))
                        if p == cls.defaults() and fill == "open" and name in COMBINED:
                            defaults_runs.setdefault(cost, []).append((cls, result))
            print("  Benchmarks")
            for cost in settings.backtest_costs_bp:
                cfg = backtest.BacktestConfig(start, end, settings.backtest_capital, cost)
                eq = backtest.buy_and_hold(bars, cfg)
                print(_bt_row(f"buy and hold, {len(symbols)} symbols equal $", cost,
                              backtest.metrics(eq, start_value=cfg.capital)))
                if not spy.empty:
                    spy_eq = backtest.buy_and_hold({"SPY": spy}, cfg)
                    print(_bt_row("buy and hold, SPY", cost, backtest.metrics(spy_eq, start_value=cfg.capital)))

        # Combined: every strategy at its default settings, each with its own fixed slice of money,
        # added together as one account.
        if defaults_runs and len(next(iter(defaults_runs.values()))) > 1:
            parts = defaults_runs[next(iter(defaults_runs))]
            who = " + ".join(f"{cls.name} ({cls.defaults().label()})" for cls, _ in parts)
            print(f"\nCombined account: {who}")
            print(f"${settings.backtest_capital:,.0f} each, kept separate (fixed slices), added together\n")
            for cost, runs in defaults_runs.items():
                equity = sum(r.equity for _, r in runs).dropna()
                invested = sum(r.equity * r.exposure for _, r in runs).dropna()
                m = backtest.metrics(equity, start_value=settings.backtest_capital * len(runs))
                m["exposure"] = float((invested / equity).mean())
                m["trades"] = sum(len(r.trades) for _, r in runs)
                print(_bt_row(f"combined, {len(runs)} strategies", cost, m))

        storage.record_event(conn, "backtest_run", {
            "batch": batch, "period": args.period, "strategies": names, "symbols": symbols,
            "run_ids": [run_ids[0], run_ids[-1]] if run_ids else [],
        })
    finally:
        conn.close()

    print("\nRETURN and CAGR are after costs. MAX DD = worst peak-to-trough fall. IN MKT = average share of "
          "capital invested.\nTrades for a run: trading-agent backtest export <run number>")
    return 0


def _cmd_backtest_export(args: argparse.Namespace) -> int:
    settings = _load(args)
    if settings is None:
        return 2
    conn = storage.connect(settings.db_file)
    try:
        run = storage.get_backtest_run(conn, args.run_id)
        if run is None:
            print(f"No backtest run #{args.run_id}.", file=sys.stderr)
            return 1
        trades = storage.backtest_trades(conn, args.run_id)
    finally:
        conn.close()
    out_dir = settings.data_dir / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"backtest_{args.run_id}_{run['strategy']}_trades.csv"
    trades.to_csv(path, index=False)
    params = ", ".join(f"{k}={v}" for k, v in run["params"].items())
    print(f"Run #{args.run_id}: {run['strategy']} v{run['version']} ({params}), {run['cost_bp']:.0f}bp, {run['period']}")
    print(f"{len(trades)} trades written to {path}")
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

    bt = sub.add_parser("backtest", help="test strategies on stored daily bars")
    bt_sub = bt.add_subparsers(dest="backtest_command", required=True)
    from .strategies import REGISTRY

    b_run = bt_sub.add_parser("run", help="run every parameter set at every cost level; results are saved")
    b_run.add_argument("--strategy", choices=["all", *REGISTRY], default="all")
    b_run.add_argument("--period", choices=["in-sample", "holdout"], default="in-sample",
                       help="in-sample (default) tunes and compares; holdout is the final check on unseen data")
    b_run.add_argument("--symbols", nargs="+", default=None, help="default: [watchlist] symbols in settings")
    b_run.add_argument("--fill", choices=["open", "close", "both"], default="open",
                       help="when orders fill: next day's open (default), same day's close, or both")
    b_run.set_defaults(func=_cmd_backtest_run)

    b_exp = bt_sub.add_parser("export", help="write a run's trades to a CSV file")
    b_exp.add_argument("run_id", type=int)
    b_exp.set_defaults(func=_cmd_backtest_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = (args.root or PROJECT_ROOT).resolve()
    load_dotenv(root / ".env", override=False)  # optional fallback; real env vars win
    args.root = root
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
