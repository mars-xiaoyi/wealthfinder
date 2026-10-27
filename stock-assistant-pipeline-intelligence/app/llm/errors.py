import logging

from app.common import error_codes
from app.common.errors import ErrorType, SAPIError


class LLMError(SAPIError):
    """Base for every failed LLM call."""

    error_type = ErrorType.LLM
    error_code = error_codes.LLM_ERROR


class LLMRateLimitError(LLMError):
    """The provider's rate limit was hit. Retryable after a backoff."""

    error_code = error_codes.LLM_RATE_LIMITED
    log_level = logging.WARNING


class LLMProviderError(LLMError):
    """A provider-side error, a network error, or the call timed out. Retryable."""

    error_code = error_codes.LLM_PROVIDER_ERROR
    log_level = logging.WARNING


class LLMRequestError(LLMError):
    """The provider rejected the request itself, e.g. a malformed request or an unknown model.
    Not self-healing: a code or config fix is needed."""

    error_code = error_codes.LLM_REQUEST_REJECTED


class LLMAuthenticationError(LLMError):
    """The provider rejected the credentials. Not self-healing: an operator has to fix them."""

    error_code = error_codes.LLM_AUTHENTICATION_FAILED
    log_level = logging.CRITICAL


class LLMSchemaViolationError(LLMError):
    """The response did not parse into the output schema, e.g. JSON cut off at the output
    token limit."""

    error_code = error_codes.LLM_SCHEMA_VIOLATION
