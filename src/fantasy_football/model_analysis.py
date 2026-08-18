"""Runtime-configured, bounded model analysis adapters.

This module owns the provider-specific boundary for model analysis.  It never
receives an ESPN adapter or credentials, stores no provider request/response,
and exposes only the application-owned :class:`AnalysisPort` protocol to the
rest of the service.

The OpenAI Responses request uses Structured Outputs with a role-specific JSON
schema.  Application parsing remains authoritative, so malformed, incomplete,
or oversized provider responses fail closed before orchestration can persist a
recommendation.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from importlib import import_module
from typing import Protocol, cast

from .config import ConfigurationError
from .orchestration import AnalysisLimits, AnalysisPort, AnalysisRequest, AnalysisRole

_MODEL_NAME_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_MAX_OUTPUT_TOKENS = 4_096
_DEFAULT_MAX_OUTPUT_TOKENS = 1_000
_DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_TIMEOUT_SECONDS = 60.0


class ModelProvider(StrEnum):
    """Supported runtime model providers.

    The service deliberately recognizes a provider only when a concrete
    adapter exists.  Adding a provider therefore cannot silently route data to
    an unreviewed endpoint.
    """

    OPENAI = "openai"


class ModelConfigurationError(ConfigurationError):
    """Raised when model runtime configuration is incomplete or unsafe."""


class ModelAnalysisError(RuntimeError):
    """A deliberately redacted failure at the provider analysis boundary."""


@dataclass(frozen=True, slots=True)
class ModelRuntimeConfig:
    """Runtime-only model settings; no service serializer or repr exposes the key."""

    provider: ModelProvider
    model: str
    api_key: str = field(repr=False)
    max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not _MODEL_NAME_PATTERN.fullmatch(self.model):
            raise ModelConfigurationError("model name is invalid")
        if not self.api_key.strip():
            raise ModelConfigurationError("model API key must not be empty")
        if not 1 <= self.max_output_tokens <= _MAX_OUTPUT_TOKENS:
            raise ModelConfigurationError(
                f"model max output tokens must be between 1 and {_MAX_OUTPUT_TOKENS}"
            )
        if not 0 < self.timeout_seconds <= _MAX_TIMEOUT_SECONDS:
            raise ModelConfigurationError(
                "model timeout seconds must be greater than zero and at most "
                f"{_MAX_TIMEOUT_SECONDS:g}"
            )

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None
    ) -> ModelRuntimeConfig | None:
        """Load an all-or-nothing model configuration from ``FFM_*`` variables.

        With none of the model variables set, analysis stays explicitly
        unavailable and no provider client is constructed.  A partial model
        configuration is an operator error rather than a reason to fall back
        silently to another provider or model.
        """

        values = os.environ if environ is None else environ
        variable_names = (
            "FFM_MODEL_PROVIDER",
            "FFM_MODEL_NAME",
            "FFM_MODEL_API_KEY",
            "FFM_MODEL_MAX_OUTPUT_TOKENS",
            "FFM_MODEL_TIMEOUT_SECONDS",
        )
        present = {name: values.get(name, "") for name in variable_names}
        if not any(value.strip() for value in present.values()):
            return None

        required_names = (
            "FFM_MODEL_PROVIDER",
            "FFM_MODEL_NAME",
            "FFM_MODEL_API_KEY",
        )
        missing = [name for name in required_names if not present[name].strip()]
        if missing:
            raise ModelConfigurationError(
                "missing model configuration: " + ", ".join(missing)
            )

        provider_value = present["FFM_MODEL_PROVIDER"].strip().lower()
        try:
            provider = ModelProvider(provider_value)
        except ValueError as exc:
            raise ModelConfigurationError("model provider is unsupported") from exc

        try:
            max_output_tokens = int(
                present["FFM_MODEL_MAX_OUTPUT_TOKENS"]
                or str(_DEFAULT_MAX_OUTPUT_TOKENS)
            )
            timeout_seconds = float(
                present["FFM_MODEL_TIMEOUT_SECONDS"] or str(_DEFAULT_TIMEOUT_SECONDS)
            )
        except ValueError as exc:
            raise ModelConfigurationError(
                "model output-token and timeout settings must be numeric"
            ) from exc

        return cls(
            provider=provider,
            model=present["FFM_MODEL_NAME"].strip(),
            api_key=present["FFM_MODEL_API_KEY"],
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
        )


class _ResponsesEndpoint(Protocol):
    def create(self, **kwargs: object) -> object: ...


class OpenAIResponsesClient(Protocol):
    """The small subset of the OpenAI client needed by this adapter."""

    @property
    def responses(self) -> _ResponsesEndpoint: ...


ModelClientFactory = Callable[[ModelRuntimeConfig], OpenAIResponsesClient]


class OpenAIAnalysisPort(AnalysisPort):
    """Adapt bounded application analysis requests to OpenAI Responses calls."""

    def __init__(
        self,
        config: ModelRuntimeConfig,
        *,
        client: OpenAIResponsesClient,
        limits: AnalysisLimits,
    ) -> None:
        if config.provider is not ModelProvider.OPENAI:
            raise ModelConfigurationError("OpenAI adapter requires the openai provider")
        self._config = config
        self._client = client
        self._limits = limits

    def analyze(self, request: AnalysisRequest) -> str:
        """Return one bounded JSON object or raise a redacted, fail-closed error."""

        self._validate_request(request)
        try:
            response = self._client.responses.create(
                model=self._config.model,
                input=[
                    {"role": "system", "content": request.instructions},
                    {"role": "user", "content": request.context_json},
                ],
                max_output_tokens=min(
                    self._config.max_output_tokens,
                    request.max_output_characters,
                ),
                store=False,
                text={"format": _response_format(request.role)},
            )
            status = getattr(response, "status", None)
            output_text = getattr(response, "output_text", None)
        except Exception:
            # Provider exceptions may include request data or credentials.  Do
            # not retain or surface any provider message at this boundary.
            raise ModelAnalysisError("model analysis request failed") from None

        if status is not None and status != "completed":
            raise ModelAnalysisError("model analysis response was not completed")
        if not isinstance(output_text, str) or not output_text.strip():
            raise ModelAnalysisError("model analysis response was unavailable")
        if len(output_text) > request.max_output_characters:
            raise ModelAnalysisError("model analysis response exceeded its limit")

        try:
            parsed = json.loads(output_text)
        except json.JSONDecodeError:
            raise ModelAnalysisError(
                "model analysis response was not valid JSON"
            ) from None
        if not isinstance(parsed, dict):
            raise ModelAnalysisError("model analysis response was not a JSON object")
        # Normalize before returning so raw provider response formatting is not
        # carried into the durable decision record.
        return json.dumps(parsed, separators=(",", ":"), sort_keys=True)

    def _validate_request(self, request: AnalysisRequest) -> None:
        if len(request.context_json) > self._limits.max_context_characters:
            raise ModelAnalysisError("model analysis context exceeded its limit")
        if (
            len(request.instructions) + len(request.context_json)
            > self._limits.max_total_input_characters
        ):
            raise ModelAnalysisError("model analysis input exceeded its limit")
        if request.max_output_characters > self._limits.max_output_characters:
            raise ModelAnalysisError("model analysis output limit was unsafe")


def build_analysis_port(
    config: ModelRuntimeConfig | None,
    *,
    limits: AnalysisLimits,
    client_factory: ModelClientFactory | None = None,
) -> AnalysisPort | None:
    """Build the configured provider adapter, or ``None`` when deliberately off."""

    if config is None:
        return None
    if config.provider is ModelProvider.OPENAI:
        factory = client_factory or _new_openai_client
        return OpenAIAnalysisPort(config, client=factory(config), limits=limits)
    raise ModelConfigurationError("model provider is unsupported")


def _new_openai_client(config: ModelRuntimeConfig) -> OpenAIResponsesClient:
    """Construct the official SDK client without making a network request."""

    try:
        module = import_module("openai")
        factory = vars(module).get("OpenAI")
        if not callable(factory):
            raise TypeError("OpenAI client factory is unavailable")
        client = factory(
            api_key=config.api_key,
            timeout=config.timeout_seconds,
            max_retries=0,
        )
    except (AttributeError, ImportError, TypeError):
        raise ModelConfigurationError("OpenAI model support is unavailable") from None
    return cast(OpenAIResponsesClient, client)


def _response_format(role: AnalysisRole) -> dict[str, object]:
    return {
        "type": "json_schema",
        "name": f"fantasy_football_{role.value}_analysis",
        "strict": True,
        "schema": _schema_for(role),
    }


def _schema_for(role: AnalysisRole) -> dict[str, object]:
    action_schema = _action_schema()
    if role is AnalysisRole.LEAD:
        return {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "confidence": {"type": "number"},
                "uncertainty": {"type": "string"},
                "arbitration": {"type": "string"},
                "next_actions": {
                    "type": "array",
                    "items": action_schema,
                    "maxItems": 3,
                },
            },
            "required": [
                "summary",
                "confidence",
                "uncertainty",
                "arbitration",
                "next_actions",
            ],
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": {
            "recommendation": {"type": "string"},
            "reasoning": {"type": "string"},
            "dissent": {"type": "string"},
            "candidates": {
                "type": "array",
                "items": action_schema,
                "maxItems": 3,
            },
        },
        "required": ["recommendation", "reasoning", "dissent", "candidates"],
        "additionalProperties": False,
    }


def _action_schema() -> dict[str, object]:
    """Represent application-optional action fields as required nullable keys.

    Strict Structured Outputs requires a closed object shape.  The application
    parser already accepts these fields as optional, so a null value has the
    same safe meaning as an omitted one while preserving one schema for every
    provider request.
    """

    return {
        "type": "object",
        "properties": {
            "kind": {"type": "string"},
            "summary": {"type": "string"},
            "rationale": {"type": "string"},
            "affected_player_ids": {
                "type": ["array", "null"],
                "items": {"type": "integer"},
            },
            "approval_expires_at": {"type": ["string", "null"]},
        },
        "required": [
            "kind",
            "summary",
            "rationale",
            "affected_player_ids",
            "approval_expires_at",
        ],
        "additionalProperties": False,
    }
