"""Read-only specialist analysis and Lead Manager arbitration.

This module is intentionally a narrow boundary between normalized ESPN data and
provider-specific model adapters.  A model adapter receives only an encoded
``LeagueSnapshot``, canonical guardrails, and bounded curated context.  It
never receives an ESPN client, credentials, an execution port, or a mutable
decision record.

Every completed cycle, including a fail-closed no-action result, is written
through :class:`fantasy_football.decisions.DecisionHistory`.  That makes the
lead recommendation and each specialist's reasoning available to the future
Telegram ``why`` workflow without making an ESPN write possible here.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol, TypeVar, cast, runtime_checkable
from uuid import uuid4

from fantasy_football.contracts import LeagueSnapshot
from fantasy_football.decisions import (
    DecisionCycle,
    DecisionHistory,
    DecisionRecord,
    ESPNFact,
    LeadRecommendation,
    PendingAction,
    SpecialistOpinion,
)
from fantasy_football.execution import redact_text

MAX_PRIOR_CONTEXTS = 12
MAX_CONTEXT_ID_LENGTH = 120
MAX_CONTEXT_SUMMARY_LENGTH = 1_000
MAX_RECOMMENDATION_LENGTH = 500
MAX_REASONING_LENGTH = 700
MAX_DISSENT_LENGTH = 700
MAX_ACTION_KIND_LENGTH = 64
MAX_ACTION_SUMMARY_LENGTH = 220
MAX_ACTION_RATIONALE_LENGTH = 300
MAX_NEXT_ACTIONS = 3
MAX_ARBITRATION_LENGTH = 600
MAX_UNCERTAINTY_LENGTH = 600
LINEUP_APPROVAL_KINDS = frozenset({"lineup_change"})
TRADE_ACCEPTANCE_KINDS = frozenset({"trade_accept", "trade_acceptance"})
MAX_TRADE_APPROVAL_WINDOW = timedelta(hours=24)
_MULTIPLE_APPROVALS = "multiple approval-required recommendations are ambiguous"
_NO_AFFECTED_PLAYERS = "lineup recommendation has no affected player identifiers"
_UNKNOWN_PLAYER_KICKOFF = (
    "lineup recommendation has no known kickoff for every affected player"
)
_LINEUP_SAFETY_WINDOW = (
    "lineup recommendation is already within its approval safety window"
)
_TRADE_EXPIRY_UNSAFE = "trade acceptance approval expiry is outside the safe window"
_AMBIGUOUS_TRADE_DEADLINE = "trade acceptance has ambiguous normalized trade deadlines"
_TRADE_EXPIRY_AFTER_DEADLINE = (
    "trade acceptance approval expiry exceeds the trade deadline"
)
_POLICY_EVALUATION_FAILED = (
    "approval policy could not safely evaluate the recommendation"
)


class AnalysisRole(StrEnum):
    """The canonical specialist and Lead Manager roles in one decision cycle."""

    LEAGUE_DATA = "league_data"
    LINEUP_WAIVER = "lineup_waiver"
    TRADE = "trade"
    RISK = "risk"
    LEAD = "lead"


SPECIALIST_ROLES: tuple[AnalysisRole, ...] = (
    AnalysisRole.LEAGUE_DATA,
    AnalysisRole.LINEUP_WAIVER,
    AnalysisRole.TRADE,
    AnalysisRole.RISK,
)

ROLE_LABELS: Mapping[AnalysisRole, str] = {
    AnalysisRole.LEAGUE_DATA: "League & Data Analyst",
    AnalysisRole.LINEUP_WAIVER: "Lineup & Waiver Manager",
    AnalysisRole.TRADE: "Trade Analyst & Negotiator",
    AnalysisRole.RISK: "Risk Reviewer / Challenger",
    AnalysisRole.LEAD: "Lead Manager",
}

# These are deliberately application-owned summaries of the canonical
# guardrails, not a caller-provided prompt extension.  Model adapters are free
# to change, but the authority boundary stays stable in application code.
CANONICAL_GUARDRAILS: tuple[str, ...] = (
    "Optimize for championship equity, including justified variance, not only "
    "weekly projection accuracy.",
    "This is a read-only analysis cycle: never execute, authorize, or claim "
    "to have made an ESPN change.",
    "A lineup change requires Ben's approval before any future execution and "
    "an unanswered approval expires 15 minutes before the affected game.",
    "A waiver or free-agent recommendation must call out if it would drop a "
    "current starter or a player on bye before any future execution.",
    "A trade proposal may be recommended, but a trade acceptance requires "
    "Ben's approval before any future execution.",
    "Treat a manual ESPN action by Ben as authoritative and do not recommend "
    "automatically undoing it.",
    "State material uncertainty, preserve specialist dissent, and prefer no "
    "action when facts are stale or incomplete.",
)

ROLE_INSTRUCTIONS: Mapping[AnalysisRole, str] = {
    AnalysisRole.LEAGUE_DATA: (
        "Identify the normalized league, roster, schedule, deadline, transaction, "
        "and market facts that materially constrain this decision. Do not infer "
        "facts outside the supplied snapshot."
    ),
    AnalysisRole.LINEUP_WAIVER: (
        "Evaluate start/sit, roster construction, waiver, and free-agent "
        "recommendations using only the supplied normalized snapshot. Separate "
        "a recommendation from uncertainty and do not claim an action was made."
    ),
    AnalysisRole.TRADE: (
        "Evaluate possible trade opportunities and negotiation posture from the "
        "supplied normalized snapshot. Do not accept a trade or repeat a pending "
        "offer; state uncertainty and any reason to hold."
    ),
    AnalysisRole.RISK: (
        "Independently challenge the other likely recommendations. Identify "
        "reaches, downside, rule conflicts, stale assumptions, and material "
        "uncertainty. Preserve a clear dissent even when the recommendation is to hold."
    ),
    AnalysisRole.LEAD: (
        "Arbitrate the validated specialist analyses for championship equity. "
        "Explain why the final recommendation wins over dissent, list only "
        "recommendation-level next actions, and never authorize or execute ESPN work."
    ),
}

_ACTION_KIND_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
_SPECIALIST_SCHEMA = (
    '{"recommendation":"string","reasoning":"string","dissent":"string",'
    '"candidates":[{"kind":"lowercase_identifier","summary":"string",'
    '"rationale":"string","affected_player_ids?":"integer[]",'
    '"approval_expires_at?":"ISO-8601 timestamp"}]}'
)
_LEAD_SCHEMA = (
    '{"summary":"string","confidence":0.0,"uncertainty":"string",'
    '"arbitration":"string","next_actions":[{"kind":"lowercase_identifier",'
    '"summary":"string","rationale":"string",'
    '"affected_player_ids?":"integer[]",'
    '"approval_expires_at?":"ISO-8601 timestamp"}]}'
)


class DecisionStatus(StrEnum):
    """Whether a cycle produced a usable recommendation or failed closed."""

    RECOMMENDATION_READY = "recommendation_ready"
    NO_ACTION = "no_action"


class StructuredOutputError(ValueError):
    """A model response did not satisfy the bounded application schema."""


@dataclass(frozen=True, slots=True)
class CuratedPriorContext:
    """A bounded, redacted lesson from a curated prior post-mortem or decision.

    Raw action logs do not belong here.  The caller must deliberately reduce
    prior history to an identifier, durable lesson, and time before it can be
    supplied to a model.
    """

    context_id: str
    summary: str
    recorded_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "context_id",
            _required_text(self.context_id, "context id", MAX_CONTEXT_ID_LENGTH),
        )
        object.__setattr__(
            self,
            "summary",
            _required_text(
                self.summary, "curated context summary", MAX_CONTEXT_SUMMARY_LENGTH
            ),
        )
        _require_aware(self.recorded_at, "curated context")

    def to_dict(self) -> dict[str, str]:
        return {
            "context_id": self.context_id,
            "summary": self.summary,
            "recorded_at": self.recorded_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class RecommendedAction:
    """A recommendation only; this type is deliberately not an execution request."""

    kind: str
    summary: str
    rationale: str
    affected_player_ids: tuple[int, ...] = ()
    approval_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not _ACTION_KIND_PATTERN.fullmatch(
            self.kind
        ):
            raise StructuredOutputError(
                "recommended action kind must be a lowercase identifier"
            )
        object.__setattr__(self, "kind", self.kind.strip())
        object.__setattr__(
            self,
            "summary",
            _required_text(
                self.summary, "recommended action summary", MAX_ACTION_SUMMARY_LENGTH
            ),
        )
        object.__setattr__(
            self,
            "rationale",
            _required_text(
                self.rationale,
                "recommended action rationale",
                MAX_ACTION_RATIONALE_LENGTH,
            ),
        )
        player_ids = tuple(self.affected_player_ids)
        if any(
            not isinstance(player_id, int)
            or isinstance(player_id, bool)
            or player_id <= 0
            for player_id in player_ids
        ):
            raise StructuredOutputError("affected player ids must be positive integers")
        if len(set(player_ids)) != len(player_ids):
            raise StructuredOutputError("affected player ids must be distinct")
        object.__setattr__(self, "affected_player_ids", player_ids)
        if self.approval_expires_at is not None:
            _require_aware(
                self.approval_expires_at, "recommended action approval expiry"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "summary": self.summary,
            "rationale": self.rationale,
            "affected_player_ids": list(self.affected_player_ids),
            "approval_expires_at": (
                self.approval_expires_at.isoformat()
                if self.approval_expires_at is not None
                else None
            ),
        }

    def brief(self) -> str:
        """Return the compact form safe to retain inside a decision record."""

        return f"[{self.kind}] {self.summary}"


@dataclass(frozen=True, slots=True)
class SpecialistAnalysis:
    """Validated structured output from one canonical specialist."""

    role: AnalysisRole
    recommendation: str
    reasoning: str
    dissent: str
    candidates: tuple[RecommendedAction, ...]

    def __post_init__(self) -> None:
        if self.role not in SPECIALIST_ROLES:
            raise StructuredOutputError("analysis must belong to a specialist role")
        object.__setattr__(
            self,
            "recommendation",
            _required_text(
                self.recommendation,
                "specialist recommendation",
                MAX_RECOMMENDATION_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "reasoning",
            _required_text(
                self.reasoning, "specialist reasoning", MAX_REASONING_LENGTH
            ),
        )
        object.__setattr__(
            self,
            "dissent",
            _optional_text(self.dissent, "specialist dissent", MAX_DISSENT_LENGTH),
        )
        candidates = tuple(self.candidates)
        if len(candidates) > MAX_NEXT_ACTIONS:
            raise StructuredOutputError("too many specialist candidates")
        if len({candidate.brief() for candidate in candidates}) != len(candidates):
            raise StructuredOutputError("specialist candidates must be distinct")
        object.__setattr__(self, "candidates", candidates)

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role.value,
            "recommendation": self.recommendation,
            "reasoning": self.reasoning,
            "dissent": self.dissent,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }

    def to_opinion(self) -> SpecialistOpinion:
        """Create the durable, bounded specialist view used by DecisionHistory."""

        reasoning = self.reasoning
        if self.candidates:
            candidate_lines = "\n".join(
                f"- {candidate.brief()}" for candidate in self.candidates
            )
            reasoning = f"{reasoning}\nCandidate recommendations:\n{candidate_lines}"
        return SpecialistOpinion(
            specialist=ROLE_LABELS[self.role],
            recommendation=self.recommendation,
            reasoning=reasoning,
            dissent=self.dissent,
        )


@dataclass(frozen=True, slots=True)
class LeadAnalysis:
    """Validated final arbitration before it is converted to a durable record."""

    summary: str
    confidence: float
    uncertainty: str
    arbitration: str
    next_actions: tuple[RecommendedAction, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "summary",
            _required_text(self.summary, "lead summary", MAX_RECOMMENDATION_LENGTH),
        )
        if (
            not isinstance(self.confidence, float | int)
            or isinstance(self.confidence, bool)
            or not math.isfinite(float(self.confidence))
            or not 0.0 <= float(self.confidence) <= 1.0
        ):
            raise StructuredOutputError(
                "lead confidence must be a finite number from zero to one"
            )
        object.__setattr__(self, "confidence", float(self.confidence))
        object.__setattr__(
            self,
            "uncertainty",
            _required_text(
                self.uncertainty, "lead uncertainty", MAX_UNCERTAINTY_LENGTH
            ),
        )
        object.__setattr__(
            self,
            "arbitration",
            _required_text(
                self.arbitration, "lead arbitration", MAX_ARBITRATION_LENGTH
            ),
        )
        next_actions = tuple(self.next_actions)
        if len(next_actions) > MAX_NEXT_ACTIONS:
            raise StructuredOutputError("too many lead next actions")
        if len({action.brief() for action in next_actions}) != len(next_actions):
            raise StructuredOutputError("lead next actions must be distinct")
        object.__setattr__(self, "next_actions", next_actions)

    def to_dict(self) -> dict[str, object]:
        return {
            "summary": self.summary,
            "confidence": self.confidence,
            "uncertainty": self.uncertainty,
            "arbitration": self.arbitration,
            "next_actions": [action.to_dict() for action in self.next_actions],
        }

    def to_recommendation(self) -> LeadRecommendation:
        """Render next actions and arbitration into the existing durable contract."""

        summary = self.summary
        if self.next_actions:
            action_lines = "\n".join(
                f"- {action.brief()}" for action in self.next_actions
            )
            summary = f"{summary}\nNext actions:\n{action_lines}"
        uncertainty = (
            f"Uncertainty: {self.uncertainty}\nArbitration: {self.arbitration}"
        )
        return LeadRecommendation(summary, self.confidence, uncertainty)


@dataclass(frozen=True, slots=True)
class AnalysisRequest:
    """Provider-neutral request passed to a model or deterministic analysis port."""

    role: AnalysisRole
    instructions: str
    context_json: str
    attempt: int
    max_output_characters: int

    def __post_init__(self) -> None:
        if not self.instructions or not self.context_json:
            raise ValueError("analysis request needs instructions and context")
        if self.attempt < 1:
            raise ValueError("analysis request attempt must be positive")
        if self.max_output_characters <= 0:
            raise ValueError("analysis request output limit must be positive")


@runtime_checkable
class AnalysisPort(Protocol):
    """A provider adapter which returns one JSON object for an analysis request."""

    def analyze(self, request: AnalysisRequest) -> str:
        """Return a bounded structured response without performing ESPN work."""


@dataclass(frozen=True, slots=True)
class AnalysisLimits:
    """Hard per-cycle ceilings for model calls, retries, context, and output."""

    attempts_per_role: int = 2
    max_total_calls: int = 10
    max_context_characters: int = 12_000
    max_total_input_characters: int = 60_000
    max_output_characters: int = 4_000
    max_total_output_characters: int = 20_000
    max_snapshot_age: timedelta = timedelta(minutes=15)
    max_future_snapshot_skew: timedelta = timedelta(seconds=30)

    def __post_init__(self) -> None:
        for name in (
            "attempts_per_role",
            "max_total_calls",
            "max_context_characters",
            "max_total_input_characters",
            "max_output_characters",
            "max_total_output_characters",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.attempts_per_role > self.max_total_calls:
            raise ValueError("attempts_per_role cannot exceed max_total_calls")
        if self.max_context_characters > self.max_total_input_characters:
            raise ValueError("per-call context cannot exceed total input budget")
        if self.max_output_characters > self.max_total_output_characters:
            raise ValueError("per-call output cannot exceed total output budget")
        if self.max_snapshot_age < timedelta(0):
            raise ValueError("max_snapshot_age cannot be negative")
        if self.max_future_snapshot_skew < timedelta(0):
            raise ValueError("max_future_snapshot_skew cannot be negative")


@dataclass(frozen=True, slots=True)
class WhyReadyResult:
    """The durable fields needed to answer a concise ``why`` request."""

    decision_id: str
    status: DecisionStatus
    recommendation: LeadRecommendation
    espn_facts: tuple[ESPNFact, ...]
    specialist_opinions: tuple[SpecialistOpinion, ...]
    next_actions: tuple[RecommendedAction, ...] = ()

    def render(self) -> str:
        """Render a bounded, human-readable explanation without raw model output."""

        lines = [
            f"Decision {self.decision_id}: {self.status.value}",
            self.recommendation.summary,
            self.recommendation.uncertainty,
            "Specialist reasoning:",
        ]
        for opinion in self.specialist_opinions:
            dissent = f" Dissent: {opinion.dissent}" if opinion.dissent else ""
            lines.append(
                f"- {opinion.specialist}: {opinion.recommendation}. "
                f"Reasoning: {opinion.reasoning}.{dissent}"
            )
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class DecisionCycleResult:
    """Result of one read-only cycle, with no executable ESPN action embedded."""

    status: DecisionStatus
    reason: str
    record: DecisionRecord | None
    specialist_analyses: tuple[SpecialistAnalysis, ...] = ()
    next_actions: tuple[RecommendedAction, ...] = ()

    @property
    def decision_id(self) -> str | None:
        return self.record.cycle.decision_id if self.record is not None else None

    @property
    def can_recommend(self) -> bool:
        """Only a fully persisted, validated cycle can produce next actions."""

        return (
            self.status is DecisionStatus.RECOMMENDATION_READY
            and self.record is not None
        )

    def why_ready(self) -> WhyReadyResult | None:
        """Return the current-cycle explanation, if persistence succeeded."""

        if self.record is None:
            return None
        return _why_from_record(self.record, next_actions=self.next_actions)


@dataclass(frozen=True, slots=True)
class PendingActionPolicyResult:
    """The policy's safe, pre-persistence treatment of lead recommendations."""

    pending_action: PendingAction | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        if self.pending_action is not None and self.failure_reason is not None:
            raise ValueError(
                "a pending action policy result cannot both allow and fail"
            )


