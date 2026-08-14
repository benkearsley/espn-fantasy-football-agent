"""Sanitized provider-boundary tests for the ESPN read adapter."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fantasy_football.config import ServiceConfig
from fantasy_football.espn_adapter import (
    ESPNAccessDeniedError,
    EspnApiReader,
    ESPNInvalidLeagueError,
    ESPNTransientError,
    ESPSchemaError,
)
from fantasy_football.secrets import ESPNCredentials, InMemorySecretProvider


def _player(player_id: int, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        playerId=player_id,
        name=name,
        position="QB",
        proTeam="BUF",
        lineupSlot="QB",
        acquisitionType="DRAFT",
        percent_owned=42.0,
        active_status="active",
    )


class FakeLeague:
    league_id = 123
    year = 2026
    current_week = 1
    season_state = "in_season"
    settings = SimpleNamespace(
        name="Engine League",
        scoring_format=[{"abbr": "pass_yds", "points": 0.04}],
        position_slot_counts={"QB": 1},
        trade_deadline=0,
    )
    teams = [
        SimpleNamespace(
            team_id=1,
            team_name="Ben's Team",
            owners=[{"displayName": "Ben"}],
            wins=1,
            losses=0,
            ties=0,
            roster=[_player(10, "Quarterback Example")],
        )
    ]
    draft: list[Any] = []

    finalScoringPeriod = 1

    def scoreboard(self, *, week: int) -> list[Any]:
        assert week == 1
        return []

    def transactions(self, *, types: set[str]) -> list[Any]:
        assert "TRADE_ACCEPT" in types
        return []

    def free_agents(self, *, size: int) -> list[Any]:
        assert size == 1000
        return [_player(11, "Free Agent Example")]


def _config() -> ServiceConfig:
    return ServiceConfig(123, 2026, Path("/tmp/fantasy-football-test").absolute())


def test_reader_maps_complete_sanitized_snapshot_and_never_enables_debug() -> None:
    calls: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> FakeLeague:
        calls.append(kwargs)
        return FakeLeague()

    snapshot = EspnApiReader(
        _config(),
        InMemorySecretProvider(ESPNCredentials("s2-secret", "swid-secret")),
        league_factory=factory,
    ).read_snapshot()

    assert snapshot.settings.name == "Engine League"
    assert snapshot.teams[0].owner_name == "Ben"
    assert snapshot.teams[0].roster[0].player.name == "Quarterback Example"
    assert snapshot.free_agents[0].player.player_id == 11
    assert calls == [
        {
            "league_id": 123,
            "year": 2026,
            "espn_s2": "s2-secret",
            "swid": "swid-secret",
            "debug": False,
        }
    ]


@pytest.mark.parametrize(
    ("message", "error"),
    [
        ("403 forbidden", ESPNAccessDeniedError),
        ("404 not found", ESPNInvalidLeagueError),
        ("connection timeout", ESPNTransientError),
        ("unexpected response", ESPSchemaError),
    ],
)
def test_provider_failures_are_classified_without_secret_leakage(
    message: str, error: type[RuntimeError]
) -> None:
    def factory(**kwargs: Any) -> FakeLeague:
        raise RuntimeError(f"{message}: s2-secret swid-secret")

    with pytest.raises(error) as raised:
        EspnApiReader(
            _config(),
            InMemorySecretProvider(ESPNCredentials("s2-secret", "swid-secret")),
            league_factory=factory,
        ).read_snapshot()
    assert "s2-secret" not in str(raised.value)
    assert "swid-secret" not in str(raised.value)
