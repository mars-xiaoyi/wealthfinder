from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from app.common import error_codes
from app.common.error_categories import ErrorCategory
from app.common.errors import ServiceUnavailableError
from app.config import LLMConfig
from app.llm import vertex_adapter as vertex_adapter_module
from app.llm.adapter import GenerationConfig, Message, Role, StructuredLLMResponse
from app.llm.errors import (
    LLMAuthenticationError,
    LLMProviderError,
    LLMRateLimitError,
    LLMRequestError,
    LLMSchemaViolationError,
)
from app.llm.vertex_adapter import VertexAIAdapter

CONFIG = LLMConfig(vertex_project="wealthfinder", vertex_location="global", api_timeout_s=30)
GENERATION_CONFIG = GenerationConfig(
    model="gemini-3.1-flash-lite", temperature=0, max_output_tokens=16384
)
MESSAGES = [Message(Role.SYSTEM, "system prompt"), Message(Role.USER, "article")]


class _Output(BaseModel):
    names: list[str]


def _response(
    text: str | None, usage: types.GenerateContentResponseUsageMetadata | None
) -> MagicMock:
    response = MagicMock(spec=types.GenerateContentResponse)
    response.text = text
    response.usage_metadata = usage
    return response


def _api_error(code: int) -> errors.APIError:
    """The SDK's own error for an HTTP status: ClientError for 4xx, ServerError for 5xx."""
    error_cls = errors.ClientError if code < 500 else errors.ServerError
    return error_cls(code, {"error": {"code": code, "message": "failed"}})


@pytest.fixture(autouse=True)
def reset_singleton(monkeypatch):
    monkeypatch.setattr(vertex_adapter_module, "_llm_adapter", None)


@pytest.fixture
def genai_client():
    """The google-genai Client the adapter constructs."""
    with mock.patch("app.llm.vertex_adapter.genai.Client") as client_cls:
        client = client_cls.return_value
        client.aio.models.generate_content = AsyncMock()
        client.aio.aclose = AsyncMock()
        yield client_cls


def vertex_ai_adapter_success(genai_client):
    VertexAIAdapter(CONFIG)

    genai_client.assert_called_once_with(
        vertexai=True,
        project=CONFIG.vertex_project,
        location=CONFIG.vertex_location,
        http_options=types.HttpOptions(timeout=30000),
    )


@pytest.mark.parametrize(
    ("thoughts_token_count", "output_tokens"),
    [
        pytest.param(None, 300, id="no_thinking"),
        pytest.param(50, 350, id="thinking"),
    ],
)
async def generate_structured_success(genai_client, thoughts_token_count, output_tokens):
    """The response is parsed into the schema; thinking tokens count as output."""
    generate_content = genai_client.return_value.aio.models.generate_content
    generate_content.return_value = _response(
        '{"names": ["騰訊控股"]}',
        types.GenerateContentResponseUsageMetadata(
            prompt_token_count=1200,
            candidates_token_count=300,
            thoughts_token_count=thoughts_token_count,
        ),
    )

    response = await VertexAIAdapter(CONFIG).generate_structured(
        MESSAGES, _Output, GENERATION_CONFIG
    )

    assert response == StructuredLLMResponse(
        output=_Output(names=["騰訊控股"]),
        input_tokens=1200,
        output_tokens=output_tokens,
        latency_ms=response.latency_ms,
    )
    assert response.latency_ms >= 0
    generate_content.assert_awaited_once_with(
        model="gemini-3.1-flash-lite",
        contents=[types.Content(role="user", parts=[types.Part(text="article")])],
        config=types.GenerateContentConfig(
            system_instruction=["system prompt"],
            temperature=0,
            max_output_tokens=16384,
            response_mime_type="application/json",
            response_json_schema=_Output.model_json_schema(),
        ),
    )