@runtime_checkable
class PendingActionPolicy(Protocol):
    """Provider-neutral approval policy injected before decision persistence."""

    def materialize(
        self,
        snapshot: LeagueSnapshot,
        actions: tuple[RecommendedAction, ...],
        *,
        now: datetime,
    ) -> PendingActionPolicyResult:
        """Return one safe approval target, no target, or a fail-closed reason."""


class DefaultPendingActionPolicy:
    """Translate only known approval-required recommendations into intents.

    This policy is intentionally ignorant of ESPN clients and executors.  It
    consumes only normalized facts and validated recommendation fields, so a
    missing player lock or trade expiry cannot become an unsafe pending action.
    """

    def __init__(
        self, *, max_trade_approval_window: timedelta = MAX_TRADE_APPROVAL_WINDOW
    ) -> None:
        if max_trade_approval_window <= timedelta(0):
            raise ValueError("trade approval window must be positive")
        self._max_trade_approval_window = max_trade_approval_window

    def materialize(
        self,
        snapshot: LeagueSnapshot,
        actions: tuple[RecommendedAction, ...],
        *,
        now: datetime,
    ) -> PendingActionPolicyResult:
        _require_aware(now, "pending action policy clock")
        approval_actions = tuple(
            action
            for action in actions
            if action.kind in LINEUP_APPROVAL_KINDS | TRADE_ACCEPTANCE_KINDS
        )
        if not approval_actions:
            return PendingActionPolicyResult()
        if len(approval_actions) != 1:
            return PendingActionPolicyResult(failure_reason=_MULTIPLE_APPROVALS)
        action = approval_actions[0]
        if action.kind in LINEUP_APPROVAL_KINDS:
            return self._lineup_action(snapshot, action, now)
        return self._trade_acceptance_action(snapshot, action, now)

    def _lineup_action(
        self, snapshot: LeagueSnapshot, action: RecommendedAction, now: datetime
    ) -> PendingActionPolicyResult:
        if not action.affected_player_ids:
            return PendingActionPolicyResult(failure_reason=_NO_AFFECTED_PLAYERS)
        kickoffs: dict[int, list[datetime]] = {}
        for kickoff in snapshot.player_kickoffs:
            kickoffs.setdefault(kickoff.player_id, []).append(kickoff.kickoff_at)
        if any(
            len(kickoffs.get(player_id, ())) != 1
            for player_id in action.affected_player_ids
        ):
            return PendingActionPolicyResult(failure_reason=_UNKNOWN_PLAYER_KICKOFF)
        earliest_kickoff = min(
            kickoffs[player_id][0] for player_id in action.affected_player_ids
        )
        expiry = earliest_kickoff - timedelta(minutes=15)
        if expiry <= now:
            return PendingActionPolicyResult(failure_reason=_LINEUP_SAFETY_WINDOW)
        return PendingActionPolicyResult(
            pending_action=_pending_action(snapshot, action, expiry)
        )

    def _trade_acceptance_action(
        self, snapshot: LeagueSnapshot, action: RecommendedAction, now: datetime
    ) -> PendingActionPolicyResult:
        expiry = action.approval_expires_at
        if expiry is None:
            return PendingActionPolicyResult(
                failure_reason="trade acceptance has no explicit approval expiry"
            )
        if expiry <= now or expiry > now + self._max_trade_approval_window:
            return PendingActionPolicyResult(failure_reason=_TRADE_EXPIRY_UNSAFE)
        trade_deadlines = tuple(
            deadline.at
            for deadline in snapshot.status.deadlines
            if deadline.name.strip().lower() == "trade"
        )
        if len(trade_deadlines) > 1:
            return PendingActionPolicyResult(failure_reason=_AMBIGUOUS_TRADE_DEADLINE)
        if trade_deadlines and expiry > trade_deadlines[0]:
            return PendingActionPolicyResult(
                failure_reason=_TRADE_EXPIRY_AFTER_DEADLINE
            )
        return PendingActionPolicyResult(
            pending_action=_pending_action(snapshot, action, expiry)
        )


