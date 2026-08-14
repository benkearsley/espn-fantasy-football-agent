"""Tests for configuration and secret boundaries."""

from pathlib import Path

import pytest

from fantasy_football.config import ConfigurationError, ServiceConfig
from fantasy_football.secrets import ESPNCredentials, InMemorySecretProvider, redact


def test_config_requires_values_and_disables_writes() -> None:
    with pytest.raises(ConfigurationError, match="missing required"):
        ServiceConfig.from_env({})
    with pytest.raises(ConfigurationError, match="writes"):
        ServiceConfig(
            league_id=1,
            season=2026,
            data_dir=Path("/tmp/data").absolute(),
            write_enabled=True,
        )


def test_config_loads_safe_values() -> None:
    config = ServiceConfig.from_env(
        {"FFM_LEAGUE_ID": "123", "FFM_SEASON": "2026", "FFM_DATA_DIR": "/tmp/ffm"}
    )
    assert config.league_id == 123
    assert config.write_enabled is False


def test_credentials_are_redacted_and_provider_is_deterministic() -> None:
    credentials = ESPNCredentials("secret-s2", "secret-swid")
    assert "secret" not in repr(credentials)
    assert InMemorySecretProvider(credentials).get_espn_credentials() == credentials
    assert redact("cookie=secret-s2") == "<redacted>"
