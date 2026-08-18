"""Curated weekly post-mortems and an explicit publication workflow.

The report boundary is intentionally narrower than the decision and execution
stores.  A :class:`DecisionRecord` is reduced to a public, curated view before
it can reach the renderer; pending actions, execution references, idempotency
keys, and action-ledger evidence are never rendered.  The resulting HTML is a
deterministic season/week artifact suitable for review on ``main`` and later
publication from a ``pages`` branch.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Final

from fantasy_football.decisions import DecisionRecord
from fantasy_football.execution import redact_text

MIN_SEASON: Final = 2000
MAX_SEASON: Final = 2100
MAX_WEEK: Final = 53
MAX_PUBLIC_TEXT: Final = 2_000
MAX_INSIGHT_TEXT: Final = 1_000
REPORT_DIRECTORY: Final = "reports"

# These fields are useful in a private action ledger but never belong in a
# public post-mortem.  The action-reference replacement also protects against
# a caller accidentally copying an opaque reference into a curated sentence.
_PRIVATE_FIELD = re.compile(
    r"(?i)\b(?:action[_ -]?id|idempotency[_ -]?key|"
    r"precondition[_ -]?fingerprint)\b\s*[:=]\s*[^\s,;&<>]+"
)
_ACTION_REFERENCE = re.compile(r"\bact-[A-Za-z0-9_-]+\b")


@dataclass(frozen=True, slots=True)
class CuratedSpecialistNote:
    """A public summary of one specialist's reasoning."""

    specialist: str
    recommendation: str
    reasoning: str
    dissent: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "specialist", _public_text(self.specialist))
        object.__setattr__(self, "recommendation", _public_text(self.recommendation))
        object.__setattr__(self, "reasoning", _public_text(self.reasoning))
        object.__setattr__(self, "dissent", _public_text(self.dissent))
        if not self.specialist or not self.recommendation:
            raise ValueError("a specialist note needs a name and recommendation")