@dataclass(slots=True)
class _CallBudget:
    """Mutable, cycle-local model budget; never persisted or exposed to a port."""

    limits: AnalysisLimits
    calls: int = 0
    input_characters: int = 0
    output_characters: int = 0

    def reserve_call(self, input_characters: int) -> bool:
        if self.calls >= self.limits.max_total_calls:
            return False
        if (
            self.input_characters + input_characters
            > self.limits.max_total_input_characters
        ):
            return False
        self.calls += 1
        self.input_characters += input_characters
        return True

    def record_output(self, output_characters: int) -> bool:
        self.output_characters += output_characters
        return self.output_characters <= self.limits.max_total_output_characters


T = TypeVar("T")


class LeadManager:
    """Run canonical specialists, then persist read-only Lead Manager arbitration."""

    def __init__(
        self,
        *,
        analysis_ports: Mapping[AnalysisRole, AnalysisPort],
        decision_history: DecisionHistory,
        pending_action_policy: PendingActionPolicy | None = None,
        limits: AnalysisLimits | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        decision_id_factory: Callable[[], str] = lambda: f"decision-{uuid4().hex}",
    ) -> None:
        missing = [role for role in AnalysisRole if role not in analysis_ports]
        if missing:
            names = ", ".join(role.value for role in missing)
            raise ValueError(f"analysis ports are required for: {names}")
        self._analysis_ports = dict(analysis_ports)
        self._decision_history = decision_history
        self._pending_action_policy = (
            pending_action_policy or DefaultPendingActionPolicy()
        )
        self._limits = limits or AnalysisLimits()
        self._clock = clock
        self._decision_id_factory = decision_id_factory

    def run_cycle(
        self,
        snapshot: LeagueSnapshot,
        *,
        trigger: str,
        prior_context: Sequence[CuratedPriorContext] = (),
    ) -> DecisionCycleResult:
        """Run the full read-only cycle and fail closed on every unsafe condition."""

        now = self._now()
        facts = (
            _snapshot_facts(snapshot)
            if snapshot.source_timestamp.tzinfo is not None
            else _invalid_timestamp_facts(now)
        )
        trigger_text = _optional_text(trigger, "trigger", 1_000)
        if not trigger_text:
            trigger_text = "unspecified decision trigger"

        snapshot_problem = _snapshot_problem(snapshot, now, self._limits)
        if snapshot_problem is not None:
            return self._persist_no_action(
                snapshot=snapshot,
                trigger=trigger_text,
                now=now,
                facts=facts,
                reason=snapshot_problem,
            )

        contexts = tuple(prior_context)
        if len(contexts) > MAX_PRIOR_CONTEXTS:
            return self._persist_no_action(
                snapshot=snapshot,
                trigger=trigger_text,
                now=now,
                facts=facts,
                reason="too much curated prior context was supplied",
            )

        try:
            base_context = _base_context(snapshot, contexts, self._limits)
        except StructuredOutputError:
            return self._persist_no_action(
                snapshot=snapshot,
                trigger=trigger_text,
                now=now,
                facts=facts,
                reason=(
                    "normalized snapshot and curated context exceed the "
                    "bounded model context"
                ),
            )

        budget = _CallBudget(self._limits)
        analyses: list[SpecialistAnalysis] = []
        opinions: list[SpecialistOpinion] = []
        for role in SPECIALIST_ROLES:
            preceding_analyses = tuple(analyses) if role is AnalysisRole.RISK else ()
            context = _encode_context(
                base_context,
                role=role,
                analyses=preceding_analyses,
                limits=self._limits,
            )
            if context is None:
                return self._persist_no_action(
                    snapshot=snapshot,
                    trigger=trigger_text,
                    now=now,
                    facts=facts,
                    reason=(
                        "normalized snapshot and curated context exceed the "
                        "bounded model context"
                    ),
                    specialist_opinions=tuple(opinions),
                    specialist_analyses=tuple(analyses),
                )

            def parser(
                text: str, *, analyst_role: AnalysisRole = role
            ) -> SpecialistAnalysis:
                return _parse_specialist(analyst_role, text)

            analysis, failure = self._call_with_retries(role, context, budget, parser)
            if analysis is None:
                opinions.append(_failed_specialist_opinion(role, failure))
                return self._persist_no_action(
                    snapshot=snapshot,
                    trigger=trigger_text,
                    now=now,
                    facts=facts,
                    reason=(
                        f"{ROLE_LABELS[role]} did not return valid structured analysis"
                    ),
                    specialist_opinions=tuple(opinions),
                    specialist_analyses=tuple(analyses),
                )
            analyses.append(analysis)
            opinions.append(analysis.to_opinion())

        lead_context = _encode_context(
            base_context,
            role=AnalysisRole.LEAD,
            analyses=tuple(analyses),
            limits=self._limits,
        )
        if lead_context is None:
            return self._persist_no_action(
                snapshot=snapshot,
                trigger=trigger_text,
                now=now,
                facts=facts,
                reason=(
                    "validated specialist context exceeds the bounded "
                    "lead-manager context"
                ),
                specialist_opinions=tuple(opinions),
                specialist_analyses=tuple(analyses),
            )
        lead, failure = self._call_with_retries(
            AnalysisRole.LEAD,
            lead_context,
            budget,
            _parse_lead,
        )
        if lead is None:
            return self._persist_no_action(
                snapshot=snapshot,
                trigger=trigger_text,
                now=now,
                facts=facts,
                reason="Lead Manager did not return valid structured arbitration",
                specialist_opinions=tuple(opinions),
                specialist_analyses=tuple(analyses),
            )

        return self._persist(
            snapshot=snapshot,
            trigger=trigger_text,
            now=now,
            facts=facts,
            specialist_opinions=tuple(opinions),
            recommendation=lead.to_recommendation(),
            status=DecisionStatus.RECOMMENDATION_READY,
            reason="validated read-only recommendation",
            specialist_analyses=tuple(analyses),
            next_actions=lead.next_actions,
        )

    def why(self, decision_id: str) -> WhyReadyResult | None:
        """Load a restart-safe explanation from the durable decision history."""

        record = self._decision_history.get_decision(decision_id)
        if record is None:
            return None
        return _why_from_record(record)

    def _call_with_retries(
        self,
        role: AnalysisRole,
        context_json: str,
        budget: _CallBudget,
        parser: Callable[[str], T],
    ) -> tuple[T | None, str]:
        instructions = _instructions_for(role)
        input_characters = len(instructions) + len(context_json)
        failure = "analysis port error"
        for attempt in range(1, self._limits.attempts_per_role + 1):
            if not budget.reserve_call(input_characters):
                return None, "analysis budget exhausted"
            request = AnalysisRequest(
                role=role,
                instructions=instructions,
                context_json=context_json,
                attempt=attempt,
                max_output_characters=self._limits.max_output_characters,
            )
            try:
                response = self._analysis_ports[role].analyze(request)
            except Exception:
                failure = "analysis port error"
                continue
            if not isinstance(response, str):
                failure = "analysis output was not text"
                continue
            if not budget.record_output(len(response)):
                return None, "analysis output budget exhausted"
            if len(response) > self._limits.max_output_characters:
                failure = "analysis output exceeded the per-call limit"
                continue
            try:
                return parser(response), ""
            except StructuredOutputError:
                failure = "analysis output did not match the required schema"
        return None, failure

    def _persist_no_action(
        self,
        *,
        snapshot: LeagueSnapshot,
        trigger: str,
        now: datetime,
        facts: tuple[ESPNFact, ...],
        reason: str,
        specialist_opinions: tuple[SpecialistOpinion, ...] = (),
        specialist_analyses: tuple[SpecialistAnalysis, ...] = (),
    ) -> DecisionCycleResult:
        recommendation = LeadRecommendation(
            f"No action: {reason}.",
            0.0,
            "Fail closed: obtain a fresh complete normalized snapshot and rerun.",
        )
        return self._persist(
            snapshot=snapshot,
            trigger=trigger,
            now=now,
            facts=facts,
            specialist_opinions=specialist_opinions,
            recommendation=recommendation,
            status=DecisionStatus.NO_ACTION,
            reason=reason,
            specialist_analyses=specialist_analyses,
            next_actions=(),
        )

    def _persist(
        self,
        *,
        snapshot: LeagueSnapshot,
        trigger: str,
        now: datetime,
        facts: tuple[ESPNFact, ...],
        specialist_opinions: tuple[SpecialistOpinion, ...],
        recommendation: LeadRecommendation,
        status: DecisionStatus,
        reason: str,
        specialist_analyses: tuple[SpecialistAnalysis, ...],
        next_actions: tuple[RecommendedAction, ...],
    ) -> DecisionCycleResult:
        policy_result = PendingActionPolicyResult()
        if status is DecisionStatus.RECOMMENDATION_READY:
            try:
                policy_result = self._pending_action_policy.materialize(
                    snapshot,
                    next_actions,
                    now=now,
                )
            except Exception:
                policy_result = PendingActionPolicyResult(
                    failure_reason=_POLICY_EVALUATION_FAILED
                )
        if policy_result.failure_reason is not None:
            recommendation = _recommendation_with_policy_reason(
                recommendation, policy_result.failure_reason
            )
            reason = (
                f"{reason}; pending approval not created: "
                f"{policy_result.failure_reason}"
            )
        pending_action = policy_result.pending_action
        if pending_action is not None:
            existing = self._decision_history.get(pending_action.action_id)
            if existing is not None:
                return DecisionCycleResult(
                    status=status,
                    reason=f"{reason}; reused the existing stable pending approval",
                    record=existing,
                    specialist_analyses=specialist_analyses,
                    next_actions=next_actions,
                )
        try:
            cycle = DecisionCycle(
                decision_id=self._decision_id_factory(),
                season=snapshot.settings.season,
                league_id=snapshot.settings.league_id,
                trigger=trigger,
                triggered_at=now,
                espn_facts=facts,
                specialist_opinions=specialist_opinions,
                recommendation=recommendation,
                pending_action=pending_action,
            )
            record = self._decision_history.record_cycle(cycle)
        except Exception:
            return DecisionCycleResult(
                status=DecisionStatus.NO_ACTION,
                reason="decision history could not persist the cycle",
                record=None,
                specialist_analyses=specialist_analyses,
                next_actions=(),
            )
        return DecisionCycleResult(
            status=status,
            reason=reason,
            record=record,
            specialist_analyses=specialist_analyses,
            next_actions=next_actions
            if status is DecisionStatus.RECOMMENDATION_READY
            else (),
        )

    def _now(self) -> datetime:
        now = self._clock()
        _require_aware(now, "Lead Manager clock")
        return now


