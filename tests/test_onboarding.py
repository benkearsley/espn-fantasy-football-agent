"""Tests for the redacted onboarding command."""

from datetime import UTC, datetime
from pathlib import Path

from fantasy_football.config import ServiceConfig
from fantasy_football.contracts import (
    ESPNLeagueReader,
    LeagueSettings,
    LeagueSnapshot,
    LeagueStatus,
    PlayerKickoff,
    Team,
)
from fantasy_football.onboarding import run_onboarding


class FakeReader:
    def __init__(self, snapshot: LeagueSnapshot) -> None:
        self.snapshot = snapshot
        self.reads = 0

    def read_snapshot(self) -> LeagueSnapshot:
        self.reads += 1
        return self.snapshot


def test_onboarding_persists_only_redacted_metadata(tmp_path: Path) -> None:
    snapshot = LeagueSnapshot(
        settings=LeagueSettings(7, "Engine League", 2026),
        teams=(Team(1, "Ben's Team", owner_name="Ben"), Team(2, "Other Team")),
        matchups=(),
        status=LeagueStatus(1, "in_season"),
        draft_picks=(),
        transactions=(),
        free_agents=(),
        source_timestamp=datetime(2026, 8, 14, tzinfo=UTC),
        player_kickoffs=(PlayerKickoff(10, datetime(2026, 8, 16, 17, tzinfo=UTC)),),
    )
    reader = FakeReader(snapshot)
    summary_path = tmp_path / "onboarding-summary.json"
    summary = run_onboarding(
        ServiceConfig(7, 2026, tmp_path.absolute()),
        reader,
        output_path=summary_path,
    )

    assert isinstance(reader, ESPNLeagueReader)
    assert reader.reads == 1
    assert summary["owner"]["teams"][0]["roster_size"] == 0
    assert summary["player_kickoffs"] == [
        {"player_id": 10, "kickoff_at": "2026-08-16T17:00:00+00:00"}
    ]
    contents = summary_path.read_text(encoding="utf-8")
    assert "source_timestamp" in contents
    assert "schedule" not in contents
    assert "espn_s2" not in contents
    assert summary_path.stat().st_mode & 0o777 == 0o600
