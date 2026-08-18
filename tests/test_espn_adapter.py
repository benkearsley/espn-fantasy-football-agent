"""Sanitized provider-boundary tests for the ESPN read adapter."""

from datetime import UTC, datetime, timedelta, timezone
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


def _player(
    player_id: int,
    name: str,
    *,
    schedule: object | None = None,
) -> SimpleNamespace:
    values: dict[str, object] = {
        "playerId": player_id,
        "name": name,
        "position": "QB",
        "proTeam": "BUF",
        "lineupSlot": "QB",
        "acquisitionType": "DRAFT",
        "percent_owned": 42.0,
        "active_status": "active",
    }
    if schedule is not None:
        values["schedule"] = schedule
    return SimpleNamespace(**values)


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


def test_reader_normalizes_current_week_roster_kickoffs_and_deduplicates() -> None:
    aware = datetime(2026, 9, 13, 16, 25, tzinfo=timezone(timedelta(hours=-4)))
    local_naive = datetime(2026, 9, 13, 11, 30)
    duplicate = _player(
        12,
        "Duplicate Example",
        schedule={
            1: {"team": "BUF", "date": aware},
            "1": {"team": "BUF", "date": aware},
        },
    )
    league = FakeLeague()
    league.teams = [
        SimpleNamespace(
            team_id=1,
            team_name="Ben's Team",
            owners=[{"displayName": "Ben"}],
            wins=1,
            losses=0,
            ties=0,
            roster=[
                _player(
                    10,
                    "Aware Example",
                    schedule={
                        1: {"team": "BUF", "date": aware},
                        2: {
                            "team": "BUF",
                            "date": datetime(2026, 9, 20, 12, 0),
                        },
                    },
                ),
                _player(
                    11,
                    "Local Example",
                    schedule={1: {"team": "BUF", "date": local_naive}},
                ),
                duplicate,
                duplicate,
            ],
        )
    ]

    snapshot = EspnApiReader(
        _config(),
        InMemorySecretProvider(ESPNCredentials("s2-secret", "swid-secret")),
        league_factory=lambda **kwargs: league,
    ).read_snapshot()

    assert [(item.player_id, item.kickoff_at) for item in snapshot.player_kickoffs] == [
        (10, aware.astimezone(UTC)),
        (11, local_naive.astimezone(UTC)),
        (12, aware.astimezone(UTC)),
    ]


def test_reader_omits_missing_bye_malformed_and_ambiguous_schedule_facts() -> None:
    date_one = datetime(2026, 9, 13, 12, tzinfo=UTC)
    date_two = datetime(2026, 9, 13, 15, tzinfo=UTC)
    players = [
        _player(20, "Missing Example"),
        _player(21, "Bye Example", schedule={1: {"team": "BYE"}}),
        _player(
            22,
            "Malformed Example",
            schedule={1: {"team": "BUF", "date": "bad"}},
        ),
        _player(23, "Malformed Container", schedule=[]),
        _player(24, "Other Week", schedule={2: {"team": "BUF", "date": date_one}}),
        _player(
            25,
            "Ambiguous Example",
            schedule={
                1: {"team": "BUF", "date": date_one},
                "1": {"team": "BUF", "date": date_two},
            },
        ),
    ]
    league = FakeLeague()
    league.teams = [
        SimpleNamespace(
            team_id=1,
            team_name="Ben's Team",
            owners=[{"displayName": "Ben"}],
            wins=1,
            losses=0,
            ties=0,
            roster=players,
        )
    ]

    snapshot = EspnApiReader(
        _config(),
        InMemorySecretProvider(ESPNCredentials("s2-secret", "swid-secret")),
        league_factory=lambda **kwargs: league,
    ).read_snapshot()

    assert snapshot.player_kickoffs == ()


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
