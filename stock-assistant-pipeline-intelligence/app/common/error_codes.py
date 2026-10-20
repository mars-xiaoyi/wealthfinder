"""Every error code SAPI returns, in docs/api.md §1.2's {SERVICE_PREFIX}-{CODE} format
(registry: docs/api.md Appendix). Error classes in app/common/errors.py and per-module
errors.py files reference these instead of literals."""

INTERNAL_ERROR = "COMMON-5000"  # Unexpected failure inside SAPI
SERVICE_UNAVAILABLE = (
    "COMMON-5001"  # SAPI-controlled tools or services unreachable, or not initialized yet
)
UPSTREAM_UNAVAILABLE = "COMMON-5002"  # Third-party tools or outside services unavailable
PARSE_ERROR = "COMMON-6000"  # Structured or unstructured content cannot be parsed
