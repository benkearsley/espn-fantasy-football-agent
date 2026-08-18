"""Tests for durable, transport-independent Telegram command workflows."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fantasy_football.decisions import (
    DecisionCycle,
    ESPNFact,
    JsonlDecisionHistory,
    LeadRecommendation,
    PendingAction,
    PendingActionStatus,
    SpecialistOpinion,
)
from fantasy_football.execution import (
    ActionAuthorizer,
    ActionRequest,
    RuntimeWriteState,
)
from fantasy_football.orchestration import DecisionCycleResult, DecisionStatus
from fantasy_football.workflows import ManagerWorkflows

NOW = datetime(2026, 9, 13, 18, tzinfo=UTC)
ACTION_ID = "act-workflow-pending-001"


def _cycle(*, expires_at: datetime | None = None) -> DecisionCycle:
    return DecisionCycle(
        decision_id="decision-workflow-001",
        season=2026,
        league_id=7,
        trigger="manual run",
        triggered_at=NOW,
        espn_facts=(ESPNFact("roster", "Starter is active", NOW),),
        specialist_opinions=(
            SpecialistOpinion(
                "Risk Reviewer / Challenger",
                "Start Player A",
                "The roster facts support the upside case.",
                "Floor risk remains material.",
            ),
        ),
        recommendation=LeadRecommendation(
            "Start Player A", 0.72, "Role changes remain possible."
        ),
        pending_action=PendingAction(
            ACTION_ID,
            "lineup_change",
            "Start Player A over Player B",
            expires_at or NOW + timedelta(minutes=30),
        ),
    )


def _workflows(
    history: JsonlDecisionHistory, state: RuntimeWriteState
) -> ManagerWorkflows:
    return ManagerWorkflows(
        decision_history=history,
        write_state=state,
        status_summary=lambda: "Roster: 15 players; health: healthy.",
        clock=lambda: NOW,
    )


def test_status_why_and_approval_are_durable_across_restart(tmp_path: Path) -> None:
    history = JsonlDecisionHistory(tmp_path, 2026, clock=lambda: NOW)
    history.record_cycle(_cycle())
    workflows = _workflows(history, RuntimeWriteState())

    status = workflows.status()
    assert "Roster: 15 players" in status
    assert ACTION_ID in status
    assert "global write switch: disabled" in status
    why = workflows.why()
    assert "Lead recommendation: Start Player A" in why
    assert "ESPN facts:" in why
    assert "Risk Reviewer / Challenger" in why
    assert "Dissent: Floor risk remains material." in why

    assert "Approval recorded" in workflows.approve(ACTION_ID, actor_id=42)
    reopened = JsonlDecisionHistory(tmp_path, 2026, clock=lambda: NOW)
    record = reopened.get(ACTION_ID)
    assert record is not None
    assert record.pending_status is PendingActionStatus.APPROVED
    assert record.pending_actor == "42"
    assert "already recorded" in _workflows(reopened, RuntimeWriteState()).approve(
        ACTION_ID, actor_id=42
    )


def test_run_reports_the_persisted_manager_cycle_result(tmp_path: Path) -> None:
    history = JsonlDecisionHistory(tmp_path, 2026, clock=lambda: NOW)
    record = history.record_cycle(_cycle())
    workflows = ManagerWorkflows(
        decision_history=history,
        write_state=RuntimeWriteState(),
        run_cycle=lambda: DecisionCycleResult(
            status=DecisionStatus.RECOMMENDATION_READY,
            reason="validated read-only recommendation",
            record=record,
        ),
        clock=lambda: NOW,
    )

    reply = workflows.run()
    assert "recommendation_ready" in reply
    assert "decision-workflow-001" in reply


def test_duplicate_concurrent_approval_appends_only_one_resolution(
    tmp_path: Path,
) -> None:
    history = JsonlDecisionHistory(tmp_path, 2026, clock=lambda: NOW)
    history.record_cycle(_cycle())
    workflows = _workflows(history, RuntimeWriteState())

    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(
            pool.map(lambda _: workflows.approve(ACTION_ID, actor_id=42), range(2))
        )

    assert sum("Approval recorded" in reply for reply in replies) == 1
    assert sum("already recorded" in reply for reply in replies) == 1
    assert len(history.events) == 2
    assert history.get(ACTION_ID).pending_actor == "42"  # type: ignore[union-attr]


def test_unknown_expired_and_veto_validation_fail_closed(tmp_path: Path) -> None:
    history = JsonlDecisionHistory(tmp_path, 2026, clock=lambda: NOW)
    history.record_cycle(_cycle(expires_at=NOW - timedelta(seconds=1)))
    workflows = _workflows(history, RuntimeWriteState())

    assert workflows.approve("act-unknown-001", actor_id=42).startswith("No pending")
    assert "Usage: veto" in workflows.veto(ACTION_ID, "", actor_id=42)
    assert "expired" in workflows.approve(ACTION_ID, actor_id=42)
    assert history.get(ACTION_ID).pending_status is PendingActionStatus.EXPIRED  # type: ignore[union-attr]


def test_veto_is_attributed_idempotent_and_persists_reason(tmp_path: Path) -> None:
    history = JsonlDecisionHistory(tmp_path, 2026, clock=lambda: NOW)
    history.record_cycle(_cycle())
    workflows = _workflows(history, RuntimeWriteState())

    assert "Veto recorded" in workflows.veto(
        ACTION_ID, "Too much downside", actor_id=42
    )
    assert "already recorded" in workflows.veto(
        ACTION_ID, "Too much downside", actor_id=42
    )
    record = history.get(ACTION_ID)
    assert record is not None
    assert record.pending_status is PendingActionStatus.VETOED
    assert record.pending_reason == "Too much downside"
    assert record.pending_actor == "42"


def test_pause_uses_execution_runtime_state_and_approval_cannot_enable_writes(
    tmp_path: Path,
) -> None:
    history = JsonlDecisionHistory(tmp_path, 2026, clock=lambda: NOW)
    history.record_cycle(_cycle())
    state = RuntimeWriteState()
    workflows = _workflows(history, state)

    assert "paused" in workflows.pause()
    assert state.paused
    assert "pause lifted" in workflows.resume()
    assert not state.paused
    workflows.approve(ACTION_ID, actor_id=42)

    request = ActionRequest(
        action_id=ACTION_ID,
        kind="lineup_change",
        season=2026,
        league_id=7,
        idempotency_key="workflow-proof-001",
        precondition_fingerprint="fresh-read-001",
        approval_required=True,
        approved_by=42,
        approval_expires_at=NOW + timedelta(minutes=10),
    )
    authorization = ActionAuthorizer(
        authorized_user_id=42, state=state, clock=lambda: NOW
    ).authorize(request, "fresh-read-001")
    assert not authorization.allowed
    assert authorization.reason == "global writes are disabled"