@dataclass(frozen=True, slots=True)
class CuratedDecision:
    """The report-safe portion of a durable decision record.

    ``decision_id`` is a decision reference, not an action reference.  The
    conversion from :class:`DecisionRecord` below intentionally does not copy
    ``pending_action`` or ``execution``.
    """

    decision_id: str
    trigger: str
    recommendation: str
    confidence: float
    uncertainty: str
    evidence: tuple[str, ...] = ()
    specialist_notes: tuple[CuratedSpecialistNote, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_id", _public_text(self.decision_id))
        object.__setattr__(self, "trigger", _public_text(self.trigger))
        object.__setattr__(self, "recommendation", _public_text(self.recommendation))
        object.__setattr__(self, "uncertainty", _public_text(self.uncertainty))
        if not self.decision_id or not self.trigger or not self.recommendation:
            raise ValueError(
                "a curated decision needs an id, trigger, and recommendation"
            )
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("decision confidence must be between zero and one")
        object.__setattr__(
            self, "evidence", _public_lines(self.evidence, MAX_PUBLIC_TEXT)
        )
        object.__setattr__(self, "specialist_notes", tuple(self.specialist_notes))

    @classmethod
    def from_record(cls, record: DecisionRecord) -> CuratedDecision:
        """Reduce a durable decision to fields safe for a public report."""

        cycle = record.cycle
        return cls(
            decision_id=cycle.decision_id,
            trigger=cycle.trigger,
            recommendation=cycle.recommendation.summary,
            confidence=cycle.recommendation.confidence,
            uncertainty=cycle.recommendation.uncertainty,
            evidence=tuple(
                f"{fact.category}: {fact.summary}" for fact in cycle.espn_facts
            ),
            specialist_notes=tuple(
                CuratedSpecialistNote(
                    specialist=opinion.specialist,
                    recommendation=opinion.recommendation,
                    reasoning=opinion.reasoning,
                    dissent=opinion.dissent,
                )
                for opinion in cycle.specialist_opinions
            ),
        )


@dataclass(frozen=True, slots=True)
class CuratedOutcome:
    """A measured result associated with a decision, without ledger details."""

    decision_id: str
    summary: str
    execution_state: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_id", _public_text(self.decision_id))
        object.__setattr__(self, "summary", _public_text(self.summary))
        state = _public_text(self.execution_state or "")
        object.__setattr__(self, "execution_state", state or None)
        if not self.decision_id or not self.summary:
            raise ValueError("a curated outcome needs a decision and summary")

    @classmethod
    def from_record(cls, record: DecisionRecord) -> CuratedOutcome | None:
        """Return a report-safe outcome, if the decision has settled."""

        outcome = record.outcome
        if outcome is None:
            return None
        return cls(
            decision_id=record.cycle.decision_id,
            summary=outcome.summary,
            execution_state=outcome.execution_state,
        )


@dataclass(frozen=True, slots=True)
class WeeklyPostmortem:
    """All curated material for one deterministic season/week report."""

    season: int
    week: int
    decisions: tuple[CuratedDecision, ...] = ()
    outcomes: tuple[CuratedOutcome, ...] = ()
    lessons: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    missed_opportunities: tuple[str, ...] = ()
    strategy_changes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not MIN_SEASON <= self.season <= MAX_SEASON:
            raise ValueError(f"season must be between {MIN_SEASON} and {MAX_SEASON}")
        if not 1 <= self.week <= MAX_WEEK:
            raise ValueError(f"week must be between one and {MAX_WEEK}")
        decisions = tuple(self.decisions)
        outcomes = tuple(self.outcomes)
        if len({decision.decision_id for decision in decisions}) != len(decisions):
            raise ValueError("weekly report decisions must have unique ids")
        if len({outcome.decision_id for outcome in outcomes}) != len(outcomes):
            raise ValueError("weekly report outcomes must have unique decision ids")
        object.__setattr__(
            self,
            "decisions",
            tuple(sorted(decisions, key=lambda item: item.decision_id)),
        )
        object.__setattr__(
            self,
            "outcomes",
            tuple(sorted(outcomes, key=lambda item: item.decision_id)),
        )
        for field_name in (
            "lessons",
            "errors",
            "missed_opportunities",
            "strategy_changes",
        ):
            object.__setattr__(
                self,
                field_name,
                _public_lines(getattr(self, field_name), MAX_INSIGHT_TEXT),
            )

    @classmethod
    def from_records(
        cls,
        season: int,
        week: int,
        records: Iterable[DecisionRecord],
        *,
        lessons: Iterable[str] = (),
        errors: Iterable[str] = (),
        missed_opportunities: Iterable[str] = (),
        strategy_changes: Iterable[str] = (),
    ) -> WeeklyPostmortem:
        """Build a report from decision history and explicitly curated insights.

        No executor or action-ledger object is accepted.  Each history record
        is reduced through :meth:`CuratedDecision.from_record`, and only its
        measured outcome (when present) is copied into the report view.
        """

        material = tuple(records)
        for record in material:
            if record.cycle.season != season:
                raise ValueError("decision record season does not match report season")
        curated_decisions = tuple(
            CuratedDecision.from_record(record) for record in material
        )
        curated_outcomes = tuple(
            outcome
            for record in material
            if (outcome := CuratedOutcome.from_record(record)) is not None
        )
        return cls(
            season=season,
            week=week,
            decisions=curated_decisions,
            outcomes=curated_outcomes,
            lessons=tuple(lessons),
            errors=tuple(errors),
            missed_opportunities=tuple(missed_opportunities),
            strategy_changes=tuple(strategy_changes),
        )


@dataclass(frozen=True, slots=True)
class PublicationPlan:
    """Reviewable local steps for publishing one report on ``main`` and Pages."""

    report_path: str
    main_steps: tuple[str, ...]
    pages_steps: tuple[str, ...]

    @property
    def steps(self) -> tuple[str, ...]:
        """Return all steps in execution order without executing any of them."""

        return self.main_steps + self.pages_steps


def build_publication_plan(
    report: WeeklyPostmortem, *, reports_root: str = REPORT_DIRECTORY
) -> PublicationPlan:
    """Describe, but do not run, the explicit main/Pages publication workflow."""

    relative_path = (
        Path(reports_root) / str(report.season) / f"week-{report.week:02d}.html"
    ).as_posix()
    return PublicationPlan(
        report_path=relative_path,
        main_steps=(
            "Review the local artifact at "
            f"{relative_path} for content and accessibility.",
            f"On main, commit only the curated artifact {relative_path} after review.",
        ),
        pages_steps=(
            "On the pages branch, copy the reviewed "
            f"{relative_path} to the matching Pages path.",
            "Review the pages diff, then commit "
            f"{relative_path}; publish only after explicit approval.",
        ),
    )


def render_html(report: WeeklyPostmortem) -> str:
    """Render a deterministic, self-contained and accessible HTML report."""

    title = f"Fantasy Football — Tuesday {report.season} Week {report.week} Post-Mortem"
    sections = [
        _decisions_section(report.decisions),
        _outcomes_section(report.outcomes),
        _insight_section("Lessons", report.lessons, "lessons"),
        _insight_section(
            "Errors and missed opportunities",
            report.errors + report.missed_opportunities,
            "errors-and-missed-opportunities",
        ),
        _insight_section(
            "Strategy changes", report.strategy_changes, "strategy-changes"
        ),
    ]
    return "\n".join(
        (
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '  <meta charset="utf-8">',
            '  <meta name="viewport" content="width=device-width, initial-scale=1">',
            f"  <title>{_html(title)}</title>",
            "  <style>",
            "    :root { color-scheme: light dark; font-family: system-ui, "
            "sans-serif; line-height: 1.5; }",
            "    body { max-width: 58rem; margin: 0 auto; padding: 1rem; }",
            "    article, section { margin-block: 2rem; }",
            "    .muted { color: #555; }",
            "    :focus-visible { outline: 3px solid #1769aa; outline-offset: 3px; }",
            "    @media (prefers-color-scheme: dark) { .muted { color: #ccc; } }",
            "  </style>",
            "</head>",
            "<body>",
            '  <a class="skip-link" href="#report-content">Skip to report content</a>',
            '  <main id="report-content" aria-labelledby="report-title">',
            f'    <h1 id="report-title">{_html(title)}</h1>',
            '    <p class="muted">Curated decision context; private '
            "action-ledger evidence is excluded.</p>",
            *sections,
            "  </main>",
            "</body>",
            "</html>",
            "",
        )
    )


def write_weekly_report(report: WeeklyPostmortem, output_dir: Path) -> Path:
    """Create a season/week report without replacing an existing artifact."""

    if not output_dir.is_absolute():
        raise ValueError("report output_dir must be absolute")
    target = output_dir / str(report.season) / f"week-{report.week:02d}.html"
    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    html = render_html(report).encode("utf-8")
    try:
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o644,
        )
    except FileExistsError:
        raise FileExistsError(f"weekly report already exists: {target}") from None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(html)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target


