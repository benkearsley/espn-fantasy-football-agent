"""Fake-client coverage for the bounded runtime model analysis boundary."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Never, cast

import pytest

import fantasy_football.service as service_module
from fantasy_football.config import ServiceConfig
from fantasy_football.contracts import (
    LeagueSettings,
    LeagueSnapshot,
    LeagueStatus,
    Player,
    RosterEntry,
    Team,
)
from fantasy_football.model_analysis import (
    ModelAnalysisError,
    ModelConfigurationError,
    ModelProvider,
    ModelRuntimeConfig,
    OpenAIAnalysisPort,
)
from fantasy_football.orchestration import (
    AnalysisLimits,
    AnalysisRequest,
    AnalysisRole,
)
from fantasy_football.service import build_runtime
from fantasy_football.telegram import TelegramConfig


@dataclass(frozen=True, slots=True)
class FakeResponse:
    output_text: str
    status: str = "completed"


class FakeResponses:
    def __init__(self, values: list[FakeResponse | Exception]) -> None:
        self.values = values
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class FakeClient:
    def __init__(self, values: list[FakeResponse | Exception]) -> None:
        self.responses = FakeResponses(values)


def _config(*, api_key: str = "runtime-model-secret") -> ModelRuntimeConfig:
    return ModelRuntimeConfig(
        provider=ModelProvider.OPENAI,
        model="test-model-2026",
        api_key=api_key,
        max_output_tokens=1_000,
        timeout_seconds=10,
    )


def _specialist_output() -> str:
    return json.dumps(
        {
            "recommendation": "Hold until the normalized facts change.",
            "reasoning": "Only normalized ESPN facts were considered.",
            "dissent": "",
            "candidates": [],
        }
    )


def _lead_output() -> str:
    return json.dumps(
        {
            "summary": "Keep the current lineup while facts remain unchanged.",
            "confidence": 0.74,
            "uncertainty": "No external data source is available.",
            "arbitration": "The specialists agree that no action is warranted.",
            "next_actions": [],
        }
    )


def _request(role: AnalysisRole) -> AnalysisRequest:
    return AnalysisRequest(
        role=role,
        instructions=f"System prompt for {role.value}",
        context_json=json.dumps({"role": role.value, "mode": "read_only"}),
        attempt=1,
        max_output_characters=500,
    )


def test_model_runtime_config_is_all_or_nothing_and_redacts_api_key() -> None:
    assert ModelRuntimeConfig.from_env({}) is None

    with pytest.raises(ModelConfigurationError, match="FFM_MODEL_NAME"):
        ModelRuntimeConfig.from_env({"FFM_MODEL_PROVIDER": "openai"})

    config = ModelRuntimeConfig.from_env(
        {
            "FFM_MODEL_PROVIDER": "openai",
            "FFM_MODEL_NAME": "gpt-runtime-mini",
            "FFM_MODEL_API_KEY": "api-key-must-not-appear",
            "FFM_MODEL_MAX_OUTPUT_TOKENS": "321",
            "FFM_MODEL_TIMEOUT_SECONDS": "12.5",
        }
    )

    assert config is not None
    assert config.provider is ModelProvider.OPENAI
    assert config.model == "gpt-runtime-mini"
    assert config.max_output_tokens == 321
    assert config.timeout_seconds == 12.5
    assert "api-key-must-not-appear" not in repr(config)


@pytest.mark.parametrize("role", list(AnalysisRole))
def test_openai_port_forwards_each_role_as_a_bounded_structured_request(
    role: AnalysisRole,
) -> None:
    output = _lead_output() if role is AnalysisRole.LEAD else _specialist_output()
    client = FakeClient([FakeResponse(output)])
    port = OpenAIAnalysisPort(
        _config(), client=client, limits=AnalysisLimits(max_output_characters=500)
    )

    assert port.analyze(_request(role)) == json.dumps(
        json.loads(output), separators=(",", ":"), sort_keys=True
    )

    assert len(client.responses.calls) == 1
    call = client.responses.calls[0]
    assert call["model"] == "test-model-2026"
    assert call["max_output_tokens"] == 500
    assert call["store"] is False
    input_items = cast(list[dict[str, str]], call["input"])
    assert input_items == [
        {"role": "system", "content": f"System prompt for {role.value}"},
        {
            "role": "user",
            "content": json.dumps({"role": role.value, "mode": "read_only"}),
        },
    ]
    text = cast(dict[str, object], call["text"])
    response_format = cast(dict[str, object], text["format"])
    assert response_format["type"] == "json_schema"
    assert response_format["strict"] is True
    assert response_format["name"] == f"fantasy_football_{role.value}_analysis"
    schema = cast(dict[str, object], response_format["schema"])
    assert schema["additionalProperties"] is False


def test_openai_port_fails_closed_and_redacts_provider_error_and_raw_response() -> None:
    secret = "runtime-model-secret"
    provider_detail = f"provider failure with {secret} and raw-response-content"
    failing_client = FakeClient([RuntimeError(provider_detail)])
    port = OpenAIAnalysisPort(
        _config(api_key=secret),
        client=failing_client,
        limits=AnalysisLimits(),
    )

    with pytest.raises(ModelAnalysisError) as error:
        port.analyze(_request(AnalysisRole.LEAGUE_DATA))

    message = str(error.value)
    assert message == "model analysis request failed"
    assert secret not in message
    assert "raw-response-content" not in message

    invalid_client = FakeClient([FakeResponse(f'["{provider_detail}"]')])
    invalid_port = OpenAIAnalysisPort(
        _config(api_key=secret),
        client=invalid_client,
        limits=AnalysisLimits(),
    )
    with pytest.raises(ModelAnalysisError) as invalid_error:
        invalid_port.analyze(_request(AnalysisRole.TRADE))

    assert str(invalid_error.value) == "model analysis response was not a JSON object"
    assert secret not in str(invalid_error.value)
    assert "raw-response-content" not in str(invalid_error.value)

    oversized_client = FakeClient([FakeResponse(json.dumps({"raw": provider_detail}))])
    oversized_port = OpenAIAnalysisPort(
        _config(api_key=secret),
        client=oversized_client,
        limits=AnalysisLimits(),
    )
    short_request = AnalysisRequest(
        role=AnalysisRole.RISK,
        instructions="short prompt",
        context_json='{"mode":"read_only"}',
        attempt=1,
        max_output_characters=20,
    )
    with pytest.raises(ModelAnalysisError) as oversized_error:
        oversized_port.analyze(short_request)

    assert str(oversized_error.value) == "model analysis response exceeded its limit"
    assert secret not in str(oversized_error.value)
    assert "raw-response-content" not in str(oversized_error.value)


def test_openai_port_rejects_over_budget_context_without_calling_provider() -> None:
    client = FakeClient([FakeResponse(_specialist_output())])
    port = OpenAIAnalysisPort(
        _config(),
        client=client,
        limits=AnalysisLimits(max_context_characters=8),
    )
    request = AnalysisRequest(
        role=AnalysisRole.RISK,
        instructions="prompt",
        context_json='{"too":"long"}',
        attempt=1,
        max_output_characters=500,
    )

    with pytest.raises(ModelAnalysisError, match="context exceeded"):
        port.analyze(request)

    assert client.responses.calls == []


class ReadOnlyFakeReader:
    def __init__(self, snapshot: LeagueSnapshot) -> None:
        self.snapshot = snapshot
        self.reads = 0

    def read_snapshot(self) -> LeagueSnapshot:
        self.reads += 1
        return self.snapshot


class RoleAwareResponses:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        input_items = cast(list[dict[str, str]], kwargs["input"])
        context = json.loads(input_items[1]["content"])
        role = AnalysisRole(context["role"])
        output = _lead_output() if role is AnalysisRole.LEAD else _specialist_output()
        return FakeResponse(output)


class RoleAwareClient:
    def __init__(self) -> None:
        self.responses = RoleAwareResponses()


def _snapshot() -> LeagueSnapshot:
    return LeagueSnapshot(
        settings=LeagueSettings(7, "Engine League", 2026),
        teams=(
            Team(
                1,
                "Ben's Team",
                roster=(RosterEntry(Player(11, "Starter", "RB"), "RB"),),
            ),
            Team(2, "Opponent"),
        ),
        matchups=(),
        status=LeagueStatus(1, "in_season"),
        draft_picks=(),
        transactions=(),
        free_agents=(),
        source_timestamp=datetime.now(UTC),
    )


def test_build_runtime_selects_model_or_safe_unavailable_and_run_persists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reader = ReadOnlyFakeReader(_snapshot())
    monkeypatch.setattr(service_module, "EspnApiReader", lambda *_: reader)
    config = ServiceConfig(7, 2026, tmp_path)
    telegram = TelegramConfig("telegram-runtime-secret", 42)
    model_client = RoleAwareClient()

    configured = build_runtime(
        config,
        telegram,
        session_path=tmp_path / "session.json",
        model_config=_config(),
        model_client_factory=lambda _: model_client,
    )

    reply = configured._router.handle(42, "run")

    assert reply is not None and "recommendation_ready" in reply
    assert reader.reads == 1
    assert len(model_client.responses.calls) == len(AnalysisRole)
    system_prompts = [
        cast(list[dict[str, str]], call["input"])[0]["content"]
        for call in model_client.responses.calls
    ]
    assert system_prompts[0].startswith("You are the League & Data Analyst.")
    assert system_prompts[-1].startswith("You are the Lead Manager.")
    assert all("Return exactly one JSON object" in prompt for prompt in system_prompts)
    configured_port = next(iter(configured._lead_manager._analysis_ports.values()))
    assert isinstance(configured_port, OpenAIAnalysisPort)
    history = configured._history
    assert len(history.records) == 1
    record = history.records[0]
    assert record.cycle.recommendation.summary.startswith("Keep the current lineup")
    assert len(record.cycle.specialist_opinions) == 4
    history_path = tmp_path / "2026" / "decision-history.jsonl"
    assert history_path.exists()
    assert "runtime-model-secret" not in history_path.read_text(encoding="utf-8")
    assert "runtime-model-secret" not in repr(configured.readiness())
    assert not hasattr(configured, "_executor")

    factory_calls: list[bool] = []

    def unexpected_factory(_: ModelRuntimeConfig) -> Never:
        factory_calls.append(True)
        raise AssertionError("unconfigured model must not create a provider client")

    unavailable = build_runtime(
        ServiceConfig(8, 2026, tmp_path / "unconfigured"),
        telegram,
        session_path=tmp_path / "other-session.json",
        model_config=None,
        model_client_factory=unexpected_factory,
    )
    unavailable_port = next(iter(unavailable._lead_manager._analysis_ports.values()))
    assert unavailable_port.analyze(_request(AnalysisRole.LEAD)) == "{}"
    assert factory_calls == []
