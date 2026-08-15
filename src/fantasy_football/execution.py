"""Fail-closed foundation for verified ESPN write actions.

This module owns the application contract for ESPN writes.  It intentionally
does not know ESPN selectors, payloads, or provider models.  A future action
module supplies a request, a Playwright browser port, and an action-specific
postcondition predicate.  The read-only :class:`ESPNLeagueReader` remains a
separate boundary.

The implementation is useful with fakes today and with a Playwright port in a
later issue.  In particular, the executor never creates a browser until the
global switch, pause state, approval, expiry, and precondition checks pass.
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, cast, runtime_checkable

REDACTED = "<redacted>"
MAX_DOM_EXCERPT = 4_000
MAX_INTENT_LENGTH = 1_000


class ActionState(StrEnum):
    """Public terminal states plus the internal reservation state."""

    REJECTED = "rejected"
    VERIFIED = "verified"
    FAILED = "failed"
    UNKNOWN_OUTCOME = "unknown_outcome"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    IN_FLIGHT = "in_flight"


TERMINAL_STATES = frozenset(
    {
        ActionState.REJECTED,
        ActionState.VERIFIED,
        ActionState.FAILED,
        ActionState.UNKNOWN_OUTCOME,
        ActionState.NEEDS_HUMAN_REVIEW,
    }
)


class ExecutionPhase(StrEnum):
    AUTHORIZATION = "authorization"
    RESERVED = "reserved"
    BEFORE_DISPATCH = "before_dispatch"
    DISPATCHED = "dispatched"
    REFRESHED = "refreshed"
    VERIFIED = "verified"
    FAILED = "failed"
    UNKNOWN_OUTCOME = "unknown_outcome"


@dataclass(frozen=True, slots=True)
class ActionRequest:
    """Provider-neutral description of one intended write.

    ``redacted_intent`` is deliberately text rather than an arbitrary payload:
    child action issues own their payload contracts and must pass only a safe
    summary to this foundation.
    """

    action_id: str
    kind: str
    season: int
    league_id: int
    idempotency_key: str
    precondition_fingerprint: str
    approval_required: bool = False
    approved_by: str | int | None = None
    approval_expires_at: datetime | None = None
    redacted_intent: str = ""

    def __post_init__(self) -> None:
        for name in (
            "action_id",
            "kind",
            "idempotency_key",
            "precondition_fingerprint",
        ):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        if self.league_id <= 0:
            raise ValueError("league_id must be positive")
        if not 2000 <= self.season <= 2100:
            raise ValueError("season must be between 2000 and 2100")
        if (
            self.approval_expires_at is not None
            and self.approval_expires_at.tzinfo is None
        ):
            raise ValueError("approval expiry must be timezone-aware")
        if len(self.redacted_intent) > MAX_INTENT_LENGTH:
            raise ValueError("redacted intent is too long")

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "kind": self.kind,
            "season": self.season,
            "league_id": self.league_id,
            "idempotency_key": self.idempotency_key,
            "precondition_fingerprint": self.precondition_fingerprint,
            "approval_required": self.approval_required,
            "approved_by": self.approved_by,
            "approval_expires_at": _iso(self.approval_expires_at),
            "redacted_intent": redact_text(self.redacted_intent),
        }


@dataclass(frozen=True, slots=True)
class PreconditionSnapshot:
    """Fresh read/version information used before browser creation."""

    fingerprint: str
    summary: str = ""
    captured_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.fingerprint:
            raise ValueError("precondition fingerprint must not be empty")
        if self.captured_at.tzinfo is None:
            raise ValueError("precondition timestamp must be timezone-aware")

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "summary": redact_text(self.summary),
            "captured_at": _iso(self.captured_at),
        }


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    allowed: bool
    reason: str
    decided_at: datetime
    requested_fingerprint: str
    current_fingerprint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "decided_at": _iso(self.decided_at),
            "requested_fingerprint": self.requested_fingerprint,
            "current_fingerprint": self.current_fingerprint,
        }


class RuntimeWriteState:
    """Mutable, process-local write and recovery controls."""

    def __init__(self, *, global_enabled: bool = False) -> None:
        self._global_enabled = global_enabled
        self._paused = False
        self._paused_actions: set[str] = set()
        self._fresh_read_required: set[str] = set()
        self._lock = threading.RLock()

    @property
    def global_enabled(self) -> bool:
        with self._lock:
            return self._global_enabled

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    def enable_global(self) -> None:
        with self._lock:
            self._global_enabled = True

    def disable_global(self) -> None:
        with self._lock:
            self._global_enabled = False

    def pause(self, action_id: str | None = None) -> None:
        with self._lock:
            if action_id is None:
                self._paused = True
            else:
                self._paused_actions.add(action_id)

    def resume(self, action_id: str | None = None) -> None:
        with self._lock:
            if action_id is None:
                self._paused = False
            else:
                self._paused_actions.discard(action_id)

    def pause_for_review(self, action_id: str) -> None:
        with self._lock:
            self._paused_actions.add(action_id)
            self._fresh_read_required.add(action_id)

    def record_fresh_read(self, action_id: str) -> None:
        """Clear the recovery gate only after a new ESPN read is complete."""

        with self._lock:
            self._fresh_read_required.discard(action_id)
            self._paused_actions.discard(action_id)

    def is_paused(self, action_id: str) -> bool:
        with self._lock:
            return (
                self._paused
                or action_id in self._paused_actions
                or action_id in self._fresh_read_required
            )

    def requires_fresh_read(self, action_id: str) -> bool:
        with self._lock:
            return action_id in self._fresh_read_required


class ActionAuthorizer:
    """Compose all pre-browser authorization checks."""

    def __init__(
        self,
        *,
        global_write_enabled: bool = False,
        paused: bool = False,
        authorized_user_id: str | int | None = None,
        state: RuntimeWriteState | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.state = (
            state
            if state is not None
            else RuntimeWriteState(global_enabled=global_write_enabled)
        )
        if paused:
            self.state.pause()
        self.authorized_user_id = authorized_user_id
        self._clock = clock

    def authorize(
        self,
        request: ActionRequest,
        current: PreconditionSnapshot | str | None,
        *,
        now: datetime | None = None,
    ) -> AuthorizationDecision:
        decided_at = now if now is not None else self._clock()
        current_fingerprint = (
            current.fingerprint
            if isinstance(current, PreconditionSnapshot)
            else current
        )
        if decided_at.tzinfo is None:
            raise ValueError("authorization clock must return a timezone-aware time")
        if not self.state.global_enabled:
            return self._deny(
                request, "global writes are disabled", decided_at, current_fingerprint
            )
        if self.state.is_paused(request.action_id):
            if self.state.requires_fresh_read(request.action_id):
                reason = "action is paused pending a fresh ESPN read and human review"
            else:
                reason = "writes are paused"
            return self._deny(request, reason, decided_at, current_fingerprint)
        if current_fingerprint is None:
            return self._deny(
                request, "current precondition snapshot is required", decided_at, None
            )
        if current_fingerprint != request.precondition_fingerprint:
            return self._deny(
                request,
                "precondition fingerprint is stale",
                decided_at,
                current_fingerprint,
            )
        if request.approval_required:
            if request.approved_by is None:
                return self._deny(
                    request,
                    "authorized user approval is required",
                    decided_at,
                    current_fingerprint,
                )
            if self.authorized_user_id is None or str(request.approved_by) != str(
                self.authorized_user_id
            ):
                return self._deny(
                    request,
                    "approval is not from the authorized user",
                    decided_at,
                    current_fingerprint,
                )
            if (
                request.approval_expires_at is None
                or request.approval_expires_at <= decided_at
            ):
                return self._deny(
                    request, "approval has expired", decided_at, current_fingerprint
                )
        return AuthorizationDecision(
            allowed=True,
            reason="authorized",
            decided_at=decided_at,
            requested_fingerprint=request.precondition_fingerprint,
            current_fingerprint=current_fingerprint,
        )

    @staticmethod
    def _deny(
        request: ActionRequest,
        reason: str,
        decided_at: datetime,
        current_fingerprint: str | None,
    ) -> AuthorizationDecision:
        return AuthorizationDecision(
            allowed=False,
            reason=reason,
            decided_at=decided_at,
            requested_fingerprint=request.precondition_fingerprint,
            current_fingerprint=current_fingerprint,
        )


@dataclass(frozen=True, slots=True)
class EvidenceCapture:
    """Evidence returned by a browser port.

    Browser ports must return a screenshot artifact already safe to persist or
    an opaque reference.  Raw network payloads are not part of this contract.
    Text is redacted again by the evidence store as defense in depth.
    """

    screenshot: str | bytes | None = None
    dom_excerpt: str = ""


@dataclass(frozen=True, slots=True)
class RedactedEvidence:
    screenshot: str | None
    dom_excerpt: str


class EvidenceRedactor:
    """Bound and redact browser evidence before it reaches a ledger/store."""

    def __init__(
        self,
        *,
        sensitive_values: Iterable[str] = (),
        max_dom_excerpt: int = MAX_DOM_EXCERPT,
    ) -> None:
        if max_dom_excerpt <= 0:
            raise ValueError("max_dom_excerpt must be positive")
        self._sensitive_values = tuple(value for value in sensitive_values if value)
        self._max_dom_excerpt = max_dom_excerpt

    def redact(
        self, evidence: EvidenceCapture | Mapping[str, Any] | None
    ) -> RedactedEvidence:
        if evidence is None:
            return RedactedEvidence(None, "")
        if isinstance(evidence, Mapping):
            screenshot = evidence.get("screenshot")
            dom_excerpt = evidence.get("dom_excerpt", evidence.get("dom", ""))
        else:
            screenshot = evidence.screenshot
            dom_excerpt = evidence.dom_excerpt
        screenshot_text: str | None
        if isinstance(screenshot, bytes):
            # Binary screenshots cannot be safely string-redacted.  Persist an
            # opaque marker; a real port may instead provide a safe reference.
            screenshot_text = "<redacted screenshot>"
        elif screenshot is None:
            screenshot_text = None
        else:
            screenshot_text = redact_text(str(screenshot), self._sensitive_values)
        safe_dom = redact_text(str(dom_excerpt), self._sensitive_values)
        return RedactedEvidence(
            screenshot=screenshot_text,
            dom_excerpt=safe_dom[: self._max_dom_excerpt],
        )


@dataclass(frozen=True, slots=True)
class ReviewSignal:
    action_id: str
    reason: str
    emitted_at: datetime
    fresh_read_required: bool = True


@dataclass(frozen=True, slots=True)
class ActionResult:
    action_id: str
    idempotency_key: str
    state: ActionState
    reason: str
    completed_at: datetime
    evidence_references: tuple[str, ...] = ()
    verification_passed: bool | None = None
    recovery_status: str = "none"
    review_required: bool = False
    fresh_read_required: bool = False

    @property
    def status(self) -> ActionState:
        """Alias that reads naturally for callers using status terminology."""

        return self.state

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "idempotency_key": self.idempotency_key,
            "state": self.state.value,
            "reason": self.reason,
            "completed_at": _iso(self.completed_at),
            "evidence_references": list(self.evidence_references),
            "verification_passed": self.verification_passed,
            "recovery_status": self.recovery_status,
            "review_required": self.review_required,
            "fresh_read_required": self.fresh_read_required,
        }


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    sequence: int
    event: str
    recorded_at: datetime
    action_id: str
    idempotency_key: str
    state: ActionState
    payload: Mapping[str, Any]
    result: ActionResult | None = None

    def __post_init__(self) -> None:
        if self.recorded_at.tzinfo is None:
            raise ValueError("ledger timestamp must be timezone-aware")
        object.__setattr__(self, "payload", _freeze_mapping(self.payload))

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES


@runtime_checkable
class ActionLedger(Protocol):
    def find(self, idempotency_key: str) -> LedgerEntry | None:
        """Return the latest immutable entry for an idempotency key."""

    def reserve(self, request: ActionRequest, recorded_at: datetime) -> LedgerEntry:
        """Atomically reserve an idempotency key before browser interaction."""

    def append(self, entry: LedgerEntry) -> None:
        """Append an immutable event; never update or delete prior events."""


class InMemoryActionLedger:
    """Deterministic fake ledger for unit tests and local dry runs."""

    def __init__(self) -> None:
        self._entries: list[LedgerEntry] = []
        self._latest: dict[str, LedgerEntry] = {}
        self._lock = threading.RLock()

    @property
    def entries(self) -> tuple[LedgerEntry, ...]:
        with self._lock:
            return tuple(self._entries)

    @property
    def records(self) -> tuple[LedgerEntry, ...]:
        return self.entries

    def find(self, idempotency_key: str) -> LedgerEntry | None:
        with self._lock:
            return self._latest.get(idempotency_key)

    def reserve(self, request: ActionRequest, recorded_at: datetime) -> LedgerEntry:
        with self._lock:
            existing = self._latest.get(request.idempotency_key)
            if existing is not None:
                return existing
            entry = LedgerEntry(
                sequence=len(self._entries) + 1,
                event="reserved",
                recorded_at=recorded_at,
                action_id=request.action_id,
                idempotency_key=request.idempotency_key,
                state=ActionState.IN_FLIGHT,
                payload={"request": request.to_dict()},
            )
            self._append_locked(entry)
            return entry

    def append(self, entry: LedgerEntry) -> None:
        with self._lock:
            self._append_locked(entry)

    def _append_locked(self, entry: LedgerEntry) -> None:
        if entry.sequence != len(self._entries) + 1:
            raise ValueError("ledger sequence must be append-only")
        self._entries.append(entry)
        self._latest[entry.idempotency_key] = entry


class JsonlActionLedger(InMemoryActionLedger):
    """Owner-only, append-only season-partitioned JSONL ledger."""

    def __init__(self, data_dir: Path, season: int) -> None:
        if not data_dir.is_absolute():
            raise ValueError("ledger data_dir must be absolute")
        if not 2000 <= season <= 2100:
            raise ValueError("season must be between 2000 and 2100")
        super().__init__()
        self.path = data_dir / str(season) / "action-ledger.jsonl"
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        if self.path.exists():
            self._load()
        self.path.touch(mode=0o600, exist_ok=True)
        os.chmod(self.path, 0o600)

    def _load(self) -> None:
        try:
            with self.path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    self._load_entry(json.loads(line))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RuntimeError("action ledger is unreadable") from exc

    def _load_entry(self, value: Mapping[str, Any]) -> None:
        result_value = value.get("result")
        result = (
            _result_from_dict(result_value)
            if isinstance(result_value, Mapping)
            else None
        )
        entry = LedgerEntry(
            sequence=int(value["sequence"]),
            event=str(value["event"]),
            recorded_at=_datetime(value["recorded_at"]),
            action_id=str(value["action_id"]),
            idempotency_key=str(value["idempotency_key"]),
            state=ActionState(str(value["state"])),
            payload=value.get("payload", {}),
            result=result,
        )
        self._append_locked(entry)

    def reserve(self, request: ActionRequest, recorded_at: datetime) -> LedgerEntry:
        with self._lock:
            existing = self._latest.get(request.idempotency_key)
            if existing is not None:
                return existing
            entry = LedgerEntry(
                sequence=len(self._entries) + 1,
                event="reserved",
                recorded_at=recorded_at,
                action_id=request.action_id,
                idempotency_key=request.idempotency_key,
                state=ActionState.IN_FLIGHT,
                payload={"request": request.to_dict()},
            )
            self._persist(entry)
            self._append_locked(entry)
            return entry

    def append(self, entry: LedgerEntry) -> None:
        with self._lock:
            if self._latest.get(entry.idempotency_key) is entry:
                return
            self._persist(entry)
            self._append_locked(entry)

    def _persist(self, entry: LedgerEntry) -> None:
        payload = {
            "sequence": entry.sequence,
            "event": entry.event,
            "recorded_at": _iso(entry.recorded_at),
            "action_id": entry.action_id,
            "idempotency_key": entry.idempotency_key,
            "state": entry.state.value,
            "payload": _json_safe(entry.payload),
            "result": entry.result.to_dict() if entry.result else None,
        }
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(self.path, 0o600)
        except (OSError, TypeError, ValueError) as exc:
            raise RuntimeError("unable to append action ledger") from exc


@runtime_checkable
class BrowserPort(Protocol):
    def dispatch(self, request: ActionRequest) -> None:
        """Submit the already-authorized action exactly once."""

    def capture_evidence(
        self, phase: ExecutionPhase
    ) -> EvidenceCapture | Mapping[str, Any] | None:
        """Return bounded evidence for an allowed execution phase."""

    def close(self) -> None:
        """Release browser resources."""


@runtime_checkable
class VerificationPort(Protocol):
    def refresh(self, request: ActionRequest) -> object:
        """Perform a fresh read after dispatch."""


@runtime_checkable
class ESPNActionExecutor(Protocol):
    def execute(
        self,
        request: ActionRequest,
        *,
        current_precondition: PreconditionSnapshot | str | None = None,
        verifier: Callable[[object], bool] | None = None,
    ) -> ActionResult:
        """Execute once, then refresh and verify the provider-side result."""


class BrowserTimeoutError(TimeoutError):
    """The browser could not establish a determinate outcome."""


class NavigationFailure(RuntimeError):
    """Navigation failed during or after an attempted dispatch."""


class UnverifiableResponse(RuntimeError):
    """The provider response could not be checked against the predicate."""


class ActionDispatchFailure(RuntimeError):
    """A known dispatch failure before the provider accepted a write."""


class PlaywrightESPNActionExecutor:
    """Provider-isolated executor accepting an injected Playwright browser port.

    This class does not import Playwright or the read adapter.  The later ESPN
    action issues supply a port that wraps a Playwright page/context and owns
    selectors.  Keeping the factory injectable is what lets this foundation
    prove all safety behavior with fakes and zero authenticated mutation.
    """

    def __init__(
        self,
        *,
        browser_factory: Callable[[], BrowserPort],
        authorizer: ActionAuthorizer,
        ledger: ActionLedger,
        verification_port: VerificationPort | None = None,
        precondition_reader: Callable[[ActionRequest], PreconditionSnapshot]
        | None = None,
        evidence_store: EvidenceStore | None = None,
        evidence_redactor: EvidenceRedactor | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        review_sink: Callable[[ReviewSignal], None] | None = None,
    ) -> None:
        self._browser_factory = browser_factory
        self._authorizer = authorizer
        self._ledger = ledger
        self._verification_port = verification_port
        self._precondition_reader = precondition_reader
        self._evidence_store = evidence_store or InMemoryEvidenceStore()
        self._evidence_redactor = evidence_redactor or EvidenceRedactor()
        self._clock = clock
        self._review_sink = review_sink
        self.review_signals: list[ReviewSignal] = []

    @property
    def authorizer(self) -> ActionAuthorizer:
        return self._authorizer

    def execute(
        self,
        request: ActionRequest,
        *,
        current_precondition: PreconditionSnapshot | str | None = None,
        verifier: Callable[[object], bool] | None = None,
    ) -> ActionResult:
        now = self._clock()
        previous = self._ledger.find(request.idempotency_key)
        if previous is not None:
            if previous.result is not None and previous.state in TERMINAL_STATES:
                return previous.result
            # An in-flight record is never retried automatically.  Treat it as
            # unresolved and require human review instead of constructing a
            # second browser.
            return self._unresolved_replay(request, previous, now)

        snapshot = self._read_precondition(request, current_precondition)
        current = snapshot if snapshot is not None else None
        # Reserve before authorization is recorded so the idempotency key is
        # durable even for a rejected attempt and is never claimed twice by
        # concurrent callers.
        reserved = self._ledger.reserve(request, now)
        if reserved.result is not None and reserved.state in TERMINAL_STATES:
            return reserved.result
        if reserved.event != "reserved":
            return self._unresolved_replay(request, reserved, now)
        decision = self._authorizer.authorize(request, current, now=now)
        self._append_event(
            request,
            event="authorization",
            state=ActionState.IN_FLIGHT if decision.allowed else ActionState.REJECTED,
            payload={
                "request": request.to_dict(),
                "authorization": decision.to_dict(),
                "precondition": snapshot.to_dict() if snapshot else None,
                "phase": ExecutionPhase.AUTHORIZATION.value,
            },
            result=None,
            recorded_at=now,
        )
        if not decision.allowed:
            result = self._result(
                request,
                ActionState.REJECTED,
                decision.reason,
                now,
            )
            self._append_final(request, result, decision=decision, snapshot=snapshot)
            return result

        self._append_event(
            request,
            event="phase",
            state=ActionState.IN_FLIGHT,
            payload={
                "request": request.to_dict(),
                "phase": ExecutionPhase.RESERVED.value,
                "precondition": snapshot.to_dict() if snapshot else None,
            },
            result=None,
            recorded_at=now,
        )

        browser: BrowserPort | None = None
        evidence_references: list[str] = []
        dispatch_started = False
        try:
            # This is the first browser-related operation in the whole method.
            browser = self._browser_factory()
            evidence_references.extend(
                self._capture(browser, request, ExecutionPhase.BEFORE_DISPATCH)
            )
            self._append_event(
                request,
                event="phase",
                state=ActionState.IN_FLIGHT,
                payload={
                    "phase": ExecutionPhase.BEFORE_DISPATCH.value,
                    "evidence_references": evidence_references,
                    "precondition": snapshot.to_dict() if snapshot else None,
                },
                result=None,
                recorded_at=self._clock(),
            )
            dispatch_started = True
            browser.dispatch(request)
            evidence_references.extend(
                self._capture(browser, request, ExecutionPhase.DISPATCHED)
            )
            self._append_event(
                request,
                event="phase",
                state=ActionState.IN_FLIGHT,
                payload={
                    "phase": ExecutionPhase.DISPATCHED.value,
                    "evidence_references": evidence_references,
                },
                result=None,
                recorded_at=self._clock(),
            )
        except (BrowserTimeoutError, NavigationFailure, UnverifiableResponse) as exc:
            self._safe_close(browser)
            return self._unknown(
                request,
                str(exc),
                evidence_references,
                dispatch_started=True,
                snapshot=snapshot,
            )
        except ActionDispatchFailure as exc:
            self._safe_close(browser)
            result = self._result(
                request,
                ActionState.FAILED,
                str(exc),
                self._clock(),
                evidence_references=evidence_references,
                verification_passed=False,
            )
            self._append_final(request, result, snapshot=snapshot)
            return result
        except Exception as exc:  # pragma: no cover - defensive fail-closed path
            self._safe_close(browser)
            return self._unknown(
                request,
                f"browser execution was indeterminate: {type(exc).__name__}",
                evidence_references,
                dispatch_started=dispatch_started,
                snapshot=snapshot,
            )
        if self._verification_port is None:
            self._safe_close(browser)
            return self._unknown(
                request,
                "post-action verification port is unavailable",
                evidence_references,
                dispatch_started=True,
                snapshot=snapshot,
            )
        try:
            refreshed = self._verification_port.refresh(request)
            if browser is not None:
                evidence_references.extend(
                    self._capture(browser, request, ExecutionPhase.REFRESHED)
                )
            self._append_event(
                request,
                event="phase",
                state=ActionState.IN_FLIGHT,
                payload={
                    "phase": ExecutionPhase.REFRESHED.value,
                    "evidence_references": evidence_references,
                },
                result=None,
                recorded_at=self._clock(),
            )
            predicate = verifier or (lambda value: bool(value))
            verified = predicate(refreshed)
        except (BrowserTimeoutError, NavigationFailure, UnverifiableResponse) as exc:
            self._safe_close(browser)
            return self._unknown(
                request,
                str(exc),
                evidence_references,
                dispatch_started=True,
                snapshot=snapshot,
            )
        except Exception as exc:  # predicate errors are unverifiable, never success
            self._safe_close(browser)
            return self._unknown(
                request,
                f"post-action verification was indeterminate: {type(exc).__name__}",
                evidence_references,
                dispatch_started=True,
                snapshot=snapshot,
            )
        if not verified:
            self._safe_close(browser)
            return self._unknown(
                request,
                "post-action predicate did not pass",
                evidence_references,
                dispatch_started=True,
                snapshot=snapshot,
            )
        self._safe_close(browser)
        result = self._result(
            request,
            ActionState.VERIFIED,
            "post-action predicate passed",
            self._clock(),
            evidence_references=evidence_references,
            verification_passed=True,
        )
        self._append_final(request, result, snapshot=snapshot)
        return result

    def resolve_review(self, action_id: str, *, fresh_read_completed: bool) -> None:
        """Release an unknown-outcome action only after a fresh read."""

        if not fresh_read_completed:
            raise ValueError("a fresh ESPN read is required before resolution")
        self._authorizer.state.record_fresh_read(action_id)

    def _read_precondition(
        self,
        request: ActionRequest,
        supplied: PreconditionSnapshot | str | None,
    ) -> PreconditionSnapshot | None:
        if isinstance(supplied, PreconditionSnapshot):
            return supplied
        if isinstance(supplied, str):
            return PreconditionSnapshot(supplied)
        if self._precondition_reader is None:
            return None
        try:
            return self._precondition_reader(request)
        except Exception:
            return None

    def _capture(
        self,
        browser: BrowserPort,
        request: ActionRequest,
        phase: ExecutionPhase,
    ) -> list[str]:
        try:
            evidence = browser.capture_evidence(phase)
            safe = self._evidence_redactor.redact(evidence)
            return [
                self._evidence_store.persist(
                    request.action_id,
                    phase,
                    safe,
                )
            ]
        except Exception:
            return [f"evidence-unavailable:{phase.value}"]

    @staticmethod
    def _safe_close(browser: BrowserPort | None) -> None:
        if browser is None:
            return
        try:
            browser.close()
        except Exception:
            # Closing a browser must not erase the already-recorded outcome or
            # cause an automatic retry.
            pass

    def _unknown(
        self,
        request: ActionRequest,
        reason: str,
        evidence_references: Sequence[str],
        *,
        dispatch_started: bool,
        snapshot: PreconditionSnapshot | None,
    ) -> ActionResult:
        _ = dispatch_started
        now = self._clock()
        self._authorizer.state.pause_for_review(request.action_id)
        signal = ReviewSignal(request.action_id, reason, now)
        self.review_signals.append(signal)
        if self._review_sink is not None:
            self._review_sink(signal)
        result = self._result(
            request,
            ActionState.UNKNOWN_OUTCOME,
            reason,
            now,
            evidence_references=evidence_references,
            verification_passed=False,
            recovery_status="paused_pending_fresh_read_and_human_review",
            review_required=True,
            fresh_read_required=True,
        )
        self._append_final(request, result, snapshot=snapshot)
        return result

    def _unresolved_replay(
        self,
        request: ActionRequest,
        previous: LedgerEntry,
        now: datetime,
    ) -> ActionResult:
        if previous.result is not None:
            return previous.result
        result = self._result(
            request,
            ActionState.NEEDS_HUMAN_REVIEW,
            "idempotency key is already in flight; automatic retry is forbidden",
            now,
            recovery_status="manual_review_required",
            review_required=True,
            fresh_read_required=True,
        )
        self._authorizer.state.pause_for_review(request.action_id)
        self._append_final(request, result, snapshot=None)
        return result

    def _result(
        self,
        request: ActionRequest,
        state: ActionState,
        reason: str,
        completed_at: datetime,
        *,
        evidence_references: Sequence[str] = (),
        verification_passed: bool | None = None,
        recovery_status: str = "none",
        review_required: bool = False,
        fresh_read_required: bool = False,
    ) -> ActionResult:
        return ActionResult(
            action_id=request.action_id,
            idempotency_key=request.idempotency_key,
            state=state,
            reason=reason,
            completed_at=completed_at,
            evidence_references=tuple(evidence_references),
            verification_passed=verification_passed,
            recovery_status=recovery_status,
            review_required=review_required,
            fresh_read_required=fresh_read_required,
        )

    def _append_final(
        self,
        request: ActionRequest,
        result: ActionResult,
        *,
        decision: AuthorizationDecision | None = None,
        snapshot: PreconditionSnapshot | None,
    ) -> None:
        self._append_event(
            request,
            event="result",
            state=result.state,
            payload={
                "request": request.to_dict(),
                "authorization": decision.to_dict() if decision else None,
                "precondition": snapshot.to_dict() if snapshot else None,
                "phase": result.state.value,
                "verification": result.verification_passed,
                "evidence_references": list(result.evidence_references),
                "recovery_status": result.recovery_status,
                "reason": result.reason,
            },
            result=result,
            recorded_at=result.completed_at,
        )

    def _append_event(
        self,
        request: ActionRequest,
        *,
        event: str,
        state: ActionState,
        payload: Mapping[str, Any],
        result: ActionResult | None,
        recorded_at: datetime,
    ) -> None:
        latest = self._ledger.find(request.idempotency_key)
        sequence = getattr(latest, "sequence", 0) + 1
        self._ledger.append(
            LedgerEntry(
                sequence=sequence,
                event=event,
                recorded_at=recorded_at,
                action_id=request.action_id,
                idempotency_key=request.idempotency_key,
                state=state,
                payload=payload,
                result=result,
            )
        )


# A shorter spelling is convenient for callers and keeps the provider name
# visible in the canonical class name above.
PlaywrightActionExecutor = PlaywrightESPNActionExecutor


class EvidenceStore(Protocol):
    def persist(
        self, action_id: str, phase: ExecutionPhase, evidence: RedactedEvidence
    ) -> str:
        """Persist only already-redacted evidence and return an opaque reference."""


class InMemoryEvidenceStore:
    def __init__(self) -> None:
        self.items: list[tuple[str, ExecutionPhase, RedactedEvidence]] = []

    def persist(
        self, action_id: str, phase: ExecutionPhase, evidence: RedactedEvidence
    ) -> str:
        self.items.append((action_id, phase, evidence))
        return f"memory:{len(self.items)}"


def redact_text(text: str, sensitive_values: Iterable[str] = ()) -> str:
    """Redact credentials, session material, query credentials, and known values."""

    result = str(text)
    for value in sensitive_values:
        if value:
            result = result.replace(value, REDACTED)
    key_pattern = (
        r"(?:espn_s2|swid|s2|token|password|secret|cookie|authorization|credential)"
    )
    result = re.sub(
        rf"(?i)(\b{key_pattern}\b\s*[:=]\s*)([^\s,;&<>]+)",
        rf"\1{REDACTED}",
        result,
    )
    result = re.sub(
        r"(?i)([?&](?:espn_s2|swid|s2|token|password|secret|credential)=)[^&#\s]+",
        rf"\1{REDACTED}",
        result,
    )
    result = re.sub(
        r"(?i)(cookie\s*:\s*)([^\r\n]+)",
        rf"\1{REDACTED}",
        result,
    )
    return result


def redact_sensitive(value: object, sensitive_values: Iterable[str] = ()) -> object:
    """Recursively redact mapping keys and string leaves for tests/callers."""

    sensitive_keys = re.compile(
        r"(?:cookie|token|password|secret|swid|espn[_-]?s2|credential|authorization)",
        re.IGNORECASE,
    )
    if isinstance(value, Mapping):
        return {
            str(key): REDACTED
            if sensitive_keys.search(str(key))
            else redact_sensitive(item, sensitive_values)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [redact_sensitive(item, sensitive_values) for item in value]
    if isinstance(value, str):
        return redact_text(value, sensitive_values)
    if isinstance(value, bytes):
        return "<redacted binary>"
    return value


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid ledger timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("ledger timestamp is not timezone-aware")
    return parsed


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    # Copy caller-owned containers and recursively freeze them.  A shallow
    # frozen outer mapping would still let a caller mutate nested evidence.
    safe = json.loads(json.dumps(_json_safe(value), sort_keys=True))
    return cast(Mapping[str, Any], _freeze_value(safe))


def _freeze_value(value: object) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_value(item) for key, item in value.items()}
        )
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return tuple(_freeze_value(item) for item in value)
    return value


def _json_safe(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, StrEnum):
        return value.value
    return value


def _result_from_dict(value: Mapping[str, Any]) -> ActionResult:
    return ActionResult(
        action_id=str(value["action_id"]),
        idempotency_key=str(value["idempotency_key"]),
        state=ActionState(str(value["state"])),
        reason=str(value["reason"]),
        completed_at=_datetime(value["completed_at"]),
        evidence_references=tuple(
            str(item) for item in value.get("evidence_references", [])
        ),
        verification_passed=value.get("verification_passed"),
        recovery_status=str(value.get("recovery_status", "none")),
        review_required=bool(value.get("review_required", False)),
        fresh_read_required=bool(value.get("fresh_read_required", False)),
    )