def _pending_action(
    snapshot: LeagueSnapshot, action: RecommendedAction, expires_at: datetime
) -> PendingAction:
    """Build a deterministic opaque reference from the validated action intent."""

    identity = {
        "season": snapshot.settings.season,
        "league_id": snapshot.settings.league_id,
        "kind": action.kind,
        "summary": action.summary,
        "rationale": action.rationale,
        "affected_player_ids": sorted(action.affected_player_ids),
        "expires_at": expires_at.isoformat(),
    }
    encoded = _json_encode(identity)
    action_id = f"act-{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:32]}"
    return PendingAction(
        action_id=action_id,
        kind=action.kind,
        summary=action.summary,
        expires_at=expires_at,
    )


def _recommendation_with_policy_reason(
    recommendation: LeadRecommendation, failure_reason: str
) -> LeadRecommendation:
    """Keep a policy refusal visible to ``why`` without creating an action."""

    return LeadRecommendation(
        recommendation.summary,
        recommendation.confidence,
        f"{recommendation.uncertainty}\nApproval policy: {failure_reason}.",
    )


def _parse_specialist(role: AnalysisRole, response: str) -> SpecialistAnalysis:
    payload = _json_object(response)
    _require_exact_keys(
        payload,
        {"recommendation", "reasoning", "dissent", "candidates"},
        "specialist output",
    )
    return SpecialistAnalysis(
        role=role,
        recommendation=_mapping_text(payload, "recommendation"),
        reasoning=_mapping_text(payload, "reasoning"),
        dissent=_mapping_text(payload, "dissent"),
        candidates=_parse_actions(payload.get("candidates"), "specialist candidates"),
    )


