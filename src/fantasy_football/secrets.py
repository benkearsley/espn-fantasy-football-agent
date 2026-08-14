"""Secret-provider boundary for authenticated ESPN read access."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True, repr=False)
class ESPNCredentials:
    """Cookie material required by the ESPN read client."""

    espn_s2: str
    swid: str
    browser_state_path: Path | None = None

    def __post_init__(self) -> None:
        if not self.espn_s2 or not self.swid:
            raise ValueError("ESPN credentials must contain espn_s2 and swid")

    def __repr__(self) -> str:
        return "ESPNCredentials(<redacted>)"


class SecretProvider(Protocol):
    """Source of credentials; callers cannot inspect the backing store."""

    def get_espn_credentials(self) -> ESPNCredentials:
        """Return credentials for a read-only ESPN session."""


class InMemorySecretProvider:
    """Deterministic provider for tests and local composition."""

    def __init__(self, credentials: ESPNCredentials) -> None:
        self._credentials = credentials

    def get_espn_credentials(self) -> ESPNCredentials:
        return self._credentials


class JsonFileSecretProvider:
    """Read credentials from an owner-only JSON file.

    A deployment may replace this with an encrypted store. Plaintext files are
    accepted only when the filesystem permissions are restrictive, and the
    file contents are never included in exceptions or representations.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def get_espn_credentials(self) -> ESPNCredentials:
        try:
            mode = self._path.stat().st_mode & 0o777
            if mode & 0o077:
                raise PermissionError("secret file permissions must be owner-only")
            with self._path.open(encoding="utf-8") as handle:
                values = json.load(handle)
            return ESPNCredentials(
                espn_s2=_required_string(values, "espn_s2"),
                swid=_required_string(values, "swid"),
                browser_state_path=_optional_path(values.get("browser_state_path")),
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise SecretProviderError(
                f"unable to load ESPN credentials from {self._path}"
            ) from exc


class SecretProviderError(RuntimeError):
    """Raised without exposing secret values or raw secret-file contents."""


def _required_string(values: object, key: str) -> str:
    if not isinstance(values, dict):
        raise ValueError(f"missing {key}")
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing {key}")
    return value


def _optional_path(value: object) -> Path | None:
    return Path(value) if isinstance(value, str) and value else None


def redact(value: object) -> str:
    """Return a safe diagnostic string for values that may contain secrets."""

    if isinstance(value, ESPNCredentials | bytes):
        return "<redacted>"
    text = str(value)
    return (
        "<redacted>"
        if any(
            token in text.lower() for token in ("espn_s2", "swid", "token", "cookie")
        )
        else text
    )
