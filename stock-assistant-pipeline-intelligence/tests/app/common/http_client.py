import httpx
import pytest
from pydantic import TypeAdapter, ValidationError

from app.common import error_codes
from app.common import http_client as http_client_module
from app.common.error_categories import ErrorCategory
from app.common.errors import ParseError, ServiceUnavailableError


@pytest.fixture(autouse=True)
def reset_singleton(monkeypatch):
    """Runs before every test here (autouse): starts each one with no client registered, so
    get_http_client_not_initialized can't see the one create_http_client registers."""
    monkeypatch.setattr(http_client_module, "_http_client", None)


def get_http_client_not_initialized():
    with pytest.raises(ServiceUnavailableError):
        http_client_module.get_http_client()


async def create_http_client():
    client = http_client_module.create_http_client(5)

    assert client.timeout == httpx.Timeout(5)
    assert http_client_module.get_http_client() is client
    await client.aclose()


@http_client_module.translate_client_errors(upstream="SADI", category=ErrorCategory.NEWS_CLIENT)
async def _call_upstream(outcome: object) -> object:
    """A decorated client method: raises outcome if it is an exception, else returns it."""
    if isinstance(outcome, Exception):
        raise outcome
    return outcome


async def translate_client_errors_success():
    assert await _call_upstream("result") == "result"


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(httpx.ConnectError("refused"), id="unreachable"),
        pytest.param(httpx.ReadTimeout("slow"), id="timeout"),
        pytest.param(
            httpx.HTTPStatusError(
                "503",
                request=httpx.Request("GET", "http://sadi"),
                response=httpx.Response(503),
            ),
            id="error_status",
        ),
    ],
)
async def translate_client_errors_http_error(error):
    with pytest.raises(ServiceUnavailableError) as exc_info:
        await _call_upstream(error)

    assert exc_info.value.error_code == error_codes.UPSTREAM_UNAVAILABLE
    assert exc_info.value.category is ErrorCategory.NEWS_CLIENT
    assert exc_info.value.__cause__ is error


async def translate_client_errors_validation_error():
    with pytest.raises(ValidationError) as validation:
        TypeAdapter(int).validate_python("not an int")

    with pytest.raises(ParseError) as exc_info:
        await _call_upstream(validation.value)

    assert exc_info.value.error_code == error_codes.PARSE_ERROR
    assert exc_info.value.category is ErrorCategory.NEWS_CLIENT
    assert exc_info.value.__cause__ is validation.value
