"""Read-only ESPN polling, retry, freshness, and health-event safeguards."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from time import sleep

from .contracts import ESPNLeagueReader, LeagueSnapshot
from .espn_adapter import (
    ESPNAccessDeniedError,
    ESPNAdapterError,
    ESPNTransientError,
)


class HealthEventKind(StrEnum):
    LOST_ACCESS = "lost_access"
    SERVICE_FAILURE = "service_failure"
    RECOVERED = "recovered"


@dataclass(frozen=True, slots=True)
class HealthEvent:
    kind: HealthEventKind
    urgent: bool
    message: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int = 3
    initial_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0

    def __post_init__(self) -> None:
        if (
            self.attempts < 1
            or self.initial_delay_seconds < 0
            or self.max_delay_seconds < 0
        ):
            raise ValueError(
                "retry policy values must be non-negative and attempts must be positive"
            )


@dataclass(frozen=True, slots=True)
class PollResult:
    snapshot: LeagueSnapshot | None
    events: tuple[HealthEvent, ...]
    attempts: int


class SnapshotIntegrityError(ESPNAdapterError):
    """Raised when a response is structurally incomplete or too stale."""


class ReadHealthMonitor:
    """Poll a reader and emit at most one alert per unhealthy interval."""

    def __init__(
        self,
        reader: ESPNLeagueReader,
        *,
        retry: RetryPolicy | None = None,
        max_age: timedelta = timedelta(hours=6),
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        wait: Callable[[float], None] = sleep,
    ) -> None:
        if max_age <= timedelta(0):
            raise ValueError("max_age must be positive")
        self._reader = reader
        self._retry = retry if retry is not None else RetryPolicy()
        self._max_age = max_age
        self._now = now
        self._wait = wait
        self._unhealthy = False

    def poll(self) -> PollResult:
        """Read until success or retry exhaustion; never call a write method."""

        last_error: ESPNAdapterError | None = None
        for attempt in range(1, self._retry.attempts + 1):
            try:
                snapshot = self._reader.read_snapshot()
                _validate_snapshot(snapshot, now=self._now(), max_age=self._max_age)
                events = self._recovery_event() if self._unhealthy else ()
                self._unhealthy = False
                return PollResult(snapshot=snapshot, events=events, attempts=attempt)
            except ESPNAccessDeniedError as exc:
                events = self._failure_event(HealthEventKind.LOST_ACCESS, str(exc))
                self._unhealthy = True
                return PollResult(
                    snapshot=None,
                    events=events,
                    attempts=attempt,
                )
            except (ESPNTransientError, SnapshotIntegrityError) as exc:
                last_error = exc
                if attempt < self._retry.attempts:
                    self._wait(
                        min(
                            self._retry.max_delay_seconds,
                            self._retry.initial_delay_seconds * (2 ** (attempt - 1)),
                        )
                    )
            except ESPNAdapterError as exc:
                last_error = exc
                break

        message = str(last_error) if last_error else "ESPN polling failed"
        events = self._failure_event(HealthEventKind.SERVICE_FAILURE, message)
        self._unhealthy = True
        return PollResult(
            snapshot=None,
            events=events,
            attempts=self._retry.attempts,
        )

    def _failure_event(
        self, kind: HealthEventKind, detail: str
    ) -> tuple[HealthEvent, ...]:
        if self._unhealthy:
            return ()
        return (
            HealthEvent(
                kind=kind,
                urgent=True,
                message=f"{kind.value}: {detail}",
                occurred_at=self._now(),
            ),
        )

    def _recovery_event(self) -> tuple[HealthEvent, ...]:
        return (
            HealthEvent(
                kind=HealthEventKind.RECOVERED,
                urgent=False,
                message="ESPN read access recovered",
                occurred_at=self._now(),
            ),
        )


def _validate_snapshot(
    snapshot: LeagueSnapshot,
    *,
    now: datetime,
    max_age: timedelta,
) -> None:
    if snapshot.settings.league_id <= 0 or not snapshot.settings.name:
        raise SnapshotIntegrityError("ESPN snapshot is missing league identity")
    if snapshot.source_timestamp.tzinfo is None:
        raise SnapshotIntegrityError("ESPN snapshot timestamp is not timezone-aware")
    age = now - snapshot.source_timestamp
    if age < timedelta(0) or age > max_age:
        raise SnapshotIntegrityError("ESPN snapshot is stale")
    required_sections: Sequence[object] = (
        snapshot.teams,
        snapshot.matchups,
        snapshot.status,
        snapshot.draft_picks,
        snapshot.transactions,
        snapshot.free_agents,
    )
    if any(section is None for section in required_sections):
        raise SnapshotIntegrityError("ESPN snapshot is incomplete")
