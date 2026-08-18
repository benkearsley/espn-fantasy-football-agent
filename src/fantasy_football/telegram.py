"""Telegram polling control plane and safe notification formatting.

The module deliberately contains no ESPN mutation calls.  The transport is
small and injectable so command handling and outbound messages can be tested
without a bot token or network access.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .contracts import LeagueSnapshot
from .decisions import PendingAction
from .health import HealthEvent
from .workflows import CommandWorkflow


class TelegramConfigurationError(ValueError):
    """Raised when Telegram runtime configuration is incomplete."""


@dataclass(frozen=True, slots=True, repr=False)
class TelegramConfig:
    bot_token: str
    allowed_user_id: int
    chat_id: int | None = None
    polling_timeout_seconds: int = 30

    def __post_init__(self) -> None:
        if not self.bot_token or self.allowed_user_id <= 0:
            raise TelegramConfigurationError("Telegram token and user ID are required")
        if self.polling_timeout_seconds < 0 or self.polling_timeout_seconds > 50:
            raise TelegramConfigurationError("polling timeout must be between 0 and 50")

    def __repr__(self) -> str:
        return (
            "TelegramConfig(bot_token=<redacted>, "
            f"allowed_user_id={self.allowed_user_id!r})"
        )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> TelegramConfig:
        values = os.environ if environ is None else environ
        token = values.get("FFM_TELEGRAM_BOT_TOKEN", "")
        user = values.get("FFM_TELEGRAM_ALLOWED_USER_ID", "")
        if not token or not user:
            raise TelegramConfigurationError(
                "missing FFM_TELEGRAM_BOT_TOKEN or FFM_TELEGRAM_ALLOWED_USER_ID"
            )
        try:
            allowed_user_id = int(user)
            chat_id = (
                int(values["FFM_TELEGRAM_CHAT_ID"])
                if values.get("FFM_TELEGRAM_CHAT_ID")
                else None
            )
            timeout = int(values.get("FFM_TELEGRAM_POLL_TIMEOUT", "30"))
        except ValueError as exc:
            raise TelegramConfigurationError(
                "Telegram IDs and timeout must be integers"
            ) from exc
        return cls(token, allowed_user_id, chat_id, timeout)


class TelegramTransport:
    """Bot API transport used by the polling service."""

    def __init__(self, config: TelegramConfig, *, timeout: float = 40) -> None:
        self._config = config
        self._timeout = timeout
        self._endpoint = f"https://api.telegram.org/bot{config.bot_token}/"

    def _call(self, method: str, values: Mapping[str, object]) -> object:
        body = urlencode({key: str(value) for key, value in values.items()}).encode()
        request = Request(self._endpoint + method, data=body, method="POST")
        with urlopen(request, timeout=self._timeout) as response:  # noqa: S310
            payload = json.load(response)
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise RuntimeError(f"Telegram API call failed: {method}")
        return payload.get("result")

    def send_message(self, chat_id: int, text: str) -> None:
        self._call("sendMessage", {"chat_id": chat_id, "text": text})

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, object]]:
        values: dict[str, object] = {
            "timeout": timeout,
            "allowed_updates": '["message"]',
        }
        if offset is not None:
            values["offset"] = offset
        result = self._call("getUpdates", values)
        return result if isinstance(result, list) else []


class Command(StrEnum):
    STATUS = "status"
    RUN = "run"
    APPROVE = "approve"
    VETO = "veto"
    PAUSE = "pause"
    RESUME = "resume"
    WHY = "why"
    DRAFT = "draft"


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    command: Command
    args: str


def parse_command(text: str) -> ParsedCommand | None:
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return None
    name = parts[0].lstrip("/").split("@", 1)[0].lower()
    try:
        command = Command(name)
    except ValueError:
        return None
    return ParsedCommand(command, parts[1] if len(parts) == 2 else "")


class CommandRouter:
    """Allowlist Telegram input and delegate all work to persistent workflows."""

    def __init__(
        self,
        allowed_user_id: int,
        *,
        workflows: CommandWorkflow,
    ) -> None:
        self.allowed_user_id = allowed_user_id
        self._workflows = workflows

    def handle(self, user_id: int, text: str) -> str | None:
        if user_id != self.allowed_user_id:
            return None
        parsed = parse_command(text)
        if parsed is None:
            return (
                "Unknown command. Use status, run, approve, veto, "
                "pause, resume, or why."
            )
        command, args = parsed.command, parsed.args.strip()
        if command is Command.STATUS:
            return self._workflows.status()
        if command is Command.RUN:
            return self._workflows.run()
        if command is Command.WHY:
            return self._workflows.why(args)
        if command is Command.PAUSE:
            return self._workflows.pause()
        if command is Command.RESUME:
            return self._workflows.resume()
        if command is Command.DRAFT:
            return "Draft control is reserved for a future season."
        if command is Command.VETO:
            action_id, reason = _action_and_reason(args)
            return self._workflows.veto(action_id, reason, actor_id=user_id)
        if command is Command.APPROVE:
            return self._workflows.approve(args, actor_id=user_id)
        return "Command safely stubbed."


def _action_and_reason(args: str) -> tuple[str, str]:
    parts = args.split(maxsplit=1)
    return (parts[0], parts[1] if len(parts) == 2 else "") if parts else ("", "")


@dataclass(frozen=True, slots=True)
class TelegramUpdate:
    update_id: int
    user_id: int
    chat_id: int
    text: str


def decode_update(raw: Mapping[str, object]) -> TelegramUpdate | None:
    message = raw.get("message")
    if not isinstance(message, dict):
        return None
    sender = message.get("from")
    chat = message.get("chat")
    text_value = message.get("text")
    if (
        not isinstance(sender, dict)
        or not isinstance(chat, dict)
        or not isinstance(text_value, str)
    ):
        return None
    update_id = raw.get("update_id")
    user_id, chat_id = sender.get("id"), chat.get("id")
    if not all(isinstance(value, int) for value in (update_id, user_id, chat_id)):
        return None
    assert isinstance(update_id, int)
    assert isinstance(user_id, int)
    assert isinstance(chat_id, int)
    return TelegramUpdate(update_id, user_id, chat_id, text_value)


class TelegramTransportProtocol(Protocol):
    def send_message(self, chat_id: int, text: str) -> None: ...

    def get_updates(
        self, offset: int | None, timeout: int
    ) -> list[dict[str, object]]: ...


class TelegramService:
    def __init__(
        self,
        transport: TelegramTransportProtocol,
        router: CommandRouter,
        *,
        poll_timeout: int = 30,
    ) -> None:
        self._transport = transport
        self._router = router
        self._poll_timeout = poll_timeout

    def process_updates(self, updates: Sequence[Mapping[str, object]]) -> None:
        for raw in updates:
            update = decode_update(raw)
            if update is None:
                continue
            reply = self._router.handle(update.user_id, update.text)
            if reply is not None:
                self._transport.send_message(update.chat_id, reply)

    def poll_once(self, offset: int | None = None) -> int | None:
        updates = self._transport.get_updates(offset, self._poll_timeout)
        self.process_updates(updates)
        ids: list[int] = [
            update_id
            for item in updates
            if isinstance((update_id := item.get("update_id")), int)
        ]
        return max(ids) + 1 if ids else offset

    def send_daily_digest(
        self,
        chat_id: int,
        snapshot: LeagueSnapshot,
        *,
        actions: Iterable[PendingAction] = (),
    ) -> None:
        self._transport.send_message(
            chat_id, format_daily_digest(snapshot, actions=actions)
        )

    def send_health_event(self, chat_id: int, event: HealthEvent) -> None:
        self._transport.send_message(chat_id, format_health_alert(event))


def format_daily_digest(
    snapshot: LeagueSnapshot,
    *,
    actions: Iterable[PendingAction] = (),
) -> str:
    lines = [
        f"Daily digest — {snapshot.settings.name} (week {snapshot.status.current_week})"
    ]
    lines.append(f"Teams: {len(snapshot.teams)} | Matchups: {len(snapshot.matchups)}")
    pending = list(actions)
    if pending:
        lines.append("Pending actions:")
        lines.extend(
            f"- {action.summary} [{action.action_id} "
            f"(expires {action.expires_at.astimezone(UTC).isoformat()})]"
            for action in pending
        )
    else:
        lines.append("No pending approvals.")
    return "\n".join(lines)


def format_health_alert(event: HealthEvent) -> str:
    urgency = "URGENT" if event.urgent else "INFO"
    return f"{urgency}: {event.message}"
