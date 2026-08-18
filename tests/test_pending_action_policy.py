"""Approval-policy integration tests with no ESPN write capability."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fantasy_football.contracts import (
    Deadline,
    LeagueSettings,
    LeagueSnapshot,
    LeagueStatus,
    Player,
    PlayerKickoff,
    RosterEntry,
    Team,
)
from fantasy_football.decisions import JsonlDecisionHistory, PendingActionStatus
from fantasy_football.execution import RuntimeWriteState
from fantasy_football.orchestration import (
    AnalysisRequest,
    AnalysisRole,
    DecisionStatus,
    LeadManager,
)
from fantasy_football.workflows import ManagerWorkflows

NOW = datetime(2026, 9, 13, 18, tzinfo=UTC)


def _snapshot(*, kickoffs: tuple[PlayerKickoff, ...] = ()) -> LeagueSnapshot:
    return LeagueSnapshot(
        settings=LeagueSettings(7, "Engine League", 2026),
        teams=(
            Team(
                1,
                "Ben's Team",
                roster=(
                    RosterEntry(Player(11, "Starter", "RB"), "RB"),
                    RosterEntry(Player(12, "Bench", "WR"), "BE"),
                ),
            ),
        ),
        matchups=(),
        status=LeagueStatus(
            2,
            "in_season",
            deadlines=(Deadline("trade", NOW + timedelta(days=2)),),
        ),
        draft_picks=(),
        transactions=(),
        free_agents=(),
        source_timestamp=NOW,
        player_kickoffs=kickoffs,
    )


class _Port:
    def __init__(self, actions: list[dict[str, object]]) -> None:
        self.actions = actions

    def analyze(self, request: AnalysisRequest) -> str:
        if request.role is AnalysisRole.LEAD:
            return json.dumps(
                {
                    "summary": "Use the validated opportunity.",
                    "confidence": 0.72,
                    "uncertainty": "Only normalized ESPN facts were used.",
                    "arbitration": "The Risk Reviewer concern is retained.",
                    "next_actions": self.actions,
                }
            )
        return json.dumps(
            {
                "recommendation": f"{request.role.value} recommendation",
                "reasoning": "Normalized facts support this view.",
                "dissent": "Recheck before execution."
                if request.role is AnalysisRole.RISK
                else "",
                "candidates": [],
            }
        )


def _manager(
    history: JsonlDecisionHistory, port: _Port, ids: Iterator[str]
) -> LeadManager:
    return LeadManager(
        analysis_ports={role: port for role in AnalysisRole},
        decision_history=history,
        clock=lambda: NOW,
        decision_id_factory=lambda: next(ids),
    )


def _workflows(history: JsonlDecisionHistory, manager: LeadManager) -> ManagerWorkflows:
    return ManagerWorkflows(
        decision_history=history,
        write_state=RuntimeWriteState(),
        run_cycle=lambda: manager.run_cycle(
            _snapshot(
                kickoffs=(
                    PlayerKickoff(11, NOW + timedelta(hours=2)),
                    PlayerKickoff(12, NOW + timedelta(hours=1)),
                )
            ),
            trigger="manual run",
        ),
        status_summary=lambda: "Roster: 2 players; health: healthy.",
        clock=lambda: NOW,
    )


def _lineup_action(*, player_ids: list[int] | None = None) -> dict[str, object]:
    return {
        "kind": "lineup_change",
        "summary": "Start Bench over Starter",
        "rationale": "The matchup supports upside.",
        "affected_player_ids": player_ids if player_ids is not None else [11, 12],
    }


def _trade_acceptance(*, expiry: datetime | None) -> dict[str, object]:
    return {
        "kind": "trade_acceptance",
        "summary": "Accept the proposed depth-for-upside trade",
        "rationale": "The trade improves championship equity.",
        **({"approval_expires_at": expiry.isoformat()} if expiry is not None else {}),
    }


def test_lineup_pending_action_is_durable_across_workflow_restart(
    tmp_path: Path,
) -> None:
    history = JsonlDecisionHistory(tmp_path, 2026, clock=lambda: NOW)
    port = _Port([_lineup_action()])
    manager = _manager(history, port, iter(("decision-policy-lineup",)))
    workflows = _workflows(history, manager)

    run = workflows.run()
    assert "recommendation_ready" in run
    record = history.records[-1]
    pending = record.cycle.pending_action
    assert pending is not None
    assert pending.expires_at == NOW + timedelta(minutes=45)
    assert pending.kind == "lineup_change"

    reopened = JsonlDecisionHistory(tmp_path, 2026, clock=lambda: NOW)
    restarted = _workflows(reopened, manager)
    assert pending.action_id in restarted.status()
    assert pending.action_id in restarted.why()
    assert "Approval recorded" in restarted.approve(pending.action_id, actor_id=42)
    approved = reopened.get(pending.action_id)
    assert approved is not None
    assert approved.pending_status is PendingActionStatus.APPROVED


def test_trade_veto_after_restart_and_global_writes_stay_disabled(
    tmp_path: Path,
) -> None:
    history = JsonlDecisionHistory(tmp_path, 2026, clock=lambda: NOW)
    port = _Port([_trade_acceptance(expiry=NOW + timedelta(minutes=30))])
    manager = _manager(history, port, iter(("decision-policy-trade",)))
    first = manager.run_cycle(_snapshot(), trigger="manual run")
    assert first.record is not None
    pending = first.record.cycle.pending_action
    assert pending is not None

    reopened = JsonlDecisionHistory(tmp_path, 2026, clock=lambda: NOW)
    state = RuntimeWriteState()
    restarted_manager = _manager(
        reopened,
        port,
        iter(("decision-policy-trade-retry",)),
    )
    restarted = ManagerWorkflows(
        decision_history=reopened,
        write_state=state,
        run_cycle=lambda: restarted_manager.run_cycle(
            _snapshot(), trigger="manual run"
        ),
        clock=lambda: NOW,
    )
    assert not state.global_enabled
    assert "recommendation_ready" in restarted.run()
    assert pending.action_id in restarted.status()
    assert pending.action_id in restarted.why()
    assert "Veto recorded" in restarted.veto(
        pending.action_id, "Too much risk", actor_id=42
    )
    vetoed = reopened.get(pending.action_id)
    assert vetoed is not None
    assert vetoed.pending_status is PendingActionStatus.VETOED


def test_stable_retry_reuses_one_pending_approval(tmp_path: Path) -> None:
    history = JsonlDecisionHistory(tmp_path, 2026, clock=lambda: NOW)
    port = _Port([_lineup_action()])
    manager = _manager(
        history,
        port,
        iter(("decision-policy-first", "decision-policy-retry")),
    )
    snapshot = _snapshot(
        kickoffs=(
            PlayerKickoff(11, NOW + timedelta(hours=2)),
            PlayerKickoff(12, NOW + timedelta(hours=1)),
        )
    )

    first = manager.run_cycle(snapshot, trigger="manual run")
    second = manager.run_cycle(snapshot, trigger="manual run")

    assert first.record is not None
    assert second.record is not None
    assert first.record.cycle.pending_action is not None
    assert second.record.cycle.pending_action is not None
    assert (
        second.record.cycle.pending_action.action_id
        == first.record.cycle.pending_action.action_id
    )
    assert len(history.records) == 1


def test_autonomous_and_unsafe_approval_recommendations_never_create_pending_actions(
    tmp_path: Path,
) -> None:
    cases: tuple[tuple[list[dict[str, object]], tuple[PlayerKickoff, ...]], ...] = (
        (
            [
                {
                    "kind": "waiver_claim",
                    "summary": "Claim a depth receiver",
                    "rationale": "Adds bench upside.",
                },
                {
                    "kind": "trade_offer",
                    "summary": "Offer depth for upside",
                    "rationale": "A selective proposal is justified.",
                },
            ],
            (),
        ),
        ([_lineup_action(player_ids=[999])], ()),
        (
            [_lineup_action(player_ids=[11])],
            (
                PlayerKickoff(11, NOW + timedelta(hours=1)),
                PlayerKickoff(11, NOW + timedelta(hours=2)),
            ),
        ),
        ([_trade_acceptance(expiry=None)], ()),
        ([_trade_acceptance(expiry=NOW + timedelta(days=2))], ()),
    )
    for number, (actions, kickoffs) in enumerate(cases):
        history = JsonlDecisionHistory(tmp_path / str(number), 2026, clock=lambda: NOW)
        manager = _manager(
            history, _Port(actions), iter((f"decision-policy-{number}",))
        )

        result = manager.run_cycle(_snapshot(kickoffs=kickoffs), trigger="manual run")

        assert result.status is DecisionStatus.RECOMMENDATION_READY
        assert result.record is not None
        assert result.record.cycle.pending_action is None
        if number:
            assert "Approval policy:" in result.record.cycle.recommendation.uncertainty
