import shutil
from pathlib import Path

import keyring
import pytest

from trading_agent import credentials

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def fake_keychain(monkeypatch):
    """Never touch the real OS keychain or real env keys from tests."""
    store: dict[tuple[str, str], str] = {}

    def get_password(service, name):
        return store.get((service, name))

    def set_password(service, name, value):
        store[(service, name)] = value

    def delete_password(service, name):
        if (service, name) not in store:
            raise keyring.errors.PasswordDeleteError("not found")
        del store[(service, name)]

    monkeypatch.setattr(keyring, "get_password", get_password)
    monkeypatch.setattr(keyring, "set_password", set_password)
    monkeypatch.setattr(keyring, "delete_password", delete_password)
    for name in credentials.KNOWN_SECRETS:
        monkeypatch.delenv(name, raising=False)
    return store


@pytest.fixture
def project_root(tmp_path):
    """A throwaway project root holding a copy of the real settings.toml."""
    (tmp_path / "config").mkdir()
    shutil.copy(REPO_ROOT / "config" / "settings.toml", tmp_path / "config" / "settings.toml")
    return tmp_path
