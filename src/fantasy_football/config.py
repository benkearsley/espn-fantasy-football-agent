"""Validated, non-secret runtime configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(ValueError):
    """Raised when required runtime configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    """Configuration needed to read one ESPN league.

    Credentials intentionally do not belong here. They are obtained through a
    :class:`~fantasy_football.secrets.SecretProvider` at the adapter boundary.
    """

    league_id: int
    season: int
    data_dir: Path
    write_enabled: bool = False
    log_raw_payloads: bool = False

    def __post_init__(self) -> None:
        if self.league_id <= 0:
            raise ConfigurationError("league_id must be a positive integer")
        if not 2000 <= self.season <= 2100:
            raise ConfigurationError("season must be between 2000 and 2100")
        if self.write_enabled:
            raise ConfigurationError(
                "ESPN writes are not supported by the read-only service"
            )
        if self.log_raw_payloads:
            raise ConfigurationError("raw ESPN payload logging is prohibited")
        if not self.data_dir.is_absolute():
            raise ConfigurationError("data_dir must be an absolute path")

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> ServiceConfig:
        """Load configuration from ``FFM_*`` environment variables."""

        values = os.environ if environ is None else environ
        required = {
            name: values.get(name)
            for name in ("FFM_LEAGUE_ID", "FFM_SEASON", "FFM_DATA_DIR")
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ConfigurationError(
                f"missing required configuration: {', '.join(missing)}"
            )
        try:
            league_id = int(required["FFM_LEAGUE_ID"] or "")
            season = int(required["FFM_SEASON"] or "")
        except ValueError as exc:
            raise ConfigurationError(
                "FFM_LEAGUE_ID and FFM_SEASON must be integers"
            ) from exc
        return cls(
            league_id=league_id,
            season=season,
            data_dir=Path(required["FFM_DATA_DIR"] or ""),
            write_enabled=_parse_bool(values.get("FFM_WRITE_ENABLED", "false")),
            log_raw_payloads=_parse_bool(values.get("FFM_LOG_RAW_PAYLOADS", "false")),
        )

    def ensure_secure_data_dir(self) -> Path:
        """Create the local data directory with owner-only permissions."""

        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.data_dir.chmod(0o700)
        mode = self.data_dir.stat().st_mode & 0o777
        if mode != 0o700:
            raise ConfigurationError("data_dir must be accessible only by its owner")
        return self.data_dir


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"invalid boolean value: {value!r}")