async def generate_structured_assistant_turn(genai_client):
    """An assistant message is sent as the model's turn."""
    generate_content = genai_client.return_value.aio.models.generate_content
    generate_content.return_value = _response('{"names": []}', None)
    messages = [*MESSAGES, Message(Role.ASSISTANT, "answer"), Message(Role.USER, "again")]

    await VertexAIAdapter(CONFIG).generate_structured(messages, _Output, GENERATION_CONFIG)

    assert generate_content.await_args.kwargs["contents"] == [
        types.Content(role="user", parts=[types.Part(text="article")]),
        types.Content(role="model", parts=[types.Part(text="answer")]),
        types.Content(role="user", parts=[types.Part(text="again")]),
    ]


async def generate_structured_no_usage_metadata(genai_client):
    genai_client.return_value.aio.models.generate_content.return_value = _response(
        '{"names": []}', None
    )

    response = await VertexAIAdapter(CONFIG).generate_structured(
        MESSAGES, _Output, GENERATION_CONFIG
    )

    assert response == StructuredLLMResponse(
        output=_Output(names=[]), input_tokens=0, output_tokens=0, latency_ms=response.latency_ms
    )
    assert response.latency_ms >= 0


@pytest.mark.parametrize(
    ("error", "expected_type", "expected_code"),
    [
        pytest.param(
            _api_error(429), LLMRateLimitError, error_codes.LLM_RATE_LIMITED, id="rate_limited"
        ),
        pytest.param(
            _api_error(401),
            LLMAuthenticationError,
            error_codes.LLM_AUTHENTICATION_FAILED,
            id="unauthenticated",
        ),
        pytest.param(
            _api_error(403),
            LLMAuthenticationError,
            error_codes.LLM_AUTHENTICATION_FAILED,
            id="forbidden",
        ),
        pytest.param(
            _api_error(500), LLMProviderError, error_codes.LLM_PROVIDER_ERROR, id="server_error"
        ),
        pytest.param(
            _api_error(504), LLMProviderError, error_codes.LLM_PROVIDER_ERROR, id="timeout"
        ),
        pytest.param(
            _api_error(400), LLMRequestError, error_codes.LLM_REQUEST_REJECTED, id="bad_request"
        ),
        pytest.param(
            _api_error(404), LLMRequestError, error_codes.LLM_REQUEST_REJECTED, id="not_found"
        ),
        pytest.param(
            httpx.ConnectError("unreachable"),
            LLMProviderError,
            error_codes.LLM_PROVIDER_ERROR,
            id="network_error",
        ),
    ],
)
async def generate_structured_call_failed(genai_client, error, expected_type, expected_code):
    genai_client.return_value.aio.models.generate_content.side_effect = error

    with pytest.raises(expected_type) as exc_info:
        await VertexAIAdapter(CONFIG).generate_structured(MESSAGES, _Output, GENERATION_CONFIG)

    assert type(exc_info.value) is expected_type
    assert exc_info.value.error_code == expected_code
    assert exc_info.value.category is ErrorCategory.LLM
    assert exc_info.value.__cause__ is error


@pytest.mark.parametrize(
    "text",
    [
        pytest.param('{"names": ["騰訊', id="truncated"),
        pytest.param('{"other": 1}', id="wrong_shape"),
        pytest.param(None, id="empty"),
    ],
)
async def generate_structured_schema_violation(genai_client, text):
    genai_client.return_value.aio.models.generate_content.return_value = _response(text, None)

    with pytest.raises(LLMSchemaViolationError) as exc_info:
        await VertexAIAdapter(CONFIG).generate_structured(MESSAGES, _Output, GENERATION_CONFIG)

    assert type(exc_info.value) is LLMSchemaViolationError
    assert exc_info.value.error_code == error_codes.LLM_SCHEMA_VIOLATION
    assert exc_info.value.category is ErrorCategory.LLM
    assert isinstance(exc_info.value.__cause__, ValidationError)


async def close_success(genai_client):
    await VertexAIAdapter(CONFIG).close()

    genai_client.return_value.aio.aclose.assert_awaited_once_with()


def get_llm_adapter_not_initialized():
    with pytest.raises(ServiceUnavailableError):
        vertex_adapter_module.get_llm_adapter()


def create_llm_adapter_success(genai_client):
    adapter = vertex_adapter_module.create_llm_adapter(CONFIG)

    assert isinstance(adapter, VertexAIAdapter)
    assert vertex_adapter_module.get_llm_adapter() is adapter
