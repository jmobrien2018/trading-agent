import pytest

from trading_agent import credentials
from trading_agent.credentials import MissingSecretError, Secret

NAME = "ALPACA_PAPER_API_KEY"


def test_secret_never_prints_its_value():
    s = Secret("super-secret-value")
    assert "super-secret-value" not in repr(s)
    assert "super-secret-value" not in str(s)
    assert "super-secret-value" not in f"{s}"
    assert s.reveal() == "super-secret-value"


def test_missing_secret_raises_with_hint():
    with pytest.raises(MissingSecretError, match="secrets set"):
        credentials.get_secret(NAME)
    assert credentials.secret_source(NAME) is None


def test_keychain_round_trip():
    credentials.set_secret(NAME, "  abc123  ")
    assert credentials.get_secret(NAME).reveal() == "abc123"
    assert credentials.secret_source(NAME) == "keychain"
    assert credentials.delete_secret(NAME) is True
    assert credentials.delete_secret(NAME) is False


def test_env_var_wins_over_keychain(monkeypatch):
    credentials.set_secret(NAME, "from-keychain")
    monkeypatch.setenv(NAME, "from-env")
    assert credentials.get_secret(NAME).reveal() == "from-env"
    assert credentials.secret_source(NAME) == "env"


def test_unknown_name_rejected():
    with pytest.raises(ValueError, match="Unknown secret"):
        credentials.set_secret("ALPACA_LIVE_API_KEY", "x")


def test_empty_value_rejected():
    with pytest.raises(ValueError, match="empty"):
        credentials.set_secret(NAME, "   ")
