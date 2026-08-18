"""Tests for curated Tuesday post-mortems and their local publication plan."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fantasy_football.decisions import (
    DecisionCycle,
    DecisionRecord,
    ESPNFact,
    ExecutionReference,
    LeadRecommendation,
    MeasuredOutcome,
    PendingAction,
    SpecialistOpinion,
)
from fantasy_football.reports import (
    WeeklyPostmortem,
    build_publication_plan,
    render_html,
    write_weekly_report,
)

NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)


def _record(
    decision_id: str = "decision-week-1-lineup",
    *,
    outcome: MeasuredOutcome | None = None,
) -> DecisionRecord:
    action = PendingAction(
        "act-private-action-001",
        "lineup_change",
        "Start Player A",
        NOW + timedelta(hours=2),
    )
    cycle = DecisionCycle(
        decision_id=decision_id,
        season=2026,
        league_id=7,
        trigger="scheduled <monitoring>",
        triggered_at=NOW,
        espn_facts=(
            ESPNFact("roster", "Player A is active; token=should-not-leak", NOW),
        ),
        specialist_opinions=(
            SpecialistOpinion(
                "Lineup Manager",
                "Start Player A",
                "Expected workload is higher",
                "Risk Reviewer: consider the floor",
            ),
        ),
        recommendation=LeadRecommendation(
            "Start Player A <over> Player B",
            0.74,
            "Role can change before kickoff",
        ),
        pending_action=action,
        execution=ExecutionReference(action.action_id, "idempotency-private-001"),
    )
    return DecisionRecord(cycle=cycle, outcome=outcome)


def test_report_is_complete_accessible_deterministic_and_redacted() -> None:
    outcome = MeasuredOutcome(
        "act-private-action-001",
        NOW + timedelta(hours=1),
        "Lineup was verified <strong>",
        "verified",
    )
    report = WeeklyPostmortem.from_records(
        2026,
        1,
        (_record(outcome=outcome),),
        lessons=("Use the late status check.",),
        errors=("Missed an opportunity <to act>.",),
        missed_opportunities=("Waiver claim was too conservative.",),
        strategy_changes=("Check role stability before choosing the flex.",),
    )

    html = render_html(report)

    assert html == render_html(report)
    assert "<!doctype html>" in html
    assert '<html lang="en">' in html
    assert "Tuesday 2026 Week 1 Post-Mortem" in html
    assert 'id="report-content"' in html
    assert "Decisions" in html
    assert "Outcomes" in html
    assert "Lessons" in html
    assert "Errors and missed opportunities" in html
    assert "Strategy changes" in html
    assert "&lt;monitoring&gt;" in html
    assert "&lt;over&gt;" in html
    assert "&lt;strong&gt;" in html
    assert "<script" not in html
    assert "should-not-leak" not in html
    assert "act-private-action-001" not in html
    assert "idempotency-private-001" not in html


def test_report_history_is_partitioned_and_existing_week_is_never_overwritten(
    tmp_path: Path,
) -> None:
    week_one = WeeklyPostmortem.from_records(2026, 1, (_record(),))
    week_two = WeeklyPostmortem.from_records(
        2026,
        2,
        (_record("decision-week-2-lineup"),),
    )

    first_path = write_weekly_report(week_one, tmp_path / "reports")
    first_content = first_path.read_text(encoding="utf-8")
    second_path = write_weekly_report(week_two, tmp_path / "reports")

    assert first_path == tmp_path / "reports" / "2026" / "week-01.html"
    assert second_path == tmp_path / "reports" / "2026" / "week-02.html"
    assert first_path.read_text(encoding="utf-8") == first_content
    with pytest.raises(FileExistsError, match="already exists"):
        write_weekly_report(week_one, tmp_path / "reports")


def test_publication_plan_documents_main_then_pages_without_running_git() -> None:
    report = WeeklyPostmortem.from_records(2026, 3, ())

    plan = build_publication_plan(report)

    assert plan.report_path == "reports/2026/week-03.html"
    assert "main" in " ".join(plan.main_steps)
    assert "pages branch" in " ".join(plan.pages_steps)
    assert "commit" in " ".join(plan.steps).lower()
    assert "push" not in " ".join(plan.steps).lower()

    readme = Path(__file__).parents[1] / "README.md"
    documentation = readme.read_text(encoding="utf-8")
    assert "## Tuesday post-mortems and publication" in documentation
    assert "On `main`" in documentation
    assert "On the `pages` branch" in documentation
    assert "Never" in documentation
    assert "raw decision-history or action-ledger files" in documentation


def test_report_rejects_records_from_another_season() -> None:
    with pytest.raises(ValueError, match="season"):
        WeeklyPostmortem.from_records(2027, 1, (_record(),))
