"""Command line: `trading-agent check` and `trading-agent secrets ...`."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from . import __version__, credentials, storage
from .config import PROJECT_ROOT, ConfigError, load_settings
from .logging_setup import setup_logging

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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = (args.root or PROJECT_ROOT).resolve()
    load_dotenv(root / ".env", override=False)  # optional fallback; real env vars win
    args.root = root
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