def _decisions_section(decisions: Sequence[CuratedDecision]) -> str:
    cards: list[str] = []
    for decision in decisions:
        evidence = _html_list(decision.evidence)
        specialists = "".join(
            "<li>"
            f"<strong>{_html(note.specialist)}:</strong> "
            f"{_html(note.recommendation)} — {_html(note.reasoning)}"
            + (f" <em>Dissent:</em> {_html(note.dissent)}" if note.dissent else "")
            + "</li>"
            for note in decision.specialist_notes
        )
        specialist_block = (
            f"<ul>{specialists}</ul>" if specialists else "<p>None recorded.</p>"
        )
        cards.append(
            "<article>"
            f"<h3>{_html(decision.decision_id)}</h3>"
            f"<p><strong>Trigger:</strong> {_html(decision.trigger)}</p>"
            f"<p><strong>Recommendation:</strong> {_html(decision.recommendation)}</p>"
            f"<p><strong>Confidence:</strong> {decision.confidence:.0%}</p>"
            f"<p><strong>Uncertainty:</strong> {_html(decision.uncertainty)}</p>"
            f"<h4>Evidence considered</h4>{evidence}"
            f"<h4>Specialist perspectives</h4>{specialist_block}"
            "</article>"
        )
    body = "".join(cards) if cards else "<p>None recorded.</p>"
    return (
        '<section aria-labelledby="decisions-heading">'
        '<h2 id="decisions-heading">Decisions</h2>'
        f"{body}</section>"
    )


def _outcomes_section(outcomes: Sequence[CuratedOutcome]) -> str:
    items = "".join(
        "<li>"
        f"<strong>{_html(outcome.decision_id)}:</strong> {_html(outcome.summary)}"
        + (
            f' <span class="muted">Status: {_html(outcome.execution_state)}</span>'
            if outcome.execution_state
            else ""
        )
        + "</li>"
        for outcome in outcomes
    )
    body = f"<ul>{items}</ul>" if items else "<p>None recorded.</p>"
    return (
        '<section aria-labelledby="outcomes-heading">'
        '<h2 id="outcomes-heading">Outcomes</h2>'
        f"{body}</section>"
    )


def _insight_section(title: str, items: Sequence[str], identifier: str) -> str:
    return (
        f'<section aria-labelledby="{_html(identifier)}-heading">'
        f'<h2 id="{_html(identifier)}-heading">{_html(title)}</h2>'
        f"{_html_list(items)}"
        "</section>"
    )


def _html_list(items: Sequence[str]) -> str:
    if not items:
        return "<p>None recorded.</p>"
    return "<ul>" + "".join(f"<li>{_html(item)}</li>" for item in items) + "</ul>"


def _public_lines(values: Iterable[str], maximum: int) -> tuple[str, ...]:
    safe = {_public_text(value, maximum) for value in values}
    safe.discard("")
    return tuple(sorted(safe))


def _public_text(value: str, maximum: int = MAX_PUBLIC_TEXT) -> str:
    if not isinstance(value, str):
        raise ValueError("report text must be a string")
    safe = redact_text(value)
    safe = _PRIVATE_FIELD.sub("<redacted>", safe)
    safe = _ACTION_REFERENCE.sub("<redacted>", safe)
    return safe[:maximum].strip()


def _html(value: str) -> str:
    return escape(value, quote=True)
