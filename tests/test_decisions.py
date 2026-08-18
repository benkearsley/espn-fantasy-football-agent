"""Tests for the durable, provider-neutral decision history."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fantasy_football.decisions import (
    DecisionCycle,
    ESPNFact,
    ExecutionReference,
    InMemoryDecisionHistory,
    JsonlDecisionHistory,
    LeadRecommendation,
    MeasuredOutcome,
    PendingAction,
    PendingActionStatus,
    SpecialistOpinion,
)
from fantasy_football.execution import ActionRequest, ActionResult, ActionState

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)
ACTION_ID = "act-pending-lineup-001"


def cycle(
    *, action_id: str = ACTION_ID, expires_at: datetime | None = None
) -> DecisionCycle:
    return DecisionCycle(
        decision_id="decision-week-1-lineup",
        season=2026,
        league_id=7,
        trigger="scheduled monitoring",
        triggered_at=NOW,
        espn_facts=(
            ESPNFact("roster", "Starter A is listed as active", NOW),
            ESPNFact("schedule", "Game begins Sunday 12:00 CT", NOW),
        ),
        specialist_opinions=(
            SpecialistOpinion(
                "Lineup & Waiver Manager",
                "Start Player A",
                "More projected touches in this matchup",
                "Risk Reviewer prefers Player B because of floor risk",
            ),
        ),
        recommendation=LeadRecommendation(
            "Start Player A over Player B", 0.74, "Role can change before kickoff"
        ),
        pending_action=PendingAction(
            action_id,
            "lineup_change",
            "Start Player A over Player B",
            expires_at or NOW + timedelta(hours=2),
        ),
    )


def test_jsonl_history_round_trips_pending_approval_and_outcome(tmp_path: Path) -> None:
    history = JsonlDecisionHistory(tmp_path, 2026, clock=lambda: NOW)
    initial = history.record_cycle(cycle())
    assert initial.pending_status is PendingActionStatus.PENDING
    history.resolve_pending(
        ACTION_ID,
        PendingActionStatus.APPROVED,
        reason="Looks good",
        actor_id=1234,
        resolved_at=NOW,
    )
    outcome = MeasuredOutcome(
        ACTION_ID, NOW + timedelta(hours=1), "Lineup was set and verified", "verified"
    )
    history.record_outcome(outcome)

    reopened = JsonlDecisionHistory(tmp_path, 2026, clock=lambda: NOW)
    restored = reopened.get(ACTION_ID)
    assert restored is not None
    assert restored.cycle == cycle()
    assert restored.pending_status is PendingActionStatus.APPROVED
    assert restored.pending_reason == "Looks good"
    assert restored.pending_actor == "1234"
    assert restored.outcome == outcome
    assert reopened.path == tmp_path / "2026" / "decision-history.jsonl"
    assert reopened.path.stat().st_mode & 0o077 == 0
    assert reopened.path.parent.stat().st_mode & 0o077 == 0


def test_history_is_append_only_and_action_references_stay_stable(
    tmp_path: Path,
) -> None:
    history = JsonlDecisionHistory(tmp_path, 2026, clock=lambda: NOW)
    history.record_cycle(cycle())
    original = history.path.read_text(encoding="utf-8")
    history.resolve_pending(ACTION_ID, PendingActionStatus.VETOED, reason="Too risky")
    updated = history.path.read_text(encoding="utf-8")
    assert updated.startswith(original)
    rows = [json.loads(line) for line in updated.splitlines()]
    assert [row["sequence"] for row in rows] == [1, 2]
    assert rows[0]["cycle"]["pending_action"]["action_id"] == ACTION_ID
    assert history.get(ACTION_ID).action_id == ACTION_ID  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="already recorded"):
        history.record_cycle(cycle())


def test_expiry_is_persisted_and_prevents_late_approval(tmp_path: Path) -> None:
    expired_at = NOW - timedelta(seconds=1)
    history = JsonlDecisionHistory(tmp_path, 2026, clock=lambda: NOW)
    history.record_cycle(cycle(expires_at=expired_at))
    assert history.pending() == ()
    record = history.get(ACTION_ID)
    assert record is not None
    assert record.pending_status is PendingActionStatus.EXPIRED
    reopened = JsonlDecisionHistory(tmp_path, 2026, clock=lambda: NOW)
    assert reopened.get(ACTION_ID).pending_status is PendingActionStatus.EXPIRED  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="already been resolved"):
        reopened.resolve_pending(ACTION_ID, PendingActionStatus.APPROVED)


def test_stored_text_is_redacted_and_bounded(tmp_path: Path) -> None:
    secret = "secret-value"
    history = JsonlDecisionHistory(tmp_path, 2026, clock=lambda: NOW)
    unsafe = DecisionCycle(
        decision_id="decision-safe-storage",
        season=2026,
        league_id=7,
        trigger="token=secret-value " + "x" * 2_000,
        triggered_at=NOW,
        espn_facts=(ESPNFact("roster", f"cookie={secret}", NOW),),
        specialist_opinions=(SpecialistOpinion("Risk", "hold", f"SWID={secret}"),),
        recommendation=LeadRecommendation("password=secret-value", 0.5, "unknown"),
        pending_action=PendingAction(
            ACTION_ID, "lineup_change", f"secret={secret}", NOW + timedelta(minutes=1)
        ),
    )
    history.record_cycle(unsafe)
    contents = history.path.read_text(encoding="utf-8")
    assert secret not in contents
    restored = history.get(ACTION_ID)
    assert restored is not None
    assert len(restored.cycle.trigger) == 1_000
    assert "<redacted>" in contents


def test_execution_reference_and_result_reconcile_without_modifying_action_ledger() -> (
    None
):
    request = ActionRequest(
        action_id=ACTION_ID,
        kind="lineup_change",
        season=2026,
        league_id=7,
        idempotency_key="lineup-week-1",
        precondition_fingerprint="fresh-read-1",
        redacted_intent="Start Player A",
    )
    decision = cycle()
    linked = DecisionCycle(
        decision_id=decision.decision_id,
        season=decision.season,
        league_id=decision.league_id,
        trigger=decision.trigger,
        triggered_at=decision.triggered_at,
        espn_facts=decision.espn_facts,
        specialist_opinions=decision.specialist_opinions,
        recommendation=decision.recommendation,
        pending_action=decision.pending_action,
        execution=ExecutionReference.from_request(request),
    )
    history = InMemoryDecisionHistory(clock=lambda: NOW)
    history.record_cycle(linked)
    result = ActionResult(
        action_id=ACTION_ID,
        idempotency_key=request.idempotency_key,
        state=ActionState.VERIFIED,
        reason="verified on fresh ESPN read",
        completed_at=NOW + timedelta(minutes=1),
    )
    record = history.record_execution_result(result)
    assert record.cycle.execution == ExecutionReference(ACTION_ID, "lineup-week-1")
    assert record.outcome is not None
    assert record.outcome.execution_state == "verified"


def test_execution_link_must_match_the_pending_action() -> None:
    request = ActionRequest(
        action_id="act-a-different-action",
        kind="lineup_change",
        season=2026,
        league_id=7,
        idempotency_key="lineup-week-1",
        precondition_fingerprint="fresh-read-1",
    )
    with pytest.raises(ValueError, match="must match"):
        DecisionCycle(
            decision_id="decision-mismatch",
            season=2026,
            league_id=7,
            trigger="run",
            triggered_at=NOW,
            espn_facts=(ESPNFact("roster", "Player A active", NOW),),
            specialist_opinions=(),
            recommendation=LeadRecommendation("Start Player A", 0.5, "unknown"),
            pending_action=PendingAction(ACTION_ID, "lineup_change", "Start A", NOW),
            execution=ExecutionReference.from_request(request),
        )


def test_execution_result_requires_matching_recorded_execution_reference() -> None:
    request = ActionRequest(
        action_id=ACTION_ID,
        kind="lineup_change",
        season=2026,
        league_id=7,
        idempotency_key="lineup-week-1",
        precondition_fingerprint="fresh-read-1",
    )
    base_cycle = cycle()
    linked = DecisionCycle(
        decision_id=base_cycle.decision_id,
        season=base_cycle.season,
        league_id=base_cycle.league_id,
        trigger=base_cycle.trigger,
        triggered_at=base_cycle.triggered_at,
        espn_facts=base_cycle.espn_facts,
        specialist_opinions=base_cycle.specialist_opinions,
        recommendation=base_cycle.recommendation,
        pending_action=base_cycle.pending_action,
        execution=ExecutionReference.from_request(request),
    )
    mismatched = ActionResult(
        action_id=ACTION_ID,
        idempotency_key="another-write",
        state=ActionState.VERIFIED,
        reason="verified",
        completed_at=NOW,
    )
    history = InMemoryDecisionHistory(clock=lambda: NOW)
    history.record_cycle(linked)
    with pytest.raises(ValueError, match="idempotency key"):
        history.record_execution_result(mismatched)
    assert history.get(ACTION_ID).outcome is None  # type: ignore[union-attr]

    no_execution = InMemoryDecisionHistory(clock=lambda: NOW)
    no_execution.record_cycle(base_cycle)
    with pytest.raises(ValueError, match="no recorded execution reference"):
        no_execution.record_execution_result(
            ActionResult(
                action_id=ACTION_ID,
                idempotency_key=request.idempotency_key,
                state=ActionState.VERIFIED,
                reason="verified",
                completed_at=NOW,
            )
        )
    record = no_execution.record_outcome(
        MeasuredOutcome(ACTION_ID, NOW, "Manual outcome recorded")
    )
    assert record.outcome is not None


def test_report_only_decision_survives_restart_and_is_retrievable(
    tmp_path: Path,
) -> None:
    pending_cycle = cycle()
    report_only = DecisionCycle(
        decision_id="decision-week-1-report-only",
        season=pending_cycle.season,
        league_id=pending_cycle.league_id,
        trigger=pending_cycle.trigger,
        triggered_at=pending_cycle.triggered_at,
        espn_facts=pending_cycle.espn_facts,
        specialist_opinions=pending_cycle.specialist_opinions,
        recommendation=pending_cycle.recommendation,
    )
    history = JsonlDecisionHistory(tmp_path, 2026, clock=lambda: NOW)
    history.record_cycle(report_only)
    reopened = JsonlDecisionHistory(tmp_path, 2026, clock=lambda: NOW)
    assert reopened.get_decision("decision-week-1-report-only") is not None
    assert reopened.records[0].cycle == report_only
