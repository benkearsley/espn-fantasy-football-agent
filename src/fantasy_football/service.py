"""Restart-safe, read-only supervised service composition.

This module is deliberately an application boundary, not an ESPN execution
adapter.  It polls a read-only ``ReadHealthMonitor``, asks a ``LeadManager``
to persist a recommendation, and delivers Telegram control-plane messages.
No action executor, browser port, or mutation method is constructed here.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from argparse import ArgumentParser
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import sleep
from typing import Protocol

from .config import ConfigurationError, ServiceConfig
from .contracts import LeagueSnapshot
from .decisions import DecisionHistory, JsonlDecisionHistory
from .espn_adapter import EspnApiReader
from .execution import RuntimeWriteState, redact_text
from .health import HealthEvent, HealthEventKind, ReadHealthMonitor
from .model_analysis import (
    ModelClientFactory,
    ModelRuntimeConfig,
    build_analysis_port,
)
from .orchestration import (
    AnalysisLimits,
    AnalysisPort,
    AnalysisRequest,
    AnalysisRole,
    DecisionCycleResult,
    LeadManager,
)
from .session import ESPNSessionStore
from .telegram import (
    CommandRouter,
    TelegramConfig,
    TelegramTransport,
    TelegramTransportProtocol,
    decode_update,
)
from .workflows import ManagerWorkflows


class OperationalStateError(RuntimeError):
    """Raised when owner-only service state is unavailable or malformed."""


@dataclass(frozen=True, slots=True)
class OperationalState:
    """The minimal durable state needed to make restart safe.

    Snapshot contents, Telegram messages, credentials, and any execution data
    are intentionally excluded.  The snapshot fingerprint is one-way and is
    used solely to avoid re-running an unchanged read after a restart.
    """

    telegram_offset: int | None = None
    last_digest_date: str | None = None
    last_monitor_at: str | None = None
    last_snapshot_fingerprint: str | None = None
    health_unhealthy: bool = False
    global_paused: bool = False

    def __post_init__(self) -> None:
        if self.telegram_offset is not None and self.telegram_offset < 0:
            raise OperationalStateError("Telegram offset must not be negative")
        if self.last_digest_date is not None:
            try:
                datetime.fromisoformat(self.last_digest_date).date()
            except ValueError as exc:
                raise OperationalStateError("digest marker is invalid") from exc
        if self.last_monitor_at is not None:
            value = datetime.fromisoformat(self.last_monitor_at)
            if value.tzinfo is None:
                raise OperationalStateError("monitor timestamp must be timezone-aware")
        if (
            self.last_snapshot_fingerprint is not None
            and len(self.last_snapshot_fingerprint) != 64
        ):
            raise OperationalStateError("snapshot fingerprint is invalid")

    @property
    def monitor_at(self) -> datetime | None:
        return (
            datetime.fromisoformat(self.last_monitor_at)
            if self.last_monitor_at is not None
            else None
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> OperationalState:
        expected = set(cls.__dataclass_fields__)
        if set(value) != expected:
            raise OperationalStateError("service state has an unexpected schema")
        offset = value["telegram_offset"]
        digest = value["last_digest_date"]
        monitored = value["last_monitor_at"]
        fingerprint = value["last_snapshot_fingerprint"]
        unhealthy = value["health_unhealthy"]
        paused = value["global_paused"]
        if not isinstance(offset, int | type(None)) or isinstance(offset, bool):
            raise OperationalStateError("Telegram offset is invalid")
        if not isinstance(digest, str | type(None)):
            raise OperationalStateError("digest marker is invalid")
        if not isinstance(monitored, str | type(None)):
            raise OperationalStateError("monitor timestamp is invalid")
        if not isinstance(fingerprint, str | type(None)):
            raise OperationalStateError("snapshot fingerprint is invalid")
        if not isinstance(unhealthy, bool) or not isinstance(paused, bool):
            raise OperationalStateError("service state flags are invalid")
        return cls(offset, digest, monitored, fingerprint, unhealthy, paused)


class OperationalStateStore(Protocol):
    """Persistent state boundary intentionally narrower than an action ledger."""

    def load(self) -> OperationalState: ...

    def save(self, state: OperationalState) -> None: ...


class JsonOperationalStateStore:
    """Atomically persist service state in a local owner-only JSON file."""

    def __init__(self, data_dir: Path) -> None:
        if not data_dir.is_absolute():
            raise ValueError("service data_dir must be absolute")
        self.path = data_dir / "service-state.json"
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        self._lock = threading.RLock()

    def load(self) -> OperationalState:
        with self._lock:
            if not self.path.exists():
                return OperationalState()
            try:
                if self.path.stat().st_mode & 0o077:
                    raise OperationalStateError(
                        "service state permissions are too broad"
                    )
                with self.path.open(encoding="utf-8") as handle:
                    raw = json.load(handle)
                if not isinstance(raw, dict):
                    raise OperationalStateError("service state is not an object")
                return OperationalState.from_dict(raw)
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                if isinstance(exc, OperationalStateError):
                    raise
                raise OperationalStateError("service state is unavailable") from exc

    def save(self, state: OperationalState) -> None:
        encoded = json.dumps(asdict(state), sort_keys=True, separators=(",", ":"))
        temporary_path: str | None = None
        with self._lock:
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.path.parent,
                    prefix=f".{self.path.name}.",
                    delete=False,
                ) as handle:
                    temporary_path = handle.name
                    os.chmod(temporary_path, 0o600)
                    handle.write(encoded)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_path, self.path)
                os.chmod(self.path, 0o600)
            except OSError as exc:
                raise OperationalStateError("unable to persist service state") from exc
            finally:
                if temporary_path is not None:
                    try:
                        Path(temporary_path).unlink(missing_ok=True)
                    except OSError:
                        pass


@dataclass(frozen=True, slots=True)
class ServiceSchedule:
    """Bounded polling and one-per-UTC-day digest schedule."""

    monitor_interval: timedelta = timedelta(minutes=5)
    idle_sleep_seconds: float = 1.0
    digest_hour_utc: int = 13
    digest_minute_utc: int = 0

    def __post_init__(self) -> None:
        if self.monitor_interval <= timedelta(0):
            raise ValueError("monitor interval must be positive")
        if self.idle_sleep_seconds < 0:
            raise ValueError("idle sleep must not be negative")
        if not 0 <= self.digest_hour_utc <= 23 or not 0 <= self.digest_minute_utc <= 59:
            raise ValueError("digest time must be a valid UTC time")


@dataclass(frozen=True, slots=True)
class CycleRun:
    """One monitor pass, including a snapshot for a possible daily digest."""

    snapshot: LeagueSnapshot | None
    result: DecisionCycleResult | None
    message: str


class SupervisedService:
    """Coordinate monitoring, Telegram, and read-only decision cycles.

    Calls are serialized with a non-blocking cycle lock.  A ``run`` Telegram
    command can therefore never overlap a scheduled cycle or create competing
    decision records.  Telegram offset and pause state are saved eagerly so a
    normal process restart cannot replay already-handled commands.
    """

    def __init__(
        self,
        *,
        monitor: ReadHealthMonitor,
        lead_manager: LeadManager,
        decision_history: DecisionHistory,
        transport: TelegramTransportProtocol,
        router: CommandRouter,
        write_state: RuntimeWriteState,
        state_store: OperationalStateStore,
        notification_chat_id: int | None,
        polling_timeout_seconds: int = 30,
        schedule: ServiceSchedule | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        wait: Callable[[float], None] = sleep,
    ) -> None:
        if not 0 <= polling_timeout_seconds <= 50:
            raise ValueError("Telegram polling timeout must be between 0 and 50")
        self._monitor = monitor
        self._lead_manager = lead_manager
        self._history = decision_history
        self._transport = transport
        self._router = router
        self._write_state = write_state
        self._state_store = state_store
        self._notification_chat_id = notification_chat_id
        self._polling_timeout_seconds = polling_timeout_seconds
        self._schedule = schedule or ServiceSchedule()
        self._clock = clock
        self._wait = wait
        self._state = state_store.load()
        if self._state.global_paused:
            self._write_state.pause()
        self._cycle_lock = threading.Lock()
        self._shutdown = threading.Event()
        self._latest_snapshot: LeagueSnapshot | None = None
        self._last_error: str | None = None

    def run_forever(self) -> None:
        """Run until ``request_shutdown`` is called or an interrupt arrives."""

        try:
            while not self._shutdown.is_set():
                self.run_once()
                if not self._shutdown.is_set():
                    self._wait(self._schedule.idle_sleep_seconds)
        except KeyboardInterrupt:
            self.request_shutdown()

    def request_shutdown(self) -> None:
        """Request a clean stop without discarding persisted state."""

        self._shutdown.set()
        self._persist_state()

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown.is_set()

    def run_once(self) -> None:
        """Process commands, run a due monitor pass, and send a due digest."""

        try:
            self._poll_telegram()
            now = self._now()
            if self._monitor_due(now):
                cycle = self._run_cycle("scheduled monitoring")
                if cycle.snapshot is not None:
                    self._maybe_send_digest(cycle.snapshot, now=now)
        except Exception as exc:  # supervisor loop stays alive after a safe alert
            self._last_error = redact_text(str(exc)) or "service iteration failed"
            self._record_service_failure(self._last_error)

    def run_on_demand(self) -> DecisionCycleResult | str:
        """Run the same serialized read-only cycle requested by Telegram."""

        cycle = self._run_cycle("Telegram run")
        if cycle.result is not None:
            return cycle.result
        return cycle.message

    def readiness(self) -> dict[str, object]:
        """Return supervisor-safe diagnostics with no secrets or raw payloads."""

        deadlines: list[dict[str, str]] = []
        if self._latest_snapshot is not None:
            deadlines = [
                {"name": deadline.name, "at": deadline.at.astimezone(UTC).isoformat()}
                for deadline in sorted(
                    self._latest_snapshot.status.deadlines, key=lambda value: value.at
                )
            ]
        return {
            "ready": (
                self._latest_snapshot is not None and not self._state.health_unhealthy
            ),
            "health": "unhealthy" if self._state.health_unhealthy else "healthy",
            "writes": "disabled",
            "paused": self._write_state.paused,
            "telegram_offset": self._state.telegram_offset,
            "last_monitor_at": self._state.last_monitor_at,
            "last_digest_date": self._state.last_digest_date,
            "next_deadlines": deadlines,
            "shutdown_requested": self._shutdown.is_set(),
            "last_error": self._last_error,
        }

    def status_summary(self) -> str:
        """A compact, redacted status summary suitable for ``status``."""

        readiness = self.readiness()
        deadline_text = ""
        if self._latest_snapshot is not None:
            deadline_text = ", ".join(
                f"{deadline.name} at {deadline.at.astimezone(UTC).isoformat()}"
                for deadline in sorted(
                    self._latest_snapshot.status.deadlines,
                    key=lambda value: value.at,
                )
            )
        lines = [
            f"Service health: {readiness['health']}; ESPN writes: disabled.",
            f"Last monitor: {readiness['last_monitor_at'] or 'not yet completed'}.",
        ]
        lines.append(f"Next deadlines: {deadline_text or 'none normalized'}.")
        return "\n".join(lines)

    def _poll_telegram(self) -> None:
        updates = self._transport.get_updates(
            self._state.telegram_offset, self._polling_timeout_seconds
        )
        for raw in updates:
            raw_id = raw.get("update_id")
            if not isinstance(raw_id, int):
                continue
            if (
                self._state.telegram_offset is not None
                and raw_id < self._state.telegram_offset
            ):
                continue
            update = decode_update(raw)
            if update is not None:
                reply = self._router.handle(update.user_id, update.text)
                self._persist_pause_state()
                if reply is not None:
                    self._transport.send_message(update.chat_id, reply)
            self._state = _replace_state(self._state, telegram_offset=raw_id + 1)
            self._persist_state()

    def _monitor_due(self, now: datetime) -> bool:
        last = self._state.monitor_at
        return last is None or now - last >= self._schedule.monitor_interval

    def _run_cycle(self, trigger: str) -> CycleRun:
        if not self._cycle_lock.acquire(blocking=False):
            return CycleRun(None, None, "A monitoring cycle is already running.")
        try:
            now = self._now()
            poll = self._monitor.poll()
            self._state = _replace_state(self._state, last_monitor_at=now.isoformat())
            self._handle_poll_health(poll.snapshot, poll.events, now)
            self._persist_state()
            if poll.snapshot is None:
                return CycleRun(
                    None, None, "ESPN read failed closed; retrying quietly."
                )
            self._latest_snapshot = poll.snapshot
            fingerprint = _snapshot_fingerprint(poll.snapshot)
            if fingerprint == self._state.last_snapshot_fingerprint:
                return CycleRun(
                    poll.snapshot,
                    None,
                    "Unchanged ESPN snapshot already has a persisted decision.",
                )
            result = self._lead_manager.run_cycle(poll.snapshot, trigger=trigger)
            if result.record is None:
                self._last_error = redact_text(result.reason)
                return CycleRun(poll.snapshot, result, result.reason)
            self._state = _replace_state(
                self._state, last_snapshot_fingerprint=fingerprint
            )
            self._persist_state()
            return CycleRun(poll.snapshot, result, result.reason)
        finally:
            self._cycle_lock.release()

    def _handle_poll_health(
        self,
        snapshot: LeagueSnapshot | None,
        events: tuple[HealthEvent, ...],
        now: datetime,
    ) -> None:
        if snapshot is not None:
            if self._state.health_unhealthy:
                self._notify_health(
                    HealthEvent(
                        HealthEventKind.RECOVERED,
                        False,
                        "ESPN read access recovered",
                        now,
                    )
                )
                self._state = _replace_state(self._state, health_unhealthy=False)
            return
        event = (
            events[0]
            if events
            else HealthEvent(
                HealthEventKind.SERVICE_FAILURE,
                True,
                "ESPN polling failed",
                now,
            )
        )
        self._record_service_failure(event.message, kind=event.kind, occurred_at=now)

    def _record_service_failure(
        self,
        message: str,
        *,
        kind: HealthEventKind = HealthEventKind.SERVICE_FAILURE,
        occurred_at: datetime | None = None,
    ) -> None:
        if self._state.health_unhealthy:
            return
        now = occurred_at or self._now()
        self._notify_health(HealthEvent(kind, True, redact_text(message), now))
        self._state = _replace_state(self._state, health_unhealthy=True)
        self._persist_state()

    def _notify_health(self, event: HealthEvent) -> None:
        if self._notification_chat_id is not None:
            try:
                self._transport.send_message(
                    self._notification_chat_id,
                    f"{'URGENT' if event.urgent else 'INFO'}: {event.message}",
                )
            except Exception as exc:
                # A broken notification path cannot be repaired by retrying it
                # in a tight loop. Persist the unhealthy interval and let the
                # supervisor keep the read-only service alive.
                self._last_error = redact_text(str(exc)) or "health alert failed"

    def _maybe_send_digest(self, snapshot: LeagueSnapshot, *, now: datetime) -> None:
        marker = now.astimezone(UTC).date().isoformat()
        due_at = now.astimezone(UTC).replace(
            hour=self._schedule.digest_hour_utc,
            minute=self._schedule.digest_minute_utc,
            second=0,
            microsecond=0,
        )
        if self._notification_chat_id is None or now.astimezone(UTC) < due_at:
            return
        if self._state.last_digest_date == marker:
            return
        pending = tuple(
            record.cycle.pending_action
            for record in self._history.pending(now=now)
            if record.cycle.pending_action is not None
        )
        lines = [
            (
                f"Daily digest — {snapshot.settings.name} "
                f"(week {snapshot.status.current_week})"
            ),
            f"Teams: {len(snapshot.teams)} | Matchups: {len(snapshot.matchups)}",
        ]
        if pending:
            lines.append("Pending actions:")
            lines.extend(
                f"- {action.summary} [{action.action_id}] "
                f"(expires {action.expires_at.astimezone(UTC).isoformat()})"
                for action in pending
            )
        else:
            lines.append("No pending approvals.")
        self._transport.send_message(self._notification_chat_id, "\n".join(lines))
        self._state = _replace_state(self._state, last_digest_date=marker)
        self._persist_state()

    def _persist_pause_state(self) -> None:
        if self._state.global_paused != self._write_state.paused:
            self._state = _replace_state(
                self._state, global_paused=self._write_state.paused
            )
            self._persist_state()

    def _persist_state(self) -> None:
        self._state_store.save(self._state)

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("service clock must return a timezone-aware time")
        return now


def _replace_state(state: OperationalState, **changes: object) -> OperationalState:
    values = asdict(state)
    values.update(changes)
    return OperationalState.from_dict(values)


def _snapshot_fingerprint(snapshot: LeagueSnapshot) -> str:
    """Hash normalized data only; never persist an ESPN payload or snapshot."""

    encoded = json.dumps(snapshot.to_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class _UnavailableAnalysisPort(AnalysisPort):
    """Fail closed until a separately configured model-provider adapter exists."""

    def analyze(self, request: AnalysisRequest) -> str:
        _ = request
        return "{}"


def build_runtime(
    config: ServiceConfig,
    telegram_config: TelegramConfig,
    *,
    session_path: Path,
    schedule: ServiceSchedule | None = None,
    model_config: ModelRuntimeConfig | None = None,
    model_client_factory: ModelClientFactory | None = None,
) -> SupervisedService:
    """Compose the live read-only service without a browser or write adapter.

    Model configuration is supplied only at runtime.  When it is absent, the
    bounded unavailable port persists a safe no-action decision and constructs
    no model client.  A configured provider adapter remains behind
    ``AnalysisPort``; ESPN reads, Telegram commands, health alerts, offsets,
    and digests remain otherwise unchanged and supervised.
    """

    data_dir = config.ensure_secure_data_dir()
    history = JsonlDecisionHistory(data_dir, config.season)
    write_state = RuntimeWriteState()
    state_store = JsonOperationalStateStore(data_dir)
    reader = EspnApiReader(config, ESPNSessionStore(session_path))
    monitor = ReadHealthMonitor(reader)
    analysis_limits = AnalysisLimits()
    analysis_port = build_analysis_port(
        model_config,
        limits=analysis_limits,
        client_factory=model_client_factory,
    )
    if analysis_port is None:
        analysis_port = _UnavailableAnalysisPort()
    lead_manager = LeadManager(
        analysis_ports={role: analysis_port for role in AnalysisRole},
        decision_history=history,
        limits=analysis_limits,
    )
    transport = TelegramTransport(telegram_config)
    holder: dict[str, SupervisedService] = {}
    workflows = ManagerWorkflows(
        decision_history=history,
        write_state=write_state,
        run_cycle=lambda: holder["service"].run_on_demand(),
        status_summary=lambda: holder["service"].status_summary(),
    )
    service = SupervisedService(
        monitor=monitor,
        lead_manager=lead_manager,
        decision_history=history,
        transport=transport,
        router=CommandRouter(telegram_config.allowed_user_id, workflows=workflows),
        write_state=write_state,
        state_store=state_store,
        notification_chat_id=telegram_config.chat_id,
        polling_timeout_seconds=telegram_config.polling_timeout_seconds,
        schedule=schedule,
    )
    holder["service"] = service
    return service


def main() -> None:
    """Run the supervised read-only Raspberry Pi service."""

    parser = ArgumentParser(prog="fantasy-football")
    parser.add_argument("--once", action="store_true", help="run one service pass")
    parser.add_argument(
        "--readiness", action="store_true", help="print redacted diagnostics and exit"
    )
    parser.add_argument("--session-path", type=Path)
    args = parser.parse_args()
    try:
        config = ServiceConfig.from_env()
        session_path = args.session_path or _session_path_from_env()
        if not session_path.is_absolute():
            raise ConfigurationError("session path must be absolute")
        service = build_runtime(
            config,
            TelegramConfig.from_env(),
            session_path=session_path,
            schedule=_schedule_from_env(),
            model_config=ModelRuntimeConfig.from_env(),
        )
    except (ConfigurationError, OperationalStateError, ValueError) as exc:
        raise SystemExit(
            f"service configuration failed: {redact_text(str(exc))}"
        ) from None
    if args.readiness:
        print(json.dumps(service.readiness(), sort_keys=True))
        return
    if args.once:
        service.run_once()
        print(json.dumps(service.readiness(), sort_keys=True))
        return
    service.run_forever()


def _session_path_from_env() -> Path:
    value = os.environ.get("FFM_SESSION_PATH")
    if not value:
        raise ConfigurationError("missing FFM_SESSION_PATH or --session-path")
    return Path(value)


def _schedule_from_env() -> ServiceSchedule:
    try:
        monitor_seconds = float(os.environ.get("FFM_MONITOR_INTERVAL_SECONDS", "300"))
        digest_hour = int(os.environ.get("FFM_DIGEST_HOUR_UTC", "13"))
        digest_minute = int(os.environ.get("FFM_DIGEST_MINUTE_UTC", "0"))
    except ValueError as exc:
        raise ConfigurationError("service scheduling values must be numeric") from exc
    return ServiceSchedule(
        monitor_interval=timedelta(seconds=monitor_seconds),
        digest_hour_utc=digest_hour,
        digest_minute_utc=digest_minute,
    )
