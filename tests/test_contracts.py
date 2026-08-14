"""Tests for app-owned normalized league contracts."""

from datetime import UTC, datetime

from fantasy_football.contracts import (
    ESPNLeagueReader,
    LeagueSettings,
    LeagueSnapshot,
    LeagueStatus,
    Team,
)


def test_snapshot_serializes_without_provider_models() -> None:
    snapshot = LeagueSnapshot(
        settings=LeagueSettings(league_id=7, name="Engine League", season=2026),
        teams=(Team(team_id=1, name="Ben's Team"),),
        matchups=(),
        status=LeagueStatus(current_week=1, season_state="preseason"),
        draft_picks=(),
        transactions=(),
        free_agents=(),
        source_timestamp=datetime(2026, 8, 14, tzinfo=UTC),
    )
    serialized = snapshot.to_dict()
    assert serialized["settings"]["league_id"] == 7
    assert serialized["source_timestamp"] == "2026-08-14T00:00:00+00:00"


def test_reader_protocol_is_read_only() -> None:
    assert not hasattr(ESPNLeagueReader, "write")
    assert not hasattr(ESPNLeagueReader, "execute")
