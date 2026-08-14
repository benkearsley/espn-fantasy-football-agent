"""Tests for retry, freshness, and deduplicated health events."""

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fantasy_football.contracts import LeagueSettings, LeagueSnapshot, LeagueStatus
from fantasy_football.espn_adapter import ESPNAccessDeniedError, ESPNTransientError
from fantasy_football.health import (
    HealthEventKind,
    ReadHealthMonitor,
    RetryPolicy,
)


def _snapshot(at: datetime) -> LeagueSnapshot:
    return LeagueSnapshot(
        settings=LeagueSettings(7, "Engine League", 2026),
        teams=(),
        matchups=(),
        status=LeagueStatus(1, "in_season"),
        draft_picks=(),
        transactions=(),
        free_agents=(),
        source_timestamp=at,
    )


class SequenceReader:
    def __init__(self, values: list[Any]) -> None:
        self.values = values

    def read_snapshot(self) -> LeagueSnapshot:
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return cast(LeagueSnapshot, value)


def test_transient_failures_backoff_then_recover() -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    waits: list[float] = []
    reader = SequenceReader(
        [
            ESPNTransientError("temporary"),
            ESPNTransientError("temporary"),
            _snapshot(now),
        ]
    )
    result = ReadHealthMonitor(
        reader,
        retry=RetryPolicy(attempts=3, initial_delay_seconds=1, max_delay_seconds=5),
        now=lambda: now,
        wait=waits.append,
    ).poll()
    assert result.snapshot is not None
    assert result.attempts == 3
    assert waits == [1, 2]
    assert result.events == ()


def test_access_alert_is_deduplicated_and_recovery_is_emitted() -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    reader = SequenceReader(
        [
            ESPNAccessDeniedError("expired"),
            ESPNAccessDeniedError("expired"),
            _snapshot(now),
        ]
    )
    monitor = ReadHealthMonitor(reader, now=lambda: now, wait=lambda _: None)

    first = monitor.poll()
    second = monitor.poll()
    recovered = monitor.poll()
    assert first.events[0].kind == HealthEventKind.LOST_ACCESS
    assert second.events == ()
    assert recovered.events[0].kind == HealthEventKind.RECOVERED
    assert recovered.events[0].urgent is False


def test_stale_snapshot_fails_closed_with_one_service_event() -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    reader = SequenceReader(
        [_snapshot(now - timedelta(days=1)), _snapshot(now - timedelta(days=1))]
    )
    monitor = ReadHealthMonitor(
        reader,
        retry=RetryPolicy(attempts=2, initial_delay_seconds=0),
        now=lambda: now,
        wait=lambda _: None,
    )
    result = monitor.poll()
    assert result.snapshot is None
    assert result.events[0].kind == HealthEventKind.SERVICE_FAILURE
    assert result.events[0].urgent is True
