"""Every error code SAPI returns, in docs/api.md §1.2's {SERVICE_PREFIX}-{CODE} format
(registry: docs/api.md Appendix). Error classes in app/common/errors.py and per-module
errors.py files reference these instead of literals."""

INTERNAL_ERROR = "COMMON-5000"  # Unexpected failure inside SAPI
SERVICE_UNAVAILABLE = (
    "COMMON-5001"  # SAPI-controlled tools or services unreachable, or not initialized yet
)
UPSTREAM_UNAVAILABLE = "COMMON-5002"  # Third-party tools or outside services unavailable
PARSE_ERROR = "COMMON-6000"  # Structured or unstructured content cannot be parsed
LLM_ERROR = "COMMON-7000"  # LLM call failed
LLM_RATE_LIMITED = "COMMON-7001"  # LLM provider rate limit hit
LLM_PROVIDER_ERROR = "COMMON-7002"  # LLM provider error, network error or timeout
LLM_AUTHENTICATION_FAILED = "COMMON-7003"  # LLM provider rejected the credentials
LLM_SCHEMA_VIOLATION = "COMMON-7004"  # LLM response did not match the output schema
LLM_REQUEST_REJECTED = "COMMON-7005"  # LLM provider rejected the request itself
