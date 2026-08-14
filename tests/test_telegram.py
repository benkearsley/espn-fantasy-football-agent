"""Tests for the allowlisted Telegram control plane."""

from datetime import UTC, datetime, timedelta

from fantasy_football.contracts import LeagueSettings, LeagueSnapshot, LeagueStatus
from fantasy_football.health import HealthEvent, HealthEventKind
from fantasy_football.telegram import (
    CommandRouter,
    PendingAction,
    TelegramConfig,
    TelegramService,
    format_daily_digest,
    parse_command,
)


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


def test_commands_are_allowlisted_and_action_approval_expires() -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    router = CommandRouter(42, now=lambda: now)
    action = PendingAction(
        "lineup", "Set lineup", now + timedelta(minutes=15), "act-test"
    )
    router.add_action(action)
    assert parse_command("/status@bot").command.value == "status"  # type: ignore[union-attr]
    assert router.handle(99, "status") is None
    approval = router.handle(42, "approve act-test")
    assert approval is not None and "act-test" in approval
    expired = PendingAction("lineup", "Expired", now - timedelta(seconds=1), "act-old")
    router.add_action(expired)
    expired_reply = router.handle(42, "approve act-old")
    assert expired_reply is not None and "expired" in expired_reply


def test_mock_daily_digest_is_sent_with_action_reference_and_health_alert() -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    action = PendingAction(
        "trade", "Accept trade", now + timedelta(minutes=15), "act-123"
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
    service = TelegramService(transport, CommandRouter(42))
    service.poll_once()
    service.send_daily_digest(7, snapshot(), actions=iter([action]))
    service.send_health_event(
        7, HealthEvent(HealthEventKind.LOST_ACCESS, True, "lost access", now)
    )
    assert len(transport.messages) == 3
    assert "act-123" in transport.messages[1][1]
    assert "expires" in transport.messages[1][1]
    assert transport.messages[2][1].startswith("URGENT:")


def test_digest_without_actions_is_clear() -> None:
    assert "No pending approvals" in format_daily_digest(snapshot())