def _parse_lead(response: str) -> LeadAnalysis:
    payload = _json_object(response)
    _require_exact_keys(
        payload,
        {"summary", "confidence", "uncertainty", "arbitration", "next_actions"},
        "lead output",
    )
    confidence = payload.get("confidence")
    if not isinstance(confidence, int | float) or isinstance(confidence, bool):
        raise StructuredOutputError("lead confidence must be numeric")
    return LeadAnalysis(
        summary=_mapping_text(payload, "summary"),
        confidence=float(confidence),
        uncertainty=_mapping_text(payload, "uncertainty"),
        arbitration=_mapping_text(payload, "arbitration"),
        next_actions=_parse_actions(payload.get("next_actions"), "lead next actions"),
    )


def _parse_actions(value: object, name: str) -> tuple[RecommendedAction, ...]:
    if not isinstance(value, list):
        raise StructuredOutputError(f"{name} must be a JSON list")
    if len(value) > MAX_NEXT_ACTIONS:
        raise StructuredOutputError(f"{name} exceed the maximum")
    actions: list[RecommendedAction] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise StructuredOutputError(f"{name} entries must be objects")
        action = cast(Mapping[str, object], item)
        required = {"kind", "summary", "rationale"}
        optional = {"affected_player_ids", "approval_expires_at"}
        if not required.issubset(action) or not set(action).issubset(
            required | optional
        ):
            raise StructuredOutputError(f"{name} has an invalid schema")
        actions.append(
            RecommendedAction(
                kind=_mapping_text(action, "kind"),
                summary=_mapping_text(action, "summary"),
                rationale=_mapping_text(action, "rationale"),
                affected_player_ids=_player_ids(action.get("affected_player_ids")),
                approval_expires_at=_approval_expiry(action.get("approval_expires_at")),
            )
        )
    return tuple(actions)


