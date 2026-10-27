import time
from http import HTTPStatus

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from app.common.error_categories import ErrorCategory
from app.common.errors import ServiceUnavailableError
from app.config import LLMConfig
from app.llm.adapter import GenerationConfig, LLMAdapter, Message, Role, StructuredLLMResponse
from app.llm.errors import (
    LLMAuthenticationError,
    LLMError,
    LLMProviderError,
    LLMRateLimitError,
    LLMRequestError,
    LLMSchemaViolationError,
)

_PROVIDER_ROLES = {Role.USER: "user", Role.ASSISTANT: "model"}
_AUTHENTICATION_CODES = {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}


class VertexAIAdapter(LLMAdapter):
    """LLMAdapter on Vertex AI's Gemini models."""

    def __init__(self, config: LLMConfig) -> None:
        # No retry_options: one attempt per call, retries happen at the message level.
        self._client = genai.Client(
            vertexai=True,
            project=config.vertex_project,
            location=config.vertex_location,
            http_options=types.HttpOptions(timeout=config.api_timeout_s * 1000),
        )

    async def generate_structured[T: BaseModel](
        self,
        messages: list[Message],
        output_schema: type[T],
        generation_config: GenerationConfig,
    ) -> StructuredLLMResponse[T]:
        """
        Make one Gemini call whose response is constrained to output_schema.
        Raises LLMRateLimitError, LLMAuthenticationError, LLMRequestError or LLMProviderError if
        the call fails, or LLMSchemaViolationError if the response does not parse into
        output_schema.
        """
        config = types.GenerateContentConfig(
            system_instruction=[m.content for m in messages if m.role is Role.SYSTEM],
            temperature=generation_config.temperature,
            max_output_tokens=generation_config.max_output_tokens,
            response_mime_type="application/json",
            response_json_schema=output_schema.model_json_schema(),
        )
        contents = [
            types.Content(role=_PROVIDER_ROLES[m.role], parts=[types.Part(text=m.content)])
            for m in messages
            if m.role is not Role.SYSTEM
        ]
        started = time.monotonic()
        try:
            response = await self._client.aio.models.generate_content(
                model=generation_config.model, contents=contents, config=config
            )
        except Exception as exc:
            raise _call_error(exc) from exc
        latency_ms = int((time.monotonic() - started) * 1000)

        try:
            output = output_schema.model_validate_json(response.text or "")
        except ValidationError as exc:
            raise LLMSchemaViolationError(
                f"Vertex AI response does not match {output_schema.__name__}: {exc!r}",
                category=ErrorCategory.LLM,
            ) from exc

        usage = response.usage_metadata or types.GenerateContentResponseUsageMetadata()
        return StructuredLLMResponse(
            output=output,
            input_tokens=usage.prompt_token_count or 0,
            output_tokens=(usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0),
            latency_ms=latency_ms,
        )

    async def close(self) -> None:
        """Release the Vertex AI client's HTTP session."""
        await self._client.aio.aclose()


def _call_error(exc: Exception) -> LLMError:
    """Classify a failed Vertex AI call."""
    dev_message = f"Vertex AI call failed: {exc!r}"
    if isinstance(exc, errors.APIError) and exc.code == HTTPStatus.TOO_MANY_REQUESTS:
        return LLMRateLimitError(dev_message, category=ErrorCategory.LLM)
    if isinstance(exc, errors.APIError) and exc.code in _AUTHENTICATION_CODES:
        return LLMAuthenticationError(dev_message, category=ErrorCategory.LLM)
    if isinstance(exc, errors.ClientError):
        return LLMRequestError(dev_message, category=ErrorCategory.LLM)
    # Any other API error (the SDK reports its own timeout as 504), or a network error.
    return LLMProviderError(dev_message, category=ErrorCategory.LLM)


_llm_adapter: LLMAdapter | None = None


def get_llm_adapter() -> LLMAdapter:
    """
    Return the process's one LLMAdapter instance.
    Raises ServiceUnavailableError if called before create_llm_adapter() has run.
    """
    if _llm_adapter is None:
        raise ServiceUnavailableError("LLM adapter is not initialized", category=ErrorCategory.LLM)
    return _llm_adapter


def create_llm_adapter(config: LLMConfig) -> LLMAdapter:
    """Create the Vertex AI adapter and register it as this module's singleton."""
    global _llm_adapter
    _llm_adapter = VertexAIAdapter(config)
    return _llm_adapter
