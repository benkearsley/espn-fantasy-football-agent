"""User-assisted ESPN session capture and permission-restricted storage.

This module deliberately does not automate login or accept an ESPN password.
The caller supplies cookies/state obtained from a temporary interactive browser
session after the user has completed login.
"""

from __future__ import annotations

import json
import os
import tempfile
from argparse import ArgumentParser
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .secrets import ESPNCredentials, SecretProviderError


class SessionError(RuntimeError):
    """Raised without exposing cookie values or browser state."""


@dataclass(frozen=True, slots=True)
class SessionStatus:
    exists: bool
    usable: bool
    expires_at: datetime | None = None


class ESPNSessionStore:
    """Persist the minimum interactive session state in an owner-only file."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("session path must be absolute")
        self._path = path

    @property
    def path(self) -> Path:
        """Return the path without reading or exposing session contents."""

        return self._path

    def capture(
        self,
        cookies: list[dict[str, Any]],
        *,
        browser_state: dict[str, Any] | None = None,
    ) -> None:
        """Save browser-produced session material atomically.

        ``cookies`` should come from an interactive browser context. Only the
        two cookies required by the read adapter are retained; optional browser
        state is retained for a future reauthentication flow.
        """

        values = _required_cookies(cookies)
        payload: dict[str, Any] = {
            "captured_at": datetime.now(UTC).isoformat(),
            "cookies": values,
        }
        if browser_state is not None:
            payload["browser_state"] = browser_state
        self._write(payload)

    def status(self) -> SessionStatus:
        """Report only existence/usability and cookie expiry metadata."""

        if not self._path.exists():
            return SessionStatus(exists=False, usable=False)
        try:
            payload = self._read()
            credentials = self._credentials(payload)
            expires_at = _expires_at(payload)
            usable = bool(credentials) and (
                expires_at is None or expires_at > datetime.now(UTC)
            )
            return SessionStatus(exists=True, usable=usable, expires_at=expires_at)
        except (
            OSError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
            KeyError,
            SessionError,
        ):
            return SessionStatus(exists=True, usable=False)

    def get_espn_credentials(self) -> ESPNCredentials:
        """Return credentials for the read adapter without logging them."""

        try:
            return self._credentials(self._read())
        except (
            OSError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
            KeyError,
            SessionError,
        ) as exc:
            raise SecretProviderError("stored ESPN session is unavailable") from exc

    def get_credentials(self) -> ESPNCredentials:
        """Backward-compatible alias for the SecretProvider method."""

        return self.get_espn_credentials()

    def clear(self) -> None:
        """Explicitly remove stored session material for reauthentication."""

        try:
            self._path.unlink(missing_ok=True)
        except OSError as exc:
            raise SessionError("unable to clear stored ESPN session") from exc

    def _write(self, payload: dict[str, Any]) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._path.parent.chmod(0o700)
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                delete=False,
            ) as handle:
                temporary_path = handle.name
                os.chmod(handle.name, 0o600)
                json.dump(payload, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self._path)
            os.chmod(self._path, 0o600)
        except (OSError, TypeError, ValueError) as exc:
            raise SessionError("unable to persist ESPN session") from exc
        finally:
            if temporary_path is not None:
                try:
                    Path(temporary_path).unlink(missing_ok=True)
                except OSError:
                    pass

    def _read(self) -> dict[str, Any]:
        mode = self._path.stat().st_mode & 0o777
        if mode & 0o077:
            raise SessionError("stored ESPN session permissions are too broad")
        with self._path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise SessionError("stored ESPN session has invalid structure")
        return payload

    @staticmethod
    def _credentials(payload: dict[str, Any]) -> ESPNCredentials:
        cookies = payload.get("cookies")
        if not isinstance(cookies, dict):
            raise SessionError("stored ESPN session has no credentials")
        return ESPNCredentials(
            espn_s2=_string_cookie(cookies, "espn_s2"),
            swid=_string_cookie(cookies, "SWID"),
        )


def _required_cookies(cookies: list[dict[str, Any]]) -> dict[str, str]:
    values: dict[str, str] = {}
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if name in {"espn_s2", "SWID"} and isinstance(value, str) and value:
            values[name] = value
    if "espn_s2" not in values or "SWID" not in values:
        raise SessionError("interactive ESPN session did not provide required cookies")
    return values


def _string_cookie(cookies: dict[str, Any], name: str) -> str:
    value = cookies.get(name)
    if not isinstance(value, str) or not value:
        raise SessionError("stored ESPN session is missing required cookies")
    return value


def _expires_at(payload: dict[str, Any]) -> datetime | None:
    cookies = payload.get("cookies")
    if not isinstance(cookies, dict):
        return None
    # Browser cookie expiry is not retained by capture; sessions are therefore
    # considered usable until ESPN rejects them and status remains conservative.
    _ = cookies
    return None


def main() -> None:
    """Show or clear the local interactive ESPN session."""

    parser = ArgumentParser(prog="fantasy-football-session")
    parser.add_argument("command", choices=("status", "clear"))
    parser.add_argument("--path", type=Path, required=True)
    args = parser.parse_args()
    store = ESPNSessionStore(args.path)
    if args.command == "clear":
        store.clear()
        print("ESPN session cleared")
        return
    status = store.status()
    expiry = status.expires_at.isoformat() if status.expires_at else "unknown"
    print(f"exists={status.exists} usable={status.usable} expires_at={expiry}")
