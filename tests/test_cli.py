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


def test_config_error_exit_code(project_root):
    (project_root / "config" / "settings.local.toml").write_text('[app]\nmode = "live"\n')
    assert main(["--root", str(project_root), "check"]) == 2
