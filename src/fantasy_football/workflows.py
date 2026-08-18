"""Persistent application workflows for the Telegram control plane.

This boundary translates a Telegram command into durable manager state.  It
does not accept a transport, credentials, ESPN adapter, or execution port.
In particular, recording an approval here is intentionally separate from
building an :class:`~fantasy_football.execution.ActionRequest`; a future write
workflow must still pass the default-deny ``ActionAuthorizer``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from .decisions import DecisionHistory, DecisionRecord, PendingActionStatus
from .execution import RuntimeWriteState
from .orchestration import DecisionCycleResult


class CommandWorkflow(Protocol):
    """Transport-independent handlers used by :class:`CommandRouter`."""

    def status(self) -> str: ...

    def run(self) -> str: ...

    def why(self, decision_id: str = "") -> str: ...

    def approve(self, action_id: str, *, actor_id: int) -> str: ...

    def veto(self, action_id: str, reason: str, *, actor_id: int) -> str: ...

    def pause(self) -> str: ...

    def resume(self) -> str: ...


class ManagerWorkflows:
    """Durable command handlers around decision history and write controls.

    ``run_cycle`` is injected because acquiring an ESPN snapshot belongs to
    the supervised service.  Its result has already been persisted by the
    Lead Manager before it is reported here.
    """

    def __init__(
        self,
        *,
        decision_history: DecisionHistory,
        write_state: RuntimeWriteState,
        run_cycle: Callable[[], DecisionCycleResult | str] | None = None,
        status_summary: Callable[[], str] = lambda: "No current roster snapshot.",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._history = decision_history
        self._write_state = write_state
        self._run_cycle = run_cycle
        self._status_summary = status_summary
        self._clock = clock
        # Resolving an action is a read-then-append transition.  Serialize this
        # application operation so duplicates in concurrent update processing
        # cannot produce an unnecessary failure or second event.
        self._lock = threading.RLock()

    def status(self) -> str:
        now = self._now()
        pending = self._history.pending(now=now)
        write_mode = "paused" if self._write_state.paused else "active"
        global_mode = "enabled" if self._write_state.global_enabled else "disabled"
        lines = [
            self._status_summary().strip() or "No current roster snapshot.",
            f"ESPN writes: {write_mode}; global write switch: {global_mode}.",
        ]
        if pending:
            lines.append("Pending decisions:")
            lines.extend(_pending_line(record) for record in pending)
        else:
            lines.append("No pending decisions.")
        return "\n".join(lines)

    def run(self) -> str:
        if self._run_cycle is None:
            return "No monitoring cycle is configured."
        result = self._run_cycle()
        if isinstance(result, str):
            return result
        if result.record is None:
            return f"Monitoring cycle failed closed: {result.reason}."
        return (
            f"Monitoring cycle complete: {result.status.value} "
            f"({result.decision_id}). {result.reason}."
        )

    def why(self, decision_id: str = "") -> str:
        with self._lock:
            record = (
                self._history.get_decision(decision_id)
                if decision_id
                else _latest_record(self._history)
            )
        if record is None:
            return "No persisted Lead Manager recommendation is available."
        return _render_why(record)

    def approve(self, action_id: str, *, actor_id: int) -> str:
        return self._resolve(
            action_id,
            PendingActionStatus.APPROVED,
            "",
            actor_id,
        )

    def veto(self, action_id: str, reason: str, *, actor_id: int) -> str:
        rationale = reason.strip()
        if not rationale:
            return "Usage: veto <action-id> <reason>"
        return self._resolve(
            action_id,
            PendingActionStatus.VETOED,
            rationale,
            actor_id,
        )

    def pause(self) -> str:
        self._write_state.pause()
        return "ESPN writes paused; monitoring continues."

    def resume(self) -> str:
        self._write_state.resume()
        mode = "enabled" if self._write_state.global_enabled else "disabled"
        return f"ESPN write pause lifted; global write switch remains {mode}."

    def _resolve(
        self,
        action_id: str,
        status: PendingActionStatus,
        reason: str,
        actor_id: int,
    ) -> str:
        identifier = action_id.strip()
        if not identifier:
            verb = "approve" if status is PendingActionStatus.APPROVED else "veto"
            suffix = " <reason>" if verb == "veto" else ""
            return f"Usage: {verb} <action-id>{suffix}"
        with self._lock:
            now = self._now()
            # This writes an expiry event, when needed, before resolving.  It
            # also makes expiry survive a restart through JsonlDecisionHistory.
            self._history.pending(now=now)
            record = self._history.get(identifier)
            if record is None:
                return "No pending action matches that identifier."
            if record.pending_status is PendingActionStatus.EXPIRED:
                return f"{identifier} has expired; no action taken."
            if record.pending_status is status:
                return _duplicate_reply(identifier, status, record)
            if record.pending_status is not PendingActionStatus.PENDING:
                return _already_resolved_reply(identifier, record)
            try:
                self._history.resolve_pending(
                    identifier,
                    status,
                    reason=reason,
                    actor_id=actor_id,
                    resolved_at=now,
                )
            except (KeyError, ValueError):
                # A shared history may have been completed between the lookup
                # and append.  Re-read and return an idempotent outcome.
                current = self._history.get(identifier)
                if current is None:
                    return "No pending action matches that identifier."
                if current.pending_status is PendingActionStatus.EXPIRED:
                    return f"{identifier} has expired; no action taken."
                if current.pending_status is status:
                    return _duplicate_reply(identifier, status, current)
                return _already_resolved_reply(identifier, current)
        if status is PendingActionStatus.APPROVED:
            return (
                f"Approval recorded for {identifier}; intent is stored, but "
                "ESPN writes still require separate authorization."
            )
        return f"Veto recorded for {identifier}; rationale saved for learning."

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("workflow clock must return a timezone-aware time")
        return now


def _latest_record(history: DecisionHistory) -> DecisionRecord | None:
    records = history.records
    return records[-1] if records else None


def _pending_line(record: DecisionRecord) -> str:
    action = record.cycle.pending_action
    assert action is not None
    return (
        f"- {action.summary} [{action.action_id}] "
        f"expires {action.expires_at.astimezone(UTC).isoformat()}"
    )


def _render_why(record: DecisionRecord) -> str:
    cycle = record.cycle
    lines = [
        f"Decision {cycle.decision_id}",
        f"Lead recommendation: {cycle.recommendation.summary}",
        f"Confidence: {cycle.recommendation.confidence:.0%}",
        f"Uncertainty: {cycle.recommendation.uncertainty}",
        "ESPN facts:",
    ]
    lines.extend(f"- {fact.category}: {fact.summary}" for fact in cycle.espn_facts)
    lines.append("Specialist reasoning:")
    for opinion in cycle.specialist_opinions:
        lines.append(
            f"- {opinion.specialist}: {opinion.recommendation}. "
            f"Reasoning: {opinion.reasoning}"
        )
        if opinion.dissent:
            lines.append(f"  Dissent: {opinion.dissent}")
    action = cycle.pending_action
    if action is not None:
        status = record.pending_status.value if record.pending_status else "none"
        lines.append(
            f"Action: {action.summary} [{action.action_id}] — {status}; "
            f"expires {action.expires_at.astimezone(UTC).isoformat()}"
        )
    return "\n".join(lines)


def _duplicate_reply(
    action_id: str, status: PendingActionStatus, record: DecisionRecord
) -> str:
    actor = f" by {record.pending_actor}" if record.pending_actor else ""
    if status is PendingActionStatus.APPROVED:
        return f"Approval already recorded for {action_id}{actor}; no action taken."
    return f"Veto already recorded for {action_id}{actor}; no action taken."


def _already_resolved_reply(action_id: str, record: DecisionRecord) -> str:
    status = record.pending_status.value if record.pending_status else "not pending"
    return f"{action_id} is already {status}; no action taken."