def _player_ids(value: object) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise StructuredOutputError("affected_player_ids must be a JSON list")
    values: list[int] = []
    for player_id in value:
        if (
            not isinstance(player_id, int)
            or isinstance(player_id, bool)
            or player_id <= 0
        ):
            raise StructuredOutputError(
                "affected_player_ids must contain positive integers"
            )
        values.append(player_id)
    return tuple(values)


def _approval_expiry(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise StructuredOutputError("approval_expires_at must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise StructuredOutputError(
            "approval_expires_at must be an ISO-8601 timestamp"
        ) from exc
    try:
        _require_aware(parsed, "recommended action approval expiry")
    except ValueError as exc:
        raise StructuredOutputError(
            "approval_expires_at must be timezone-aware"
        ) from exc
    return parsed


def _json_object(response: str) -> Mapping[str, object]:
    try:
        parsed = json.loads(response)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError("analysis output must be JSON") from exc
    if not isinstance(parsed, Mapping):
        raise StructuredOutputError("analysis output must be a JSON object")
    return cast(Mapping[str, object], parsed)


def _require_exact_keys(
    value: Mapping[str, object], expected: set[str], name: str
) -> None:
    if set(value) != expected:
        raise StructuredOutputError(f"{name} has an invalid schema")


def _mapping_text(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise StructuredOutputError(f"{key} must be a string")
    return item


def _base_context(
    snapshot: LeagueSnapshot,
    prior_context: tuple[CuratedPriorContext, ...],
    limits: AnalysisLimits,
) -> dict[str, object]:
    context: dict[str, object] = {
        "schema_version": 1,
        "mode": "read_only",
        "normalized_espn_snapshot": snapshot.to_dict(),
        "canonical_guardrails": list(CANONICAL_GUARDRAILS),
        "curated_prior_context": [item.to_dict() for item in prior_context],
    }
    encoded = _json_encode(context)
    if len(encoded) > limits.max_context_characters:
        raise StructuredOutputError("base context exceeds the per-call limit")
    return context


def _encode_context(
    base_context: Mapping[str, object],
    *,
    role: AnalysisRole,
    analyses: tuple[SpecialistAnalysis, ...],
    limits: AnalysisLimits,
) -> str | None:
    context = dict(base_context)
    context["role"] = role.value
    if analyses:
        context["validated_specialist_analyses"] = [
            analysis.to_dict() for analysis in analyses
        ]
    encoded = _json_encode(context)
    if len(encoded) > limits.max_context_characters:
        return None
    return encoded


def _json_encode(value: Mapping[str, object]) -> str:
    try:
        return json.dumps(value, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise StructuredOutputError("analysis context could not be encoded") from exc


def _instructions_for(role: AnalysisRole) -> str:
    schema = _LEAD_SCHEMA if role is AnalysisRole.LEAD else _SPECIALIST_SCHEMA
    return (
        f"You are the {ROLE_LABELS[role]}. {ROLE_INSTRUCTIONS[role]} "
        "Use only the supplied JSON context. Return exactly one JSON object with "
        f"this schema and no markdown or prose outside it: {schema}"
    )


def _snapshot_problem(
    snapshot: LeagueSnapshot, now: datetime, limits: AnalysisLimits
) -> str | None:
    source_timestamp = snapshot.source_timestamp
    if source_timestamp.tzinfo is None:
        return "normalized snapshot timestamp is not timezone-aware"
    if source_timestamp - now > limits.max_future_snapshot_skew:
        return "normalized snapshot timestamp is implausibly in the future"
    if now - source_timestamp > limits.max_snapshot_age:
        return "normalized snapshot is stale"
    if not snapshot.settings.name.strip() or snapshot.settings.season < 2000:
        return "normalized snapshot league settings are incomplete"
    if snapshot.status.current_week < 1 or not snapshot.status.season_state.strip():
        return "normalized snapshot league status is incomplete"
    if not snapshot.teams:
        return "normalized snapshot has no league teams"
    team_ids: set[int] = set()
    roster_entries = 0
    for team in snapshot.teams:
        if team.team_id <= 0 or not team.name.strip() or team.team_id in team_ids:
            return "normalized snapshot team data is incomplete"
        team_ids.add(team.team_id)
        for entry in team.roster:
            if (
                entry.player.player_id <= 0
                or not entry.player.name.strip()
                or not entry.lineup_slot.strip()
            ):
                return "normalized snapshot roster data is incomplete"
            roster_entries += 1
    if roster_entries == 0:
        return "normalized snapshot has no roster data"
    return None


def _snapshot_facts(snapshot: LeagueSnapshot) -> tuple[ESPNFact, ...]:
    """Reduce normalized data to safe, durable facts without retaining payloads."""

    encoded = _json_encode(snapshot.to_dict())
    fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
    roster_entries = sum(len(team.roster) for team in snapshot.teams)
    return (
        ESPNFact(
            "league",
            (
                f"League {snapshot.settings.name}; season {snapshot.settings.season}; "
                f"week {snapshot.status.current_week}; "
                f"state {snapshot.status.season_state}."
            ),
            snapshot.source_timestamp,
        ),
        ESPNFact(
            "rosters",
            (
                f"{len(snapshot.teams)} teams and {roster_entries} normalized "
                "roster entries were supplied."
            ),
            snapshot.source_timestamp,
        ),
        ESPNFact(
            "schedule_and_market",
            (
                f"{len(snapshot.matchups)} matchups, "
                f"{len(snapshot.status.deadlines)} deadlines, "
                f"{len(snapshot.transactions)} transactions, and "
                f"{len(snapshot.free_agents)} free agents were supplied."
            ),
            snapshot.source_timestamp,
        ),
        ESPNFact(
            "snapshot_freshness",
            (
                "Normalized snapshot timestamp "
                f"{snapshot.source_timestamp.isoformat()} "
                f"with fingerprint {fingerprint}."
            ),
            snapshot.source_timestamp,
        ),
    )


def _invalid_timestamp_facts(now: datetime) -> tuple[ESPNFact, ...]:
    """Record malformed snapshot time safely without copying it into the ledger."""

    return (
        ESPNFact(
            "snapshot_validation",
            "Normalized snapshot source timestamp is not timezone-aware.",
            now,
        ),
    )


def _failed_specialist_opinion(role: AnalysisRole, failure: str) -> SpecialistOpinion:
    return SpecialistOpinion(
        specialist=ROLE_LABELS[role],
        recommendation="No recommendation",
        reasoning="No valid structured analysis was available after bounded retries.",
        dissent=f"Fail closed: {failure}.",
    )


def _why_from_record(
    record: DecisionRecord, *, next_actions: tuple[RecommendedAction, ...] = ()
) -> WhyReadyResult:
    status = (
        DecisionStatus.NO_ACTION
        if record.cycle.recommendation.summary.startswith("No action:")
        else DecisionStatus.RECOMMENDATION_READY
    )
    return WhyReadyResult(
        decision_id=record.cycle.decision_id,
        status=status,
        recommendation=record.cycle.recommendation,
        espn_facts=record.cycle.espn_facts,
        specialist_opinions=record.cycle.specialist_opinions,
        next_actions=next_actions,
    )


def _required_text(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise StructuredOutputError(f"{name} must be a string")
    safe = redact_text(value).strip()
    if not safe:
        raise StructuredOutputError(f"{name} must not be empty")
    if len(safe) > maximum:
        raise StructuredOutputError(f"{name} exceeds its bounded length")
    return safe


def _optional_text(value: object, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise StructuredOutputError(f"{name} must be a string")
    safe = redact_text(value).strip()
    if len(safe) > maximum:
        raise StructuredOutputError(f"{name} exceeds its bounded length")
    return safe


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None:
        raise ValueError(f"{name} timestamp must be timezone-aware")
