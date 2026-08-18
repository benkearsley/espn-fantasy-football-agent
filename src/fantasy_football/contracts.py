"""Application-owned, read-only league data contracts."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ScoringRule:
    stat: str
    points: float


@dataclass(frozen=True, slots=True)
class LeagueSettings:
    league_id: int
    name: str
    season: int
    scoring: tuple[ScoringRule, ...] = ()
    roster_slots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.league_id <= 0 or not self.name or self.season < 2000:
            raise ValueError("invalid league settings")


@dataclass(frozen=True, slots=True)
class Player:
    player_id: int
    name: str
    position: str | None = None
    pro_team: str | None = None


@dataclass(frozen=True, slots=True)
class RosterEntry:
    player: Player
    lineup_slot: str
    acquisition_type: str | None = None


@dataclass(frozen=True, slots=True)
class Team:
    team_id: int
    name: str
    owner_name: str | None = None
    wins: int = 0
    losses: int = 0
    ties: int = 0
    roster: tuple[RosterEntry, ...] = ()


@dataclass(frozen=True, slots=True)
class Matchup:
    matchup_id: int
    week: int
    home_team_id: int
    away_team_id: int
    home_score: float | None = None
    away_score: float | None = None
    status: str = "scheduled"


@dataclass(frozen=True, slots=True)
class Deadline:
    name: str
    at: datetime


@dataclass(frozen=True, slots=True)
class PlayerKickoff:
    """A normalized player game lock time, when the reader can establish one.

    ESPN readers that cannot safely provide this fact leave ``LeagueSnapshot``'s
    optional collection empty.  Approval policy must then fail closed rather
    than infer a lock time from free-form recommendation text.
    """

    player_id: int
    kickoff_at: datetime

    def __post_init__(self) -> None:
        if self.player_id <= 0:
            raise ValueError("player kickoff player_id must be positive")
        if self.kickoff_at.tzinfo is None:
            raise ValueError("player kickoff must be timezone-aware")


@dataclass(frozen=True, slots=True)
class LeagueStatus:
    current_week: int
    season_state: str
    deadlines: tuple[Deadline, ...] = ()


@dataclass(frozen=True, slots=True)
class DraftPick:
    pick_number: int
    round: int
    team_id: int
    player: Player


@dataclass(frozen=True, slots=True)
class Transaction:
    transaction_id: int
    kind: str
    status: str
    team_ids: tuple[int, ...] = ()
    player_ids: tuple[int, ...] = ()
    executed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FreeAgent:
    player: Player
    ownership_percent: float | None = None
    waiver_status: str | None = None


@dataclass(frozen=True, slots=True)
class LeagueSnapshot:
    settings: LeagueSettings
    teams: tuple[Team, ...]
    matchups: tuple[Matchup, ...]
    status: LeagueStatus
    draft_picks: tuple[DraftPick, ...]
    transactions: tuple[Transaction, ...]
    free_agents: tuple[FreeAgent, ...]
    source_timestamp: datetime
    player_kickoffs: tuple[PlayerKickoff, ...] = ()

    def __post_init__(self) -> None:
        if self.source_timestamp.tzinfo is None:
            raise ValueError("source_timestamp must be timezone-aware")

    def to_dict(self) -> dict[str, Any]:
        """Serialize the normalized snapshot without provider objects."""

        serialized = _serialize(self)
        assert isinstance(serialized, dict)
        return serialized


@runtime_checkable
class ESPNLeagueReader(Protocol):
    """Read-only interface used by onboarding and decision code."""

    def read_snapshot(self) -> LeagueSnapshot:
        """Retrieve the complete normalized current-season snapshot."""


def _serialize(value: object) -> Any:
    if is_dataclass(value):
        return {
            field.name: _serialize(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, tuple):
        return [_serialize(item) for item in value]
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value
