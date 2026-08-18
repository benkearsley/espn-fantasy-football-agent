"""Integration tests for restart-safe read-only supervised service behavior."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from fantasy_football.contracts import LeagueSettings, LeagueSnapshot, LeagueStatus
from fantasy_football.decisions import (
    DecisionCycle,
    ESPNFact,
    JsonlDecisionHistory,
    LeadRecommendation,
)
from fantasy_football.espn_adapter import ESPNAccessDeniedError
from fantasy_football.execution import RuntimeWriteState
from fantasy_football.health import ReadHealthMonitor, RetryPolicy
from fantasy_football.orchestration import (
    DecisionCycleResult,
    DecisionStatus,
    LeadManager,
)
from fantasy_football.service import (
    JsonOperationalStateStore,
    ServiceSchedule,
    SupervisedService,
)
from fantasy_football.telegram import CommandRouter
from fantasy_football.workflows import ManagerWorkflows


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def advance(self, amount: timedelta) -> None:
        self.value += amount


class SequenceReader:
    def __init__(self, values: list[LeagueSnapshot | Exception]) -> None:
        self.values = values
        self.calls = 0

    def read_snapshot(self) -> LeagueSnapshot:
        self.calls += 1
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class FakeTransport:
    def __init__(self, updates: list[dict[str, object]] | None = None) -> None:
        self.updates = updates or []
        self.messages: list[tuple[int, str]] = []
        self.offsets: list[int | None] = []

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, object]]:
        self.offsets.append(offset)
        assert timeout == 0
        return self.updates

    def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


class FakeLead:
    def __init__(self, history: JsonlDecisionHistory, clock: Clock) -> None:
        self.history = history
        self.clock = clock
        self.triggers: list[str] = []

    def run_cycle(
        self, snapshot: LeagueSnapshot, *, trigger: str
    ) -> DecisionCycleResult:
        self.triggers.append(trigger)
        sequence = len(self.triggers)
        now = self.clock.now()
        record = self.history.record_cycle(
            DecisionCycle(
                decision_id=f"decision-service-{sequence}",
                season=snapshot.settings.season,
                league_id=snapshot.settings.league_id,
                trigger=trigger,
                triggered_at=now,
                espn_facts=(ESPNFact("service", "normalized read", now),),
                specialist_opinions=(),
                recommendation=LeadRecommendation("No write requested", 0.8, "None"),
            )
        )
        return DecisionCycleResult(
            DecisionStatus.RECOMMENDATION_READY, "read-only recommendation", record
        )


def _snapshot(at: datetime) -> LeagueSnapshot:
    return LeagueSnapshot(
        settings=LeagueSettings(7, "Engine League", 2026),
        teams=(),
        matchups=(),
        status=LeagueStatus(2, "in_season"),
        draft_picks=(),
        transactions=(),
        free_agents=(),
        source_timestamp=at,
    )


def _service(
    tmp_path: Path,
    *,
    clock: Clock,
    reader: SequenceReader,
    transport: FakeTransport,
) -> tuple[SupervisedService, FakeLead, RuntimeWriteState]:
    history = JsonlDecisionHistory(tmp_path, 2026, clock=clock.now)
    lead = FakeLead(history, clock)
    state = RuntimeWriteState()
    holder: dict[str, SupervisedService] = {}
    workflows = ManagerWorkflows(
        decision_history=history,
        write_state=state,
        run_cycle=lambda: holder["service"].run_on_demand(),
        status_summary=lambda: holder["service"].status_summary(),
        clock=clock.now,
    )
    service = SupervisedService(
        monitor=ReadHealthMonitor(
            reader,
            retry=RetryPolicy(attempts=1),
            now=clock.now,
            wait=lambda _: None,
        ),
        lead_manager=cast(LeadManager, lead),
        decision_history=history,
        transport=transport,
        router=CommandRouter(42, workflows=workflows),
        write_state=state,
        state_store=JsonOperationalStateStore(tmp_path),
        notification_chat_id=42,
        polling_timeout_seconds=0,
        schedule=ServiceSchedule(
            monitor_interval=timedelta(minutes=5),
            idle_sleep_seconds=0,
            digest_hour_utc=13,
        ),
        clock=clock.now,
        wait=lambda _: None,
    )
    holder["service"] = service
    return service, lead, state


def test_scheduled_and_on_demand_cycles_restart_without_duplicate_updates_or_digest(
    tmp_path: Path,
) -> None:
    clock = Clock(datetime(2026, 9, 13, 14, tzinfo=UTC))
    first = _snapshot(clock.now())
    clock.advance(timedelta(minutes=5))
    second = _snapshot(clock.now())
    clock.advance(timedelta(minutes=5))
    third = _snapshot(clock.now())
    clock.advance(timedelta(minutes=5))
    fourth = _snapshot(clock.now())
    unchanged = _snapshot(fourth.source_timestamp)
    clock.value = first.source_timestamp
    updates = [
        {
            "update_id": 1,
            "message": {"from": {"id": 42}, "chat": {"id": 42}, "text": "pause"},
        },
        {
            "update_id": 2,
            "message": {"from": {"id": 42}, "chat": {"id": 42}, "text": "run"},
        },
    ]
    transport = FakeTransport(updates)
    service, lead, state = _service(
        tmp_path,
        clock=clock,
        reader=SequenceReader([first, second, third, fourth]),
        transport=transport,
    )

    service.run_once()  # /pause and /run; /run owns the first cycle.
    assert state.paused
    assert lead.triggers == ["Telegram run"]
    assert len(transport.messages) == 2

    clock.advance(timedelta(minutes=5))
    updates.append(
        {
            "update_id": 3,
            "message": {"from": {"id": 42}, "chat": {"id": 42}, "text": "run"},
        }
    )
    service.run_once()  # A second on-demand cycle owns the next monitor read.
    assert lead.triggers == ["Telegram run", "Telegram run"]

    clock.advance(timedelta(minutes=5))
    service.run_once()  # First scheduled cycle and the one daily digest.
    clock.advance(timedelta(minutes=5))
    service.run_once()  # A second scheduled cycle; no duplicate digest.
    assert lead.triggers == [
        "Telegram run",
        "Telegram run",
        "scheduled monitoring",
        "scheduled monitoring",
    ]
    assert sum("Daily digest" in text for _, text in transport.messages) == 1

    clock.advance(timedelta(minutes=5))
    restarted, restarted_lead, restarted_state = _service(
        tmp_path,
        clock=clock,
        reader=SequenceReader([unchanged]),
        transport=transport,
    )
    restarted.run_once()

    assert restarted_state.paused  # Global pause survived the process restart.
    assert restarted_lead.triggers == []  # Fingerprint prevents duplicate decision.
    assert len(transport.messages) == 4  # No replayed updates or second digest.
    assert JsonOperationalStateStore(tmp_path).load().telegram_offset == 4
    assert (tmp_path / "service-state.json").stat().st_mode & 0o777 == 0o600


def test_health_alerts_are_deduplicated_across_restart_and_recovery_is_sent(
    tmp_path: Path,
) -> None:
    clock = Clock(datetime(2026, 9, 13, 10, tzinfo=UTC))
    transport = FakeTransport()
    service, _, _ = _service(
        tmp_path,
        clock=clock,
        reader=SequenceReader([ESPNAccessDeniedError("session expired")]),
        transport=transport,
    )
    service.run_once()
    assert [text for _, text in transport.messages] == [
        "URGENT: lost_access: session expired"
    ]

    clock.advance(timedelta(minutes=5))
    restarted, _, _ = _service(
        tmp_path,
        clock=clock,
        reader=SequenceReader([_snapshot(clock.now())]),
        transport=transport,
    )
    restarted.run_once()
    assert [text for _, text in transport.messages][-1] == (
        "INFO: ESPN read access recovered"
    )
    assert restarted.readiness()["health"] == "healthy"


def test_shutdown_is_graceful_and_service_has_no_write_executor(tmp_path: Path) -> None:
    clock = Clock(datetime(2026, 9, 13, 10, tzinfo=UTC))
    service, _, _ = _service(
        tmp_path,
        clock=clock,
        reader=SequenceReader([_snapshot(clock.now())]),
        transport=FakeTransport(),
    )
    waits: list[float] = []

    def stop_after_wait(seconds: float) -> None:
        waits.append(seconds)
        service.request_shutdown()

    service._wait = stop_after_wait

    service.run_forever()

    assert service.shutdown_requested
    assert waits == [0]
    assert not hasattr(service, "_executor")
    assert "ESPNActionExecutor" not in inspect.getsource(SupervisedService)
