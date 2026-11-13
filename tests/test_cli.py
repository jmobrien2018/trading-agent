import logging

import pytest

from trading_agent import credentials, storage
from trading_agent.cli import main


@pytest.fixture(autouse=True)
def reset_logging():
    yield
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()


def test_check_creates_storage_and_logs(project_root, capsys):
    assert main(["--root", str(project_root), "check"]) == 0
    out = capsys.readouterr().out
    assert "mode       : paper" in out
    assert "MISSING" in out
    assert (project_root / "logs" / "trading-agent.log").is_file()

    conn = storage.connect(project_root / "data" / "trading_agent.sqlite3")
    try:
        assert storage.recent_events(conn)[0]["kind"] == "startup_check"
    finally:
        conn.close()


def test_check_strict_fails_when_secrets_missing(project_root):
    assert main(["--root", str(project_root), "check", "--strict"]) == 1


def test_check_strict_passes_and_never_prints_values(project_root, capsys, monkeypatch):
    for name in credentials.KNOWN_SECRETS:
        monkeypatch.setenv(name, f"value-of-{name}")
    assert main(["--root", str(project_root), "check", "--strict"]) == 0
    out = capsys.readouterr().out
    assert "value-of-" not in out
    assert (project_root / "logs" / "trading-agent.log").read_text().find("value-of-") == -1


def test_secrets_set_uses_hidden_prompt(project_root, monkeypatch, capsys):
    monkeypatch.setattr("getpass.getpass", lambda prompt: "typed-key")
    assert main(["--root", str(project_root), "secrets", "set", "ANTHROPIC_API_KEY"]) == 0
    assert credentials.get_secret("ANTHROPIC_API_KEY").reveal() == "typed-key"
    assert "typed-key" not in capsys.readouterr().out


@pytest.fixture
def fake_api(monkeypatch):
    """Point the CLI at an in-memory Alpaca with three finished sessions in the past few days."""
    from datetime import datetime, time, timedelta, timezone
    from zoneinfo import ZoneInfo

    from fake_alpaca import DATA, PAPER, FakeAlpaca
    from trading_agent import alpaca
    from trading_agent.credentials import Secret

    fake = FakeAlpaca()
    ny = ZoneInfo("America/New_York")
    today = datetime.now(timezone.utc).astimezone(ny).date()
    for back in (3, 2, 1):
        d = today - timedelta(days=back)
        fake.calendar.append({"date": d.isoformat(), "open": "09:30", "close": "16:00"})
        open_utc = datetime.combine(d, time(9, 30), ny).astimezone(timezone.utc)
        skip = {10, 11, 12} if back == 2 else set()
        fake.add_minutes("AAPL", open_utc, 390, skip=skip)

    def client_from_settings(settings, **kw):
        return alpaca.AlpacaClient(Secret("test-key"), Secret("s"), PAPER, DATA, transport=fake.transport,
                                   sleep=lambda s: None)

    monkeypatch.setattr(alpaca, "client_from_settings", client_from_settings)
    return fake


def test_data_pull_then_report(project_root, fake_api, capsys):
    root = ["--root", str(project_root)]
    assert main([*root, "data", "pull", "--symbols", "aapl", "msft"]) == 0
    out = capsys.readouterr().out
    assert "Pulled 1,167 1Min bars (IEX feed)" in out
    assert "No bars returned for: MSFT" in out

    assert main([*root, "data", "report", "--symbols", "AAPL"]) == 0
    out = capsys.readouterr().out
    assert "3 completed sessions" in out
    assert "1,167/1,170" in out and "99.7%" in out and "3 min" in out

    conn = storage.connect(project_root / "data" / "trading_agent.sqlite3")
    try:
        kinds = [e["kind"] for e in storage.recent_events(conn)]
    finally:
        conn.close()
    assert kinds[:2] == ["data_report", "data_pull"]


def test_pull_window_starts_at_new_york_midnight():
    from datetime import datetime, timedelta, timezone

    from trading_agent.cli import pull_window

    now = datetime(2026, 11, 13, 17, 44, tzinfo=timezone.utc)  # 12:44 in New York
    start, end = pull_window(now, 7, "iex")
    assert start == datetime(2026, 11, 6, 5, 0, tzinfo=timezone.utc)  # Nov 6 00:00 EST
    assert end == now
    _, sip_end = pull_window(now, 7, "sip")
    assert sip_end == now - timedelta(minutes=16)


def test_session_at_window_edge_is_pulled_whole(project_root, fake_api, capsys):
    """Regression: a pull starting mid-session made the report show a false gap on day one."""
    from datetime import datetime, time, timedelta, timezone
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    edge = datetime.now(timezone.utc).astimezone(ny).date() - timedelta(days=7)
    fake_api.calendar = [{"date": edge.isoformat(), "open": "09:30", "close": "16:00"}]
    fake_api.bars = {}
    fake_api.add_minutes("SPY", datetime.combine(edge, time(9, 30), ny).astimezone(timezone.utc), 390)

    root = ["--root", str(project_root)]
    assert main([*root, "data", "pull", "--days", "7", "--symbols", "SPY"]) == 0
    assert main([*root, "data", "report", "--days", "7", "--symbols", "SPY"]) == 0
    out = capsys.readouterr().out
    assert "390/390" in out and "100.0%" in out and "0 min" in out


def test_data_pull_without_keys_explains(project_root, capsys):
    assert main(["--root", str(project_root), "data", "pull"]) == 2
    assert "trading-agent secrets set ALPACA_PAPER_API_KEY" in capsys.readouterr().err


def test_report_before_any_pull(project_root, fake_api, capsys):
    assert main(["--root", str(project_root), "data", "report"]) == 1
    assert "No 1Min bars stored yet" in capsys.readouterr().out


def test_broker_account(project_root, fake_api, capsys):
    assert main(["--root", str(project_root), "broker", "account"]) == 0
    out = capsys.readouterr().out
    assert "ACTIVE" in out and "market open now" in out


def test_config_error_exit_code(project_root):
    (project_root / "config" / "settings.local.toml").write_text('[app]\nmode = "live"\n')
    assert main(["--root", str(project_root), "check"]) == 2
