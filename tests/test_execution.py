"""Fake-only safety and verification tests for the ESPN action foundation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fantasy_football.execution import (
    ActionAuthorizer,
    ActionRequest,
    ActionState,
    BrowserTimeoutError,
    EvidenceCapture,
    EvidenceRedactor,
    ExecutionPhase,
    InMemoryActionLedger,
    InMemoryEvidenceStore,
    JsonlActionLedger,
    NavigationFailure,
    PlaywrightESPNActionExecutor,
    RuntimeWriteState,
    redact_sensitive,
)

NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)


def request(
    *,
    key: str = "idem-1",
    approval_required: bool = False,
    approved_by: int | None = None,
    expiry: datetime | None = None,
) -> ActionRequest:
    return ActionRequest(
        action_id="act-opaque-1",
        kind="fake-action",
        season=2026,
        league_id=7,
        idempotency_key=key,
        precondition_fingerprint="version-1",
        approval_required=approval_required,
        approved_by=approved_by,
        approval_expires_at=expiry,
        redacted_intent="Set a fake state",
    )


@dataclass
class FakeBrowser:
    dispatch_error: Exception | None = None
    dispatches: int = 0
    captures: list[ExecutionPhase] | None = None
    closed: bool = False

    def __post_init__(self) -> None:
        self.captures = []

    def dispatch(self, action: ActionRequest) -> None:
        self.dispatches += 1
        if self.dispatch_error:
            raise self.dispatch_error

    def capture_evidence(self, phase: ExecutionPhase) -> EvidenceCapture:
        assert self.captures is not None
        self.captures.append(phase)
        return EvidenceCapture(
            screenshot="safe-reference",
            dom_excerpt=(
                "cookie: super-secret; token=token-secret; visible fake state"
            ),
        )

    def close(self) -> None:
        self.closed = True


class FakeVerification:
    def __init__(self, value: object = True, error: Exception | None = None) -> None:
        self.value = value
        self.error = error
        self.refreshes = 0

    def refresh(self, action: ActionRequest) -> object:
        self.refreshes += 1
        if self.error:
            raise self.error
        return self.value


def make_executor(
    browser: FakeBrowser | None,
    *,
    enabled: bool = True,
    ledger: InMemoryActionLedger | JsonlActionLedger | None = None,
    verification: FakeVerification | None = None,
    state: RuntimeWriteState | None = None,
    now: datetime = NOW,
    reviews: list[object] | None = None,
) -> tuple[
    PlaywrightESPNActionExecutor,
    FakeVerification,
    Callable[[], int],
]:
    created = 0

    def factory() -> FakeBrowser:
        nonlocal created
        created += 1
        assert browser is not None
        return browser

    control = state or RuntimeWriteState(global_enabled=enabled)
    authorizer = ActionAuthorizer(
        state=control,
        authorized_user_id=42,
        clock=lambda: now,
    )
    verifier = verification or FakeVerification()
    executor = PlaywrightESPNActionExecutor(
        browser_factory=factory,
        authorizer=authorizer,
        ledger=ledger or InMemoryActionLedger(),
        verification_port=verifier,
        evidence_store=InMemoryEvidenceStore(),
        evidence_redactor=EvidenceRedactor(
            sensitive_values=("super-secret", "token-secret")
        ),
        clock=lambda: now,
        review_sink=reviews.append if reviews is not None else None,
    )
    return executor, verifier, lambda: created


def test_default_disabled_writes_reject_before_browser_construction() -> None:
    executor, _, created = make_executor(FakeBrowser(), enabled=False)

    result = executor.execute(request(), current_precondition="version-1")

    assert result.state is ActionState.REJECTED
    assert "disabled" in result.reason
    assert created() == 0


@pytest.mark.parametrize(
    "action,expected",
    [
        (request(approval_required=True), "approval is required"),
        (
            request(
                approval_required=True,
                approved_by=42,
                expiry=NOW - timedelta(seconds=1),
            ),
            "expired",
        ),
        (request(), "stale"),
    ],
)
def test_invalid_authorization_and_stale_preconditions_reject_without_browser(
    action: ActionRequest, expected: str
) -> None:
    executor, _, created = make_executor(FakeBrowser())
    current = "version-1" if expected != "stale" else "version-0"

    result = executor.execute(action, current_precondition=current)

    assert result.state is ActionState.REJECTED
    assert expected in result.reason
    assert created() == 0


def test_valid_expiring_authorization_is_required_and_accepted() -> None:
    browser = FakeBrowser()
    executor, _, _ = make_executor(browser)
    approved = request(
        approval_required=True,
        approved_by=42,
        expiry=NOW + timedelta(minutes=15),
    )

    result = executor.execute(approved, current_precondition="version-1")

    assert result.state is ActionState.VERIFIED
    assert browser.dispatches == 1


def test_enabled_execution_reserves_dispatches_refreshes_verifies_and_redacts() -> None:
    browser = FakeBrowser()
    ledger = InMemoryActionLedger()
    executor, verification, _ = make_executor(
        browser, ledger=ledger, verification=FakeVerification("ok")
    )

    result = executor.execute(
        request(),
        current_precondition="version-1",
        verifier=lambda value: value == "ok",
    )

    assert result.state is ActionState.VERIFIED
    assert result.verification_passed is True
    assert verification.refreshes == 1
    assert browser.dispatches == 1
    assert browser.captures == [
        ExecutionPhase.BEFORE_DISPATCH,
        ExecutionPhase.DISPATCHED,
        ExecutionPhase.REFRESHED,
    ]
    assert all(entry.recorded_at.tzinfo is not None for entry in ledger.entries)
    assert ledger.entries[-1].result == result
    assert ledger.entries[-1].payload["evidence_references"]
    with pytest.raises(TypeError):
        ledger.entries[-1].payload["evidence_references"] = []  # type: ignore[index]


def test_idempotent_replay_does_not_redispatch_verified_action() -> None:
    browser = FakeBrowser()
    executor, _, _ = make_executor(browser)
    first = executor.execute(request(), current_precondition="version-1")
    second = executor.execute(request(), current_precondition="version-0")

    assert first == second
    assert browser.dispatches == 1


def test_timeout_pauses_action_emits_review_and_cannot_retry() -> None:
    browser = FakeBrowser(dispatch_error=BrowserTimeoutError("timed out"))
    reviews: list[object] = []
    executor, _, created = make_executor(browser, reviews=reviews)

    first = executor.execute(request(), current_precondition="version-1")
    second = executor.execute(request(), current_precondition="version-1")

    assert first.state is ActionState.UNKNOWN_OUTCOME
    assert first.review_required is True
    assert first.fresh_read_required is True
    assert second == first
    assert len(reviews) == 1
    assert created() == 1
    assert executor.authorizer.state.requires_fresh_read("act-opaque-1")


def test_navigation_failure_and_failed_predicate_fail_closed() -> None:
    browser = FakeBrowser(dispatch_error=NavigationFailure("navigation failed"))
    executor, _, _ = make_executor(browser)
    result = executor.execute(request(), current_precondition="version-1")
    assert result.state is ActionState.UNKNOWN_OUTCOME

    browser2 = FakeBrowser()
    executor2, _, _ = make_executor(browser2)
    result2 = executor2.execute(
        request(key="idem-2"),
        current_precondition="version-1",
        verifier=lambda _: False,
    )
    assert result2.state is ActionState.UNKNOWN_OUTCOME
    assert result2.review_required is True


def test_fresh_read_is_required_before_review_release() -> None:
    browser = FakeBrowser(dispatch_error=BrowserTimeoutError("ambiguous"))
    executor, _, _ = make_executor(browser)
    result = executor.execute(request(), current_precondition="version-1")
    assert result.fresh_read_required
    with pytest.raises(ValueError):
        executor.resolve_review("act-opaque-1", fresh_read_completed=False)
    executor.resolve_review("act-opaque-1", fresh_read_completed=True)
    assert not executor.authorizer.state.requires_fresh_read("act-opaque-1")


def test_redactor_handles_credentials_query_values_and_dom_bound() -> None:
    redactor = EvidenceRedactor(
        sensitive_values=("visible-secret",), max_dom_excerpt=20
    )
    evidence = redactor.redact(
        EvidenceCapture(
            screenshot=b"raw screenshot bytes",
            dom_excerpt=(
                "SWID=visible-secret token=abc123 "
                "?espn_s2=query-secret visible-secret and more"
            ),
        )
    )
    assert evidence.screenshot == "<redacted screenshot>"
    assert "visible-secret" not in evidence.dom_excerpt
    assert "abc123" not in evidence.dom_excerpt
    assert len(evidence.dom_excerpt) == 20
    redacted = redact_sensitive(
        {"cookies": "secret", "intent": "visible-secret"}, ("visible-secret",)
    )
    assert isinstance(redacted, dict)
    assert redacted["cookies"] == "<redacted>"


def test_jsonl_ledger_is_season_partitioned_and_replayable(tmp_path: Path) -> None:
    ledger = JsonlActionLedger(tmp_path, 2026)
    browser = FakeBrowser()
    executor, _, _ = make_executor(browser, ledger=ledger)
    result = executor.execute(request(), current_precondition="version-1")

    reopened = JsonlActionLedger(tmp_path, 2026)
    assert reopened.path == tmp_path / "2026" / "action-ledger.jsonl"
    assert reopened.find("idem-1") is not None
    assert reopened.find("idem-1").result == result  # type: ignore[union-attr]
    assert reopened.path.stat().st_mode & 0o077 == 0
