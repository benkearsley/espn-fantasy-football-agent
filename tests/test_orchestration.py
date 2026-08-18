"""Deterministic tests for the read-only specialist decision cycle."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fantasy_football.contracts import (
    FreeAgent,
    LeagueSettings,
    LeagueSnapshot,
    LeagueStatus,
    Player,
    RosterEntry,
    Team,
)
from fantasy_football.decisions import InMemoryDecisionHistory
from fantasy_football.orchestration import (
    AnalysisLimits,
    AnalysisRequest,
    AnalysisRole,
    DecisionStatus,
    LeadManager,
)

NOW = datetime(2026, 9, 13, 18, tzinfo=UTC)


def _snapshot(*, timestamp: datetime = NOW) -> LeagueSnapshot:
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
            Team(2, "Opponent", roster=(RosterEntry(Player(21, "Other", "QB"), "QB"),)),
        ),
        matchups=(),
        status=LeagueStatus(2, "in_season"),
        draft_picks=(),
        transactions=(),
        free_agents=(FreeAgent(Player(42, "Waiver Target", "WR"), 12.5),),
        source_timestamp=timestamp,
    )


def _specialist(role: AnalysisRole) -> str:
    candidates = {
        AnalysisRole.LEAGUE_DATA: [],
        AnalysisRole.LINEUP_WAIVER: [
            {
                "kind": "lineup_change",
                "summary": "Start Bench over Starter",
                "rationale": "The supplied roster makes the matchup fit better.",
            },
            {
                "kind": "waiver_claim",
                "summary": "Claim Waiver Target",
                "rationale": "Adds depth from the supplied free-agent pool.",
            },
        ],
        AnalysisRole.TRADE: [
            {
                "kind": "trade_offer",
                "summary": "Explore a depth-for-upside offer",
                "rationale": "The roster construction supports a selective offer.",
            }
        ],
        AnalysisRole.RISK: [],
    }[role]
    return json.dumps(
        {
            "recommendation": f"{role.value} recommendation",
            "reasoning": f"{role.value} used only normalized ESPN facts.",
            "dissent": "Do not overreact to one weekly result."
            if role is AnalysisRole.RISK
            else "",
            "candidates": candidates,
        }
    )


def _lead() -> str:
    return json.dumps(
        {
            "summary": (
                "Prioritize the lineup improvement while monitoring waiver value."
            ),
            "confidence": 0.71,
            "uncertainty": "The normalized snapshot has no external injury feed.",
            "arbitration": (
                "Keep the lineup recommendation and retain the Risk Reviewer dissent."
            ),
            "next_actions": [
                {
                    "kind": "lineup_change",
                    "summary": "Start Bench over Starter",
                    "rationale": (
                        "Best championship-equity choice in the supplied facts."
                    ),
                },
                {
                    "kind": "waiver_claim",
                    "summary": "Claim Waiver Target",
                    "rationale": "Useful depth, subject to the future guardrails.",
                },
            ],
        }
    )


class FakePort:
    def __init__(self, responses: dict[AnalysisRole, list[str]]) -> None:
        self.responses = responses
        self.requests: list[AnalysisRequest] = []

    def analyze(self, request: AnalysisRequest) -> str:
        self.requests.append(request)
        return self.responses[request.role].pop(0)


def _manager(port: FakePort, *, limits: AnalysisLimits | None = None) -> LeadManager:
    return LeadManager(
        analysis_ports={role: port for role in AnalysisRole},
        decision_history=InMemoryDecisionHistory(clock=lambda: NOW),
        limits=limits,
        clock=lambda: NOW,
        decision_id_factory=lambda: "decision-orchestration",
    )


def _responses() -> dict[AnalysisRole, list[str]]:
    return {
        role: [_lead() if role is AnalysisRole.LEAD else _specialist(role)]
        for role in AnalysisRole
    }


def test_cycle_persists_traceable_lineup_waiver_trade_and_risk_dissent() -> None:
    port = FakePort(_responses())
    result = _manager(port).run_cycle(_snapshot(), trigger="manual run")

    assert result.status is DecisionStatus.RECOMMENDATION_READY
    assert result.record is not None
    assert result.record.cycle.recommendation.confidence == 0.71
    assert [action.kind for action in result.next_actions] == [
        "lineup_change",
        "waiver_claim",
    ]
    assert result.specialist_analyses[1].candidates[1].summary == "Claim Waiver Target"
    assert result.specialist_analyses[2].candidates[0].kind == "trade_offer"
    assert result.record.cycle.specialist_opinions[3].dissent
    assert "Risk Reviewer dissent" in result.record.cycle.recommendation.uncertainty
    lead_request = port.requests[-1]
    assert lead_request.role is AnalysisRole.LEAD
    assert "validated_specialist_analyses" in lead_request.context_json
    risk_request = port.requests[3]
    assert risk_request.role is AnalysisRole.RISK
    risk_context = json.loads(risk_request.context_json)
    risk_analyses = risk_context["validated_specialist_analyses"]
    assert [analysis["role"] for analysis in risk_analyses] == [
        "league_data",
        "lineup_waiver",
        "trade",
    ]
    assert result.why_ready() is not None


def test_stale_or_incomplete_snapshot_fails_closed_and_persists_no_action() -> None:
    port = FakePort(_responses())
    stale = _manager(port).run_cycle(
        _snapshot(timestamp=NOW - timedelta(minutes=16)), trigger="poll"
    )
    assert stale.status is DecisionStatus.NO_ACTION
    assert stale.record is not None
    assert "stale" in stale.reason
    assert port.requests == []

    incomplete = _snapshot()
    incomplete = LeagueSnapshot(
        settings=incomplete.settings,
        teams=(),
        matchups=incomplete.matchups,
        status=incomplete.status,
        draft_picks=incomplete.draft_picks,
        transactions=incomplete.transactions,
        free_agents=incomplete.free_agents,
        source_timestamp=incomplete.source_timestamp,
    )
    result = _manager(FakePort(_responses())).run_cycle(incomplete, trigger="poll")
    assert result.status is DecisionStatus.NO_ACTION
    assert "no league teams" in result.reason
    assert result.record is not None


def test_naive_snapshot_timestamp_fails_closed_with_a_safe_ledger_fact() -> None:
    snapshot = _snapshot()
    object.__setattr__(snapshot, "source_timestamp", datetime(2026, 9, 13, 18))
    port = FakePort(_responses())

    result = _manager(port).run_cycle(snapshot, trigger="poll")

    assert result.status is DecisionStatus.NO_ACTION
    assert result.record is not None
    assert "not timezone-aware" in result.reason
    assert port.requests == []
    fact = result.record.cycle.espn_facts[0]
    assert fact.category == "snapshot_validation"
    assert fact.observed_at == NOW


def test_invalid_model_output_retries_then_records_fail_closed_specialist() -> None:
    responses = _responses()
    responses[AnalysisRole.LEAGUE_DATA] = ["not-json", "also-not-json"]
    port = FakePort(responses)
    result = _manager(port).run_cycle(_snapshot(), trigger="poll")

    assert result.status is DecisionStatus.NO_ACTION
    assert len(port.requests) == 2
    assert [request.attempt for request in port.requests] == [1, 2]
    assert result.record is not None
    assert result.record.cycle.specialist_opinions[0].dissent.startswith("Fail closed")


def test_total_call_budget_is_bounded_and_deterministic() -> None:
    port = FakePort(_responses())
    result = _manager(
        port,
        limits=AnalysisLimits(attempts_per_role=1, max_total_calls=3),
    ).run_cycle(_snapshot(), trigger="poll")

    assert result.status is DecisionStatus.NO_ACTION
    assert result.reason.startswith("Risk Reviewer / Challenger")
    assert len(port.requests) == 3
    assert result.record is not None
