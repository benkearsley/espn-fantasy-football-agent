"""Tests for the allowlisted Telegram control plane."""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs
from urllib.request import Request

import pytest

import fantasy_football.telegram as telegram
from fantasy_football.contracts import LeagueSettings, LeagueSnapshot, LeagueStatus
from fantasy_football.decisions import PendingAction
from fantasy_football.health import HealthEvent, HealthEventKind
from fantasy_football.telegram import (
    CommandRouter,
    TelegramConfig,
    TelegramService,
    TelegramTransport,
    format_daily_digest,
    parse_command,
)


class FakeWorkflow:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def status(self) -> str:
        self.calls.append(("status",))
        return "durable status"

    def run(self) -> str:
        self.calls.append(("run",))
        return "durable run"

    def why(self, decision_id: str = "") -> str:
        self.calls.append(("why", decision_id))
        return "durable why"

    def approve(self, action_id: str, *, actor_id: int) -> str:
        self.calls.append(("approve", action_id, actor_id))
        return "durable approval"

    def veto(self, action_id: str, reason: str, *, actor_id: int) -> str:
        self.calls.append(("veto", action_id, reason, actor_id))
        return "durable veto"

    def pause(self) -> str:
        self.calls.append(("pause",))
        return "durable pause"

    def resume(self) -> str:
        self.calls.append(("resume",))
        return "durable resume"


class FakeTransport:
    def __init__(self, updates: list[dict[str, object]] | None = None) -> None:
        self.messages: list[tuple[int, str]] = []
        self.updates = updates or []

    def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, object]]:
        return self.updates


def snapshot() -> LeagueSnapshot:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    return LeagueSnapshot(
        settings=LeagueSettings(7, "Engine League", 2026),
        teams=(),
        matchups=(),
        status=LeagueStatus(2, "in_season"),
        draft_picks=(),
        transactions=(),
        free_agents=(),
        source_timestamp=now,
    )


def test_config_loads_telegram_secret_from_environment_without_repr_leak() -> None:
    config = TelegramConfig.from_env(
        {
            "FFM_TELEGRAM_BOT_TOKEN": "secret-token",
            "FFM_TELEGRAM_ALLOWED_USER_ID": "42",
        }
    )
    assert config.allowed_user_id == 42
    assert "secret-token" not in repr(config)


def test_bot_api_transport_constructs_generic_send_message_request_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = TelegramConfig("runtime-token", 42)
    transport = TelegramTransport(config, timeout=7)
    observed: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self) -> object:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok": true, "result": {"message_id": 1}}'

    def fake_urlopen(request: object, *, timeout: float) -> FakeResponse:
        observed["request"] = request
        observed["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(telegram, "urlopen", fake_urlopen)
    transport.send_message(123, "arbitrary runtime text")

    request = observed["request"]
    assert isinstance(request, Request)
    assert request.full_url == "https://api.telegram.org/botruntime-token/sendMessage"
    assert isinstance(request.data, bytes)
    assert parse_qs(request.data.decode()) == {
        "chat_id": ["123"],
        "text": ["arbitrary runtime text"],
    }
    assert observed["timeout"] == 7


def test_commands_are_allowlisted_and_delegate_to_persistent_workflows() -> None:
    workflow = FakeWorkflow()
    router = CommandRouter(42, workflows=workflow)
    assert parse_command("/status@bot").command.value == "status"  # type: ignore[union-attr]
    assert router.handle(99, "status") is None
    assert router.handle(42, "status") == "durable status"
    assert router.handle(42, "run") == "durable run"
    assert router.handle(42, "approve act-test") == "durable approval"
    assert router.handle(42, "veto act-test too risky") == "durable veto"
    assert router.handle(42, "veto act-test") == "durable veto"
    assert router.handle(42, "pause") == "durable pause"
    assert router.handle(42, "resume") == "durable resume"
    assert router.handle(42, "why decision-1") == "durable why"
    assert workflow.calls == [
        ("status",),
        ("run",),
        ("approve", "act-test", 42),
        ("veto", "act-test", "too risky", 42),
        ("veto", "act-test", "", 42),
        ("pause",),
        ("resume",),
        ("why", "decision-1"),
    ]


def test_mock_daily_digest_is_sent_with_action_reference_and_health_alert() -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    action = PendingAction(
        "act-digest-001", "trade", "Accept trade", now + timedelta(minutes=15)
    )
    transport = FakeTransport(
        [
            {
                "update_id": 1,
                "message": {"from": {"id": 42}, "chat": {"id": 7}, "text": "status"},
            },
            {
                "update_id": 2,
                "message": {"from": {"id": 99}, "chat": {"id": 8}, "text": "status"},
            },
        ]
    )
    service = TelegramService(transport, CommandRouter(42, workflows=FakeWorkflow()))
    service.poll_once()
    service.send_daily_digest(7, snapshot(), actions=iter([action]))
    service.send_health_event(
        7, HealthEvent(HealthEventKind.LOST_ACCESS, True, "lost access", now)
    )
    assert len(transport.messages) == 3
    assert "act-digest-001" in transport.messages[1][1]
    assert "expires" in transport.messages[1][1]
    assert transport.messages[2][1].startswith("URGENT:")


def test_digest_without_actions_is_clear() -> None:
    assert "No pending approvals" in format_daily_digest(snapshot())
