import httpx
import pytest

from app.common import http_client as http_client_module
from app.common.errors import ServiceUnavailableError


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
