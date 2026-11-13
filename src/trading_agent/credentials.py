"""API keys: read from environment or the OS keychain, never from code or config files.

Lookup order: environment variable (incl. a git-ignored .env loaded at startup), then the
OS keychain (Windows Credential Manager on this machine). Values are wrapped in `Secret`
so they don't leak into logs, tracebacks or reprs by accident.
"""

from __future__ import annotations

import os

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

SERVICE = "trading-agent"

# The only secret names the app knows about. Live keys are added in Phase 8, not before.
KNOWN_SECRETS: dict[str, str] = {
    "ALPACA_PAPER_API_KEY": "Alpaca paper-trading API key ID",
    "ALPACA_PAPER_SECRET_KEY": "Alpaca paper-trading secret key",
    "ANTHROPIC_API_KEY": "Claude API key (needed from Phase 5)",
}


class MissingSecretError(Exception):
    """A required secret is not set anywhere."""


class Secret:
    """Holds a secret value; prints as a mask. Call .reveal() only where the value is used."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('****')"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:  # avoid accidental comparisons with plain strings
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)


def _check_name(name: str) -> None:
    if name not in KNOWN_SECRETS:
        raise ValueError(f"Unknown secret {name!r}. Known: {', '.join(KNOWN_SECRETS)}")


def _from_keychain(name: str) -> str | None:
    try:
        return keyring.get_password(SERVICE, name)
    except KeyringError:
        return None


def secret_source(name: str) -> str | None:
    """Where a secret would be read from: 'env', 'keychain', or None if not set."""
    _check_name(name)
    if os.environ.get(name):
        return "env"
    if _from_keychain(name):
        return "keychain"
    return None


def get_secret(name: str) -> Secret:
    _check_name(name)
    value = os.environ.get(name) or _from_keychain(name)
    if not value:
        raise MissingSecretError(f"{name} is not set. Run: trading-agent secrets set {name}")
    return Secret(value)


def set_secret(name: str, value: str) -> None:
    _check_name(name)
    value = value.strip()
    if not value:
        raise ValueError("Refusing to store an empty secret")
    keyring.set_password(SERVICE, name, value)


def delete_secret(name: str) -> bool:
    """Remove a secret from the keychain. Returns False if it wasn't there."""
    _check_name(name)
    try:
        keyring.delete_password(SERVICE, name)
    except PasswordDeleteError:
        return False
    return True
