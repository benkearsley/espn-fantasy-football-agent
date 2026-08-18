"""Durable, provider-neutral records for manager decision cycles.

The decision history deliberately sits beside (and only references) the action
executor ledger.  It stores the bounded explanation for a material decision;
the executor remains the sole owner of append-only evidence for an ESPN write.
No provider response, model object, or arbitrary payload is accepted here.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

from fantasy_football.execution import ActionRequest, ActionResult, redact_text

MAX_FACT_LENGTH = 1_000
MAX_REASONING_LENGTH = 2_000
MAX_ACTION_SUMMARY_LENGTH = 1_000
_ACTION_REFERENCE_PREFIX = "act-"


class PendingActionStatus(StrEnum):
    """The durable state of an action awaiting a human decision."""

    PENDING = "pending"
    APPROVED = "approved"
    VETOED = "vetoed"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class ESPNFact:
    """A bounded, redacted ESPN fact used by a decision.

    ``summary`` is intentionally not an ESPN payload.  A caller must reduce a
    provider response to one useful fact before constructing this value.
    """

    category: str
    summary: str
    observed_at: datetime

    def __post_init__(self) -> None:
        if not self.category.strip():
            raise ValueError("fact category must not be empty")
        _timezone_aware(self.observed_at, "fact observation")
        object.__setattr__(self, "category", self.category.strip())
        object.__setattr__(self, "summary", _safe_text(self.summary, MAX_FACT_LENGTH))

    def to_dict(self) -> dict[str, str]:
        return {
            "category": self.category,
            "summary": self.summary,
            "observed_at": _iso(self.observed_at),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ESPNFact:
        return cls(
            category=_string(value, "category"),
            summary=_string(value, "summary"),
            observed_at=_datetime(value.get("observed_at")),
        )


@dataclass(frozen=True, slots=True)
class SpecialistOpinion:
    """A specialist recommendation, including any disagreement with the lead."""

    specialist: str
    recommendation: str
    reasoning: str
    dissent: str = ""

    def __post_init__(self) -> None:
        if not self.specialist.strip():
            raise ValueError("specialist must not be empty")
        if not self.recommendation.strip():
            raise ValueError("specialist recommendation must not be empty")
        object.__setattr__(self, "specialist", self.specialist.strip())
        object.__setattr__(
            self,
            "recommendation",
            _safe_text(self.recommendation, MAX_REASONING_LENGTH),
        )
        object.__setattr__(
            self, "reasoning", _safe_text(self.reasoning, MAX_REASONING_LENGTH)
        )
        object.__setattr__(
            self, "dissent", _safe_text(self.dissent, MAX_REASONING_LENGTH)
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "specialist": self.specialist,
            "recommendation": self.recommendation,
            "reasoning": self.reasoning,
            "dissent": self.dissent,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> SpecialistOpinion:
        return cls(
            specialist=_string(value, "specialist"),
            recommendation=_string(value, "recommendation"),
            reasoning=_string(value, "reasoning"),
            dissent=_optional_string(value, "dissent"),
        )


@dataclass(frozen=True, slots=True)
class LeadRecommendation:
    """The Lead Manager's final recommendation and uncertainty."""

    summary: str
    confidence: float
    uncertainty: str

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between zero and one")
        object.__setattr__(
            self, "summary", _safe_text(self.summary, MAX_REASONING_LENGTH)
        )
        object.__setattr__(
            self, "uncertainty", _safe_text(self.uncertainty, MAX_REASONING_LENGTH)
        )
        if not self.summary:
            raise ValueError("lead recommendation must not be empty")

    def to_dict(self) -> dict[str, str | float]:
        return {
            "summary": self.summary,
            "confidence": self.confidence,
            "uncertainty": self.uncertainty,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> LeadRecommendation:
        confidence = value.get("confidence")
        if not isinstance(confidence, int | float):
            raise ValueError("recommendation confidence must be numeric")
        return cls(
            summary=_string(value, "summary"),
            confidence=float(confidence),
            uncertainty=_string(value, "uncertainty"),
        )


@dataclass(frozen=True, slots=True)
class PendingAction:
    """An opaque action reference that may be approved or vetoed by the owner."""

    action_id: str
    kind: str
    summary: str
    expires_at: datetime

    def __post_init__(self) -> None:
        _validate_action_id(self.action_id)
        if not self.kind.strip():
            raise ValueError("pending action kind must not be empty")
        _timezone_aware(self.expires_at, "pending action expiry")
        object.__setattr__(self, "kind", self.kind.strip())
        object.__setattr__(
            self, "summary", _safe_text(self.summary, MAX_ACTION_SUMMARY_LENGTH)
        )
        if not self.summary:
            raise ValueError("pending action summary must not be empty")

    @classmethod
    def create(
        cls,
        kind: str,
        summary: str,
        expires_at: datetime,
        *,
        action_id: str | None = None,
    ) -> PendingAction:
        """Create an opaque reference or preserve one from an upstream workflow."""

        return cls(action_id or new_action_id(), kind, summary, expires_at)

    def to_dict(self) -> dict[str, str]:
        return {
            "action_id": self.action_id,
            "kind": self.kind,
            "summary": self.summary,
            "expires_at": _iso(self.expires_at),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> PendingAction:
        return cls(
            action_id=_string(value, "action_id"),
            kind=_string(value, "kind"),
            summary=_string(value, "summary"),
            expires_at=_datetime(value.get("expires_at")),
        )


@dataclass(frozen=True, slots=True)
class ExecutionReference:
    """Safe link to an immutable entry in the executor's action ledger."""

    action_id: str
    idempotency_key: str

    def __post_init__(self) -> None:
        _validate_action_id(self.action_id)
        if not self.idempotency_key:
            raise ValueError("execution idempotency key must not be empty")

    @classmethod
    def from_request(cls, request: ActionRequest) -> ExecutionReference:
        """Build a link without copying the executor's sensitive action payload."""

        return cls(request.action_id, request.idempotency_key)

    def to_dict(self) -> dict[str, str]:
        return {"action_id": self.action_id, "idempotency_key": self.idempotency_key}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ExecutionReference:
        return cls(
            action_id=_string(value, "action_id"),
            idempotency_key=_string(value, "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class DecisionCycle:
    """The complete explainable record for one material monitoring decision."""

    decision_id: str
    season: int
    league_id: int
    trigger: str
    triggered_at: datetime
    espn_facts: tuple[ESPNFact, ...]
    specialist_opinions: tuple[SpecialistOpinion, ...]
    recommendation: LeadRecommendation
    pending_action: PendingAction | None = None
    execution: ExecutionReference | None = None

    def __post_init__(self) -> None:
        if not self.decision_id.strip():
            raise ValueError("decision id must not be empty")
        if not 2000 <= self.season <= 2100:
            raise ValueError("season must be between 2000 and 2100")
        if self.league_id <= 0:
            raise ValueError("league id must be positive")
        if not self.trigger.strip():
            raise ValueError("decision trigger must not be empty")
        _timezone_aware(self.triggered_at, "decision trigger")
        if not self.espn_facts:
            raise ValueError("a decision requires at least one ESPN fact")
        if self.execution is not None and self.pending_action is not None:
            if self.execution.action_id != self.pending_action.action_id:
                raise ValueError("execution reference must match the pending action")
        object.__setattr__(self, "decision_id", self.decision_id.strip())
        object.__setattr__(self, "trigger", _safe_text(self.trigger, MAX_FACT_LENGTH))
        object.__setattr__(self, "espn_facts", tuple(self.espn_facts))
        object.__setattr__(self, "specialist_opinions", tuple(self.specialist_opinions))

    def to_dict(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "season": self.season,
            "league_id": self.league_id,
            "trigger": self.trigger,
            "triggered_at": _iso(self.triggered_at),
            "espn_facts": [fact.to_dict() for fact in self.espn_facts],
            "specialist_opinions": [
                opinion.to_dict() for opinion in self.specialist_opinions
            ],
            "recommendation": self.recommendation.to_dict(),
            "pending_action": self.pending_action.to_dict()
            if self.pending_action
            else None,
            "execution": self.execution.to_dict() if self.execution else None,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> DecisionCycle:
        facts = value.get("espn_facts")
        opinions = value.get("specialist_opinions")
        recommendation = value.get("recommendation")
        pending = value.get("pending_action")
        execution = value.get("execution")
        if not isinstance(facts, Sequence) or isinstance(facts, str | bytes):
            raise ValueError("decision facts must be a sequence")
        if not isinstance(opinions, Sequence) or isinstance(opinions, str | bytes):
            raise ValueError("specialist opinions must be a sequence")
        if not isinstance(recommendation, Mapping):
            raise ValueError("decision recommendation is required")
        if pending is not None and not isinstance(pending, Mapping):
            raise ValueError("pending action must be a mapping")
        if execution is not None and not isinstance(execution, Mapping):
            raise ValueError("execution reference must be a mapping")
        return cls(
            decision_id=_string(value, "decision_id"),
            season=_integer(value, "season"),
            league_id=_integer(value, "league_id"),
            trigger=_string(value, "trigger"),
            triggered_at=_datetime(value.get("triggered_at")),
            espn_facts=tuple(
                ESPNFact.from_dict(_mapping(item, "ESPN fact")) for item in facts
            ),
            specialist_opinions=tuple(
                SpecialistOpinion.from_dict(_mapping(item, "specialist opinion"))
                for item in opinions
            ),
            recommendation=LeadRecommendation.from_dict(recommendation),
            pending_action=PendingAction.from_dict(pending) if pending else None,
            execution=ExecutionReference.from_dict(execution) if execution else None,
        )


@dataclass(frozen=True, slots=True)
class MeasuredOutcome:
    """A bounded outcome captured after a decision or execution has settled."""

    action_id: str
    measured_at: datetime
    summary: str
    execution_state: str | None = None

    def __post_init__(self) -> None:
        _validate_action_id(self.action_id)
        _timezone_aware(self.measured_at, "outcome measurement")
        object.__setattr__(
            self, "summary", _safe_text(self.summary, MAX_REASONING_LENGTH)
        )
        if not self.summary:
            raise ValueError("outcome summary must not be empty")
        if self.execution_state is not None:
            object.__setattr__(
                self, "execution_state", _safe_text(self.execution_state, 128)
            )

    @classmethod
    def from_execution_result(
        cls, result: ActionResult, *, summary: str | None = None
    ) -> MeasuredOutcome:
        """Link an executor result without copying its evidence or request payload."""

        return cls(
            action_id=result.action_id,
            measured_at=result.completed_at,
            summary=summary or result.reason,
            execution_state=result.state.value,
        )

    def to_dict(self) -> dict[str, str | None]:
        return {
            "action_id": self.action_id,
            "measured_at": _iso(self.measured_at),
            "summary": self.summary,
            "execution_state": self.execution_state,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> MeasuredOutcome:
        state = value.get("execution_state")
        if state is not None and not isinstance(state, str):
            raise ValueError("outcome execution state must be a string")
        return cls(
            action_id=_string(value, "action_id"),
            measured_at=_datetime(value.get("measured_at")),
            summary=_string(value, "summary"),
            execution_state=state,
        )


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """The restart-safe current view reconstructed from immutable history events."""

    cycle: DecisionCycle
    pending_status: PendingActionStatus | None = None
    pending_reason: str = ""
    pending_actor: str | None = None
    outcome: MeasuredOutcome | None = None

    @property
    def action_id(self) -> str | None:
        if self.cycle.pending_action is not None:
            return self.cycle.pending_action.action_id
        if self.cycle.execution is not None:
            return self.cycle.execution.action_id
        return None

    def is_pending(self, now: datetime) -> bool:
        if self.cycle.pending_action is None:
            return False
        return (
            self.pending_status is PendingActionStatus.PENDING
            and self.cycle.pending_action.expires_at > now
        )


@runtime_checkable
class DecisionHistory(Protocol):
    """Append-only decision history boundary used by future manager workflows."""

    def record_cycle(self, cycle: DecisionCycle) -> DecisionRecord:
        """Append a material decision exactly once."""

    def get(self, action_id: str) -> DecisionRecord | None:
        """Return a decision by its stable action reference, when it has one."""

    def get_decision(self, decision_id: str) -> DecisionRecord | None:
        """Return a decision even when it has no action awaiting execution."""

    @property
    def records(self) -> tuple[DecisionRecord, ...]:
        """All material decisions in durable insertion order."""

    def pending(self, *, now: datetime | None = None) -> tuple[DecisionRecord, ...]:
        """Return pending actions, durably expiring any that have elapsed."""

    def resolve_pending(
        self,
        action_id: str,
        status: PendingActionStatus,
        *,
        reason: str = "",
        actor_id: str | int | None = None,
        resolved_at: datetime | None = None,
    ) -> DecisionRecord:
        """Append an approval or veto; previously recorded decisions are unchanged."""

    def record_outcome(self, outcome: MeasuredOutcome) -> DecisionRecord:
        """Append a measured outcome linked to an existing decision action."""


class InMemoryDecisionHistory:
    """Deterministic append-only history for tests and local dry runs."""

    def __init__(
        self, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self._clock = clock
        self._records: dict[str, DecisionRecord] = {}
        self._decisions: dict[str, DecisionRecord] = {}
        self._decision_ids: set[str] = set()
        self._events: list[dict[str, object]] = []
        self._lock = threading.RLock()

    @property
    def events(self) -> tuple[Mapping[str, object], ...]:
        with self._lock:
            return tuple(
                cast(Mapping[str, object], dict(event)) for event in self._events
            )

    def record_cycle(self, cycle: DecisionCycle) -> DecisionRecord:
        with self._lock:
            if cycle.decision_id in self._decision_ids:
                raise ValueError("decision id is already recorded")
            action_id = _cycle_action_id(cycle)
            if action_id is not None and action_id in self._records:
                raise ValueError("action reference is already recorded")
            record = DecisionRecord(
                cycle=cycle,
                pending_status=(
                    PendingActionStatus.PENDING if cycle.pending_action else None
                ),
            )
            self._append_event(
                {
                    "event": "cycle_recorded",
                    "recorded_at": _iso(self._now()),
                    "cycle": cycle.to_dict(),
                }
            )
            self._install_cycle(record)
            return record

    def get(self, action_id: str) -> DecisionRecord | None:
        with self._lock:
            return self._records.get(action_id)

    def get_decision(self, decision_id: str) -> DecisionRecord | None:
        with self._lock:
            return self._decisions.get(decision_id)

    @property
    def records(self) -> tuple[DecisionRecord, ...]:
        """All material decisions, including cycles with no action to execute."""

        with self._lock:
            return tuple(self._decisions.values())

    def pending(self, *, now: datetime | None = None) -> tuple[DecisionRecord, ...]:
        with self._lock:
            current = now or self._now()
            self.expire_pending(now=current)
            return tuple(
                record
                for record in self._records.values()
                if record.is_pending(current)
            )

    def expire_pending(
        self, *, now: datetime | None = None
    ) -> tuple[DecisionRecord, ...]:
        with self._lock:
            current = now or self._now()
            _timezone_aware(current, "decision history clock")
            expired: list[DecisionRecord] = []
            for action_id, record in tuple(self._records.items()):
                pending = record.cycle.pending_action
                if (
                    pending is not None
                    and record.pending_status is PendingActionStatus.PENDING
                    and pending.expires_at <= current
                ):
                    expired.append(
                        self._resolve(
                            action_id,
                            PendingActionStatus.EXPIRED,
                            "",
                            None,
                            current,
                        )
                    )
            return tuple(expired)

    def resolve_pending(
        self,
        action_id: str,
        status: PendingActionStatus,
        *,
        reason: str = "",
        actor_id: str | int | None = None,
        resolved_at: datetime | None = None,
    ) -> DecisionRecord:
        if status not in {PendingActionStatus.APPROVED, PendingActionStatus.VETOED}:
            raise ValueError("only approvals and vetoes may be resolved explicitly")
        with self._lock:
            at = resolved_at or self._now()
            _timezone_aware(at, "pending action resolution")
            self.expire_pending(now=at)
            return self._resolve(action_id, status, reason, actor_id, at)

    def record_outcome(self, outcome: MeasuredOutcome) -> DecisionRecord:
        with self._lock:
            record = self._required(action=outcome.action_id)
            if record.outcome is not None:
                raise ValueError("an outcome is already recorded for this action")
            self._append_event(
                {
                    "event": "outcome_recorded",
                    "recorded_at": _iso(self._now()),
                    "outcome": outcome.to_dict(),
                }
            )
            updated = replace(record, outcome=outcome)
            self._records[outcome.action_id] = updated
            self._decisions[updated.cycle.decision_id] = updated
            return updated

    def record_execution_result(
        self, result: ActionResult, *, summary: str | None = None
    ) -> DecisionRecord:
        """Bridge an executor result while leaving its ledger as the evidence owner."""

        record = self._required(action=result.action_id)
        execution = record.cycle.execution
        if execution is None:
            raise ValueError("action has no recorded execution reference")
        if execution.idempotency_key != result.idempotency_key:
            raise ValueError("execution result idempotency key does not match decision")
        return self.record_outcome(
            MeasuredOutcome.from_execution_result(result, summary=summary)
        )

    def _resolve(
        self,
        action_id: str,
        status: PendingActionStatus,
        reason: str,
        actor_id: str | int | None,
        at: datetime,
    ) -> DecisionRecord:
        record = self._required(action=action_id)
        if record.cycle.pending_action is None:
            raise ValueError("action does not require approval or veto")
        if record.pending_status is not PendingActionStatus.PENDING:
            raise ValueError("pending action has already been resolved")
        safe_reason = _safe_text(reason, MAX_REASONING_LENGTH)
        safe_actor = str(actor_id) if actor_id is not None else None
        self._append_event(
            {
                "event": "pending_resolved",
                "recorded_at": _iso(at),
                "action_id": action_id,
                "status": status.value,
                "reason": safe_reason,
                "actor_id": safe_actor,
            }
        )
        updated = replace(
            record,
            pending_status=status,
            pending_reason=safe_reason,
            pending_actor=safe_actor,
        )
        self._records[action_id] = updated
        self._decisions[updated.cycle.decision_id] = updated
        return updated

    def _append_event(self, event: dict[str, object]) -> None:
        event["sequence"] = len(self._events) + 1
        self._events.append(event)

    def _install_cycle(self, record: DecisionRecord) -> None:
        self._decision_ids.add(record.cycle.decision_id)
        self._decisions[record.cycle.decision_id] = record
        action_id = _cycle_action_id(record.cycle)
        if action_id is not None:
            self._records[action_id] = record

    def _required(self, *, action: str) -> DecisionRecord:
        record = self._records.get(action)
        if record is None:
            raise KeyError(f"unknown action reference: {action}")
        return record

    def _now(self) -> datetime:
        value = self._clock()
        _timezone_aware(value, "decision history clock")
        return value


class JsonlDecisionHistory(InMemoryDecisionHistory):
    """Owner-only, season-partitioned append-only decision history.

    The JSONL file is the source of truth.  The in-memory records are always
    rebuilt from it at startup, making outstanding approval/veto work survive
    a service restart without a mutable side file.
    """

    def __init__(
        self,
        data_dir: Path,
        season: int,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not data_dir.is_absolute():
            raise ValueError("decision data_dir must be absolute")
        if not 2000 <= season <= 2100:
            raise ValueError("season must be between 2000 and 2100")
        super().__init__(clock=clock)
        self.path = data_dir / str(season) / "decision-history.jsonl"
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        if self.path.exists():
            self._load()
        self.path.touch(mode=0o600, exist_ok=True)
        os.chmod(self.path, 0o600)

    def _append_event(self, event: dict[str, object]) -> None:
        event["sequence"] = len(self._events) + 1
        payload = json.dumps(event, separators=(",", ":"), sort_keys=True)
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(self.path, 0o600)
        except (OSError, TypeError, ValueError) as exc:
            raise RuntimeError("unable to append decision history") from exc
        self._events.append(event)

    def _load(self) -> None:
        try:
            with self.path.open(encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        self._replay(_mapping(json.loads(line), "decision event"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
            raise RuntimeError("decision history is unreadable") from exc

    def _replay(self, event: Mapping[str, object]) -> None:
        sequence = _integer(event, "sequence")
        if sequence != len(self._events) + 1:
            raise ValueError("decision history sequence must be append-only")
        kind = _string(event, "event")
        if kind == "cycle_recorded":
            cycle = DecisionCycle.from_dict(
                _mapping(event.get("cycle"), "decision cycle")
            )
            if cycle.decision_id in self._decision_ids:
                raise ValueError("decision id is already recorded")
            action_id = _cycle_action_id(cycle)
            if action_id is not None and action_id in self._records:
                raise ValueError("action reference is already recorded")
            self._install_cycle(
                DecisionRecord(
                    cycle=cycle,
                    pending_status=(
                        PendingActionStatus.PENDING if cycle.pending_action else None
                    ),
                )
            )
        elif kind == "pending_resolved":
            action_id = _string(event, "action_id")
            status = PendingActionStatus(_string(event, "status"))
            record = self._required(action=action_id)
            if (
                record.cycle.pending_action is None
                or record.pending_status is not PendingActionStatus.PENDING
            ):
                raise ValueError("invalid pending action transition")
            updated = replace(
                record,
                pending_status=status,
                pending_reason=_optional_string(event, "reason"),
                pending_actor=_optional_none_string(event, "actor_id"),
            )
            self._records[action_id] = updated
            self._decisions[updated.cycle.decision_id] = updated
        elif kind == "outcome_recorded":
            outcome = MeasuredOutcome.from_dict(
                _mapping(event.get("outcome"), "outcome")
            )
            record = self._required(action=outcome.action_id)
            if record.outcome is not None:
                raise ValueError("an outcome is already recorded for this action")
            updated = replace(record, outcome=outcome)
            self._records[outcome.action_id] = updated
            self._decisions[updated.cycle.decision_id] = updated
        else:
            raise ValueError("unknown decision history event")
        self._events.append(dict(event))


def new_action_id() -> str:
    """Return a Telegram-safe opaque action reference with no provider semantics."""

    return _ACTION_REFERENCE_PREFIX + secrets.token_hex(16)


def _cycle_action_id(cycle: DecisionCycle) -> str | None:
    if cycle.pending_action is not None:
        return cycle.pending_action.action_id
    if cycle.execution is not None:
        return cycle.execution.action_id
    return None


def _validate_action_id(value: str) -> None:
    if not value.startswith(_ACTION_REFERENCE_PREFIX) or len(value) < 12:
        raise ValueError("action reference must be an opaque act- identifier")
    if not all(
        character.isascii() and (character.isalnum() or character in "-_")
        for character in value
    ):
        raise ValueError("action reference contains unsupported characters")


def _safe_text(value: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError("stored text must be a string")
    return redact_text(value)[:maximum].strip()


def _timezone_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None:
        raise ValueError(f"{name} timestamp must be timezone-aware")


def _iso(value: datetime) -> str:
    return value.isoformat()


def _datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO string")
    parsed = datetime.fromisoformat(value)
    _timezone_aware(parsed, "stored")
    return parsed


def _string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise ValueError(f"{key} must be a string")
    return item


def _optional_string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key, "")
    if not isinstance(item, str):
        raise ValueError(f"{key} must be a string")
    return item


def _optional_none_string(value: Mapping[str, object], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str):
        raise ValueError(f"{key} must be a string or null")
    return item


def _integer(value: Mapping[str, object], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool):
        raise ValueError(f"{key} must be an integer")
    return item


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return cast(Mapping[str, object], value)
