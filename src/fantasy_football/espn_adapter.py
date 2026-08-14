"""Read-only adapter around the maintained ``espn-api`` package."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from espn_api.football import League  # type: ignore[import-untyped]

from .config import ServiceConfig
from .contracts import (
    Deadline,
    DraftPick,
    ESPNLeagueReader,
    FreeAgent,
    LeagueSettings,
    LeagueSnapshot,
    LeagueStatus,
    Matchup,
    Player,
    RosterEntry,
    ScoringRule,
    Team,
    Transaction,
)
from .secrets import SecretProvider


class ESPNAdapterError(RuntimeError):
    """Base class for sanitized ESPN access failures."""


class ESPNAccessDeniedError(ESPNAdapterError):
    """The session is missing, expired, or not authorized for the league."""


class ESPNInvalidLeagueError(ESPNAdapterError):
    """The league identifier or season is not valid."""


class ESPNTransientError(ESPNAdapterError):
    """A retryable network or ESPN service failure."""


class ESPSchemaError(ESPNAdapterError):
    """The provider returned data the adapter could not normalize."""


class LeagueLike(Protocol):
    """Small structural view of the third-party League object for testing."""

    league_id: int
    year: int
    current_week: int


LeagueFactory = Callable[..., LeagueLike]


class EspnApiReader(ESPNLeagueReader):
    """Fetch and normalize a complete league snapshot without write methods."""

    def __init__(
        self,
        config: ServiceConfig,
        secrets: SecretProvider,
        *,
        league_factory: LeagueFactory = League,
        free_agent_limit: int = 1000,
    ) -> None:
        if free_agent_limit <= 0:
            raise ValueError("free_agent_limit must be positive")
        self._config = config
        self._secrets = secrets
        self._league_factory = league_factory
        self._free_agent_limit = free_agent_limit

    def read_snapshot(self) -> LeagueSnapshot:
        """Construct a fresh provider object and map all read-only sections."""

        credentials = self._secrets.get_espn_credentials()
        try:
            league = self._league_factory(
                league_id=self._config.league_id,
                year=self._config.season,
                espn_s2=credentials.espn_s2,
                swid=credentials.swid,
                debug=False,
            )
            return self._snapshot_from_league(league)
        except ESPNAdapterError:
            raise
        except Exception as exc:
            raise _classify_provider_error(exc) from exc

    def _snapshot_from_league(self, league: LeagueLike) -> LeagueSnapshot:
        try:
            settings = _settings(league, self._config)
            teams = tuple(_team(team) for team in _items(league, "teams"))
            matchups = _matchups(league)
            draft_picks = tuple(_draft_pick(pick) for pick in _items(league, "draft"))
            transactions = tuple(
                _transaction(transaction)
                for transaction in _call(
                    league,
                    "transactions",
                    types={
                        "FREEAGENT",
                        "WAIVER",
                        "WAIVER_ERROR",
                        "TRADE_ACCEPT",
                        "TRADE_VETO",
                        "TRADE_PROPOSAL",
                        "TRADE_UPHOLD",
                        "TRADE_DECLINE",
                        "TRADE_ERROR",
                    },
                )
            )
            free_agents = tuple(
                _free_agent(player)
                for player in _call(league, "free_agents", size=self._free_agent_limit)
            )
            status = LeagueStatus(
                current_week=int(league.current_week),
                season_state=str(getattr(league, "season_state", "in_season")),
                deadlines=_deadlines(league),
            )
            return LeagueSnapshot(
                settings=settings,
                teams=teams,
                matchups=matchups,
                status=status,
                draft_picks=draft_picks,
                transactions=transactions,
                free_agents=free_agents,
                source_timestamp=datetime.now(UTC),
            )
        except (AttributeError, KeyError, TypeError, ValueError, IndexError) as exc:
            raise ESPSchemaError(
                "ESPN response did not match the expected schema"
            ) from exc


def _settings(league: Any, config: ServiceConfig) -> LeagueSettings:
    settings = league.settings
    scoring = tuple(
        ScoringRule(
            stat=str(item.get("abbr", item.get("label", "unknown"))),
            points=float(item.get("points", 0)),
        )
        for item in getattr(settings, "scoring_format", ())
    )
    roster_slots = tuple(
        str(slot) for slot in getattr(settings, "position_slot_counts", {}).keys()
    )
    return LeagueSettings(
        league_id=config.league_id,
        name=str(settings.name),
        season=config.season,
        scoring=scoring,
        roster_slots=roster_slots,
    )


def _team(team: Any) -> Team:
    roster = tuple(
        RosterEntry(
            player=_player(player),
            lineup_slot=str(getattr(player, "lineupSlot", "")),
            acquisition_type=getattr(player, "acquisitionType", None),
        )
        for player in getattr(team, "roster", ())
    )
    owners = getattr(team, "owners", ())
    owner_name = _owner_name(owners[0]) if owners else None
    return Team(
        team_id=int(team.team_id),
        name=str(team.team_name),
        owner_name=owner_name,
        wins=int(team.wins),
        losses=int(team.losses),
        ties=int(team.ties),
        roster=roster,
    )


def _owner_name(owner: Any) -> str | None:
    if isinstance(owner, dict):
        value = owner.get("displayName") or owner.get("firstName")
    else:
        value = getattr(owner, "displayName", None)
    return str(value) if value else None


def _player(player: Any) -> Player:
    return Player(
        player_id=int(player.playerId),
        name=str(player.name),
        position=getattr(player, "position", None) or None,
        pro_team=getattr(player, "proTeam", None) or None,
    )


def _matchup(matchup: Any, week: int) -> Matchup:
    home = matchup.home_team
    away = matchup.away_team
    return Matchup(
        matchup_id=int(getattr(matchup, "matchup_id", 0)),
        week=week,
        home_team_id=int(home.team_id),
        away_team_id=int(away.team_id),
        home_score=float(getattr(matchup, "home_score", 0)),
        away_score=float(getattr(matchup, "away_score", 0)),
        status="complete" if getattr(matchup, "winner", None) else "scheduled",
    )


def _matchups(league: Any) -> tuple[Matchup, ...]:
    final_week = int(
        getattr(league, "finalScoringPeriod", getattr(league, "current_week", 0))
    )
    matchups: list[Matchup] = []
    for week in range(1, final_week + 1):
        matchups.extend(
            _matchup(matchup, week)
            for matchup in _call(league, "scoreboard", week=week)
        )
    return tuple(matchups)


def _draft_pick(pick: Any) -> DraftPick:
    return DraftPick(
        pick_number=int(pick.round_pick),
        round=int(pick.round_num),
        team_id=int(pick.team.team_id),
        player=Player(player_id=int(pick.playerId), name=str(pick.playerName)),
    )


def _transaction(transaction: Any) -> Transaction:
    items = tuple(item.playerId for item in getattr(transaction, "items", ()))
    team = getattr(transaction, "team", None)
    return Transaction(
        transaction_id=int(getattr(transaction, "transaction_id", 0)),
        kind=str(transaction.type),
        status=str(transaction.status),
        team_ids=(int(team.team_id),) if team is not None else (),
        player_ids=tuple(int(player_id) for player_id in items),
        executed_at=_timestamp(getattr(transaction, "date", None)),
    )


def _free_agent(player: Any) -> FreeAgent:
    return FreeAgent(
        player=_player(player),
        ownership_percent=getattr(player, "percent_owned", None),
        waiver_status=getattr(player, "active_status", None),
    )


def _deadlines(league: Any) -> tuple[Deadline, ...]:
    deadline = getattr(getattr(league, "settings", None), "trade_deadline", 0)
    timestamp = _timestamp(deadline)
    return (Deadline(name="trade", at=timestamp),) if timestamp else ()


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    number = float(value)
    if number > 10_000_000_000:
        number /= 1000
    return datetime.fromtimestamp(number, tz=UTC)


def _items(value: Any, attribute: str) -> list[Any]:
    items = getattr(value, attribute)
    return list(items)


def _call(value: Any, method: str, **kwargs: Any) -> list[Any]:
    return list(getattr(value, method)(**kwargs))


def _classify_provider_error(exc: Exception) -> ESPNAdapterError:
    text = str(exc).lower()
    if any(
        token in text
        for token in ("401", "403", "unauthorized", "forbidden", "private")
    ):
        return ESPNAccessDeniedError(
            "ESPN access denied; reauthenticate the read session"
        )
    if any(token in text for token in ("404", "not found", "invalid league")):
        return ESPNInvalidLeagueError("ESPN league or season was not found")
    if any(
        token in text
        for token in ("timeout", "timed out", "connection", "temporarily", "503", "502")
    ):
        return ESPNTransientError("ESPN request failed temporarily; retry later")
    return ESPSchemaError("ESPN response could not be normalized")
