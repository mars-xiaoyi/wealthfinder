"""
Real Postgres and Redis containers for the `live` integration suite, driven through the
docker CLI. Host ports are fixed per session (not Docker-assigned) so `docker start` after
`docker stop` brings a container back on the same port the app is configured with.
"""

import contextlib
import dataclasses
import functools
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app import main
from app.common import hk_stock_list

POSTGRES_IMAGE = "postgres:16-alpine"
REDIS_IMAGE = "redis:7-alpine"
POSTGRES_PASSWORD = "password"
READY_TIMEOUT_S = 30.0
# Every env var with a default in app/config.py; cleared so a developer's shell can't change
# them. Keep in sync with app/config.py.
OPTIONAL_ENV = (
    "DB_POOL_SIZE",
    "DB_CLOSE_TIMEOUT_S",
    "DB_WRITE_MAX_RETRY",
    "DB_RETRY_BASE_WAIT_MS",
    "HTTP_TIMEOUT_S",
    "HEALTH_CHECK_TIMEOUT_S",
    "REDIS_RECONNECT_INTERVAL_S",
    "STREAM_CLAIM_TIMEOUT_MS",
    "ENTITY_ANALYSIS_BATCH_SIZE",
    "ENTITY_ANALYSIS_BATCH_TIMEOUT_MS",
    "ENTITY_ANALYSIS_MAX_RETRY",
    "SADI_API_URL",
    "HOSTNAME",
    "VERTEX_AI_LOCATION",
    "LLM_API_TIMEOUT_S",
    "ENTITY_ANALYSIS_MAX_INPUT_CHARS",
)
# A project id for cases that never call Vertex AI; nothing is sent to it.
PLACEHOLDER_VERTEX_PROJECT = "sapi-integration-test"
HKEX_URL_CONSTANTS = ("_ACTIVESTOCK_EN_URL", "_ACTIVESTOCK_ZH_URL", "_LIST_OF_SECURITIES_URL")
# Nothing listens on port 1, so a connection is refused at once.
REFUSED_URL = "http://127.0.0.1:1"
SERVICE_ROOT = Path(__file__).resolve().parents[2]


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _redis_answers_ping(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1) as sock:
            sock.sendall(b"PING\r\n")
            return sock.recv(16).startswith(b"+PONG")
    except OSError:
        return False


@dataclasses.dataclass
class Container:
    name: str
    kind: str  # "postgres" | "redis"
    port: int

    def stop(self) -> None:
        _docker("stop", "-t", "5", self.name)

    def pause(self) -> None:
        _docker("pause", self.name)

    def status(self) -> str:
        return _docker("inspect", "-f", "{{.State.Status}}", self.name).stdout.strip()

    def is_ready(self) -> bool:
        if self.kind == "redis":
            return _redis_answers_ping(self.port)
        # -h 127.0.0.1: the image's first-boot init server listens on a socket only, so a TCP
        # check doesn't report ready until the real server is up.
        result = subprocess.run(
            ["docker", "exec", self.name, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
            capture_output=True,
        )
        return result.returncode == 0

    def wait_ready(self) -> None:
        deadline = time.monotonic() + READY_TIMEOUT_S
        while not self.is_ready():
            if time.monotonic() > deadline:
                raise TimeoutError(f"{self.name} not ready after {READY_TIMEOUT_S}s")
            time.sleep(0.2)

    def restore(self) -> None:
        """Bring the container back to running and ready, whatever state a test left it in."""
        status = self.status()
        if status == "paused":
            _docker("unpause", self.name)
        elif status != "running":
            _docker("start", self.name)
        self.wait_ready()


@dataclasses.dataclass
class Infra:
    postgres: Container
    redis_stream: Container
    redis_state: Container

    def for_component(self, component: str) -> Container:
        """Map a GET /v1/health field name to the container behind it."""
        containers = {
            "database": self.postgres,
            "redis_stream": self.redis_stream,
            "redis_state": self.redis_state,
        }
        return containers[component]

    def all(self) -> tuple[Container, Container, Container]:
        return (self.postgres, self.redis_stream, self.redis_state)

    def env(self) -> dict[str, str]:
        return {
            "DATABASE_URL": (
                f"postgresql://postgres:{POSTGRES_PASSWORD}@127.0.0.1:{self.postgres.port}/sapi"
            ),
            "REDIS_STREAM_URL": f"redis://127.0.0.1:{self.redis_stream.port}/0",
            "REDIS_STATE_URL": f"redis://127.0.0.1:{self.redis_state.port}/0",
        }

    def restore_all(self) -> None:
        for container in self.all():
            container.restore()


def _run(
    name: str,
    kind: str,
    image: str,
    container_port: int,
    *env: str,
    command: tuple[str, ...] = (),
) -> Container:
    port = _free_port()
    env_args = [arg for item in env for arg in ("-e", item)]
    _docker(
        "run",
        "-d",
        "--name",
        name,
        "-p",
        f"127.0.0.1:{port}:{container_port}",
        *env_args,
        image,
        *command,
    )
    return Container(name=name, kind=kind, port=port)


@pytest.fixture(scope="session")
def infra() -> Iterator[Infra]:
    try:
        _docker("info")
    except (FileNotFoundError, subprocess.CalledProcessError):
        pytest.fail("The live suite needs a running Docker daemon.")

    prefix = f"sapi-it-{uuid.uuid4().hex[:8]}"
    started: list[Container] = []
    try:
        started.append(
            _run(
                f"{prefix}-postgres",
                "postgres",
                POSTGRES_IMAGE,
                5432,
                f"POSTGRES_PASSWORD={POSTGRES_PASSWORD}",
                "POSTGRES_DB=sapi",
            )
        )
        # AOF, so a stopped and restarted stream Redis keeps its streams and consumer groups.
        started.append(
            _run(
                f"{prefix}-redis-stream",
                "redis",
                REDIS_IMAGE,
                6379,
                command=("redis-server", "--appendonly", "yes"),
            )
        )
        started.append(_run(f"{prefix}-redis-state", "redis", REDIS_IMAGE, 6379))
        infra = Infra(*started)
        for container in infra.all():
            container.wait_ready()
        _alembic(infra, "upgrade", "head")
        yield infra
    finally:
        for container in started:
            subprocess.run(["docker", "rm", "-f", container.name], capture_output=True)


def _alembic(infra: Infra, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the alembic CLI against the session's Postgres, as an operator would."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=SERVICE_ROOT,
        env={**os.environ, "DATABASE_URL": infra.env()["DATABASE_URL"]},
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def alembic(infra: Infra) -> Callable[..., subprocess.CompletedProcess[str]]:
    """alembic("upgrade", "head") runs the alembic CLI against the session's Postgres."""
    return functools.partial(_alembic, infra)


@pytest.fixture(autouse=True)
def app_env(infra: Infra, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point app/config.py at the containers, and leave every container running after the test.

    Each test also starts with no HK Stock List registered, as a fresh process does; HKEX itself
    is real unless a test points the URLs elsewhere (hkex_urls).
    """
    for key in OPTIONAL_ENV:
        monkeypatch.delenv(key, raising=False)
    for key, value in infra.env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(hk_stock_list, "_hk_stock_list_cache", None)
    yield
    infra.restore_all()


@pytest.fixture(autouse=True)
def vertex_project(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cases marked vertex call the real Vertex AI, with the developer's VERTEX_AI_PROJECT and
    application default credentials; every other case gets a placeholder project."""
    if request.node.get_closest_marker("vertex") is None:
        monkeypatch.setenv("VERTEX_AI_PROJECT", PLACEHOLDER_VERTEX_PROJECT)
    elif not os.environ.get("VERTEX_AI_PROJECT"):
        pytest.fail("vertex cases need VERTEX_AI_PROJECT and application default credentials.")


@pytest.fixture
def hkex_urls(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Point HKEX URLs elsewhere: hkex_urls(every=url) for all three, or per constant name
    (e.g. _LIST_OF_SECURITIES_URL=url); the rest stay real."""

    def point(every: str | None = None, **urls: str) -> None:
        for name in HKEX_URL_CONSTANTS:
            url = every if every is not None else urls.get(name)
            if url is not None:
                monkeypatch.setattr(hk_stock_list, name, url)

    return point


@dataclasses.dataclass
class StubResponse:
    status: int
    body: bytes


class _StubServer(http.server.ThreadingHTTPServer):
    response: StubResponse
    requests: int


class _StubHandler(http.server.BaseHTTPRequestHandler):
    server: _StubServer

    def do_GET(self) -> None:
        self.server.requests += 1
        self.send_response(self.server.response.status)
        self.send_header("Content-Length", str(len(self.server.response.body)))
        self.end_headers()
        self.wfile.write(self.server.response.body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@dataclasses.dataclass
class StubHKEX:
    """A local HTTP server answering every GET with one canned response."""

    url: str
    server: _StubServer

    @property
    def requests(self) -> int:
        return self.server.requests


@pytest.fixture
def stub_hkex() -> Iterator[Callable[[StubResponse], StubHKEX]]:
    servers: list[_StubServer] = []

    def start(response: StubResponse) -> StubHKEX:
        server = _StubServer(("127.0.0.1", 0), _StubHandler)
        server.response = response
        server.requests = 0
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return StubHKEX(url=f"http://127.0.0.1:{server.server_address[1]}", server=server)

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@dataclasses.dataclass
class SADIRequest:
    cleaned_id: str
    at: float  # time.monotonic() when the request arrived


@dataclasses.dataclass
class _Behaviour:
    status: int
    delay_s: float
    title: str = "騰訊控股公佈第四季業績"
    body: str = "騰訊控股第四季廣告收入錄得強勁增長"


class _SADIServer(http.server.ThreadingHTTPServer):
    stub: "StubSADI"


class _SADIHandler(http.server.BaseHTTPRequestHandler):
    server: _SADIServer

    def do_GET(self) -> None:
        cleaned_id = self.path.rsplit("/", 1)[-1]
        stub = self.server.stub
        stub.requests.append(SADIRequest(cleaned_id, time.monotonic()))
        behaviour = stub.behaviours.get(cleaned_id, _Behaviour(200, 0.0))
        time.sleep(behaviour.delay_s)
        body = json.dumps(_sadi_body(cleaned_id, behaviour)).encode()
        self.send_response(behaviour.status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


def _sadi_body(cleaned_id: str, behaviour: _Behaviour) -> dict[str, object]:
    """SADI's GET /v1/cleaned_news/{cleaned_id} response body for a behaviour."""
    if behaviour.status == 404:
        return {"error_code": "COMMON-4004", "message": "Resource not found", "detail": {}}
    if behaviour.status == 503:
        return {"error_code": "COMMON-5001", "message": "Service unavailable", "detail": {}}
    return {
        "cleaned_id": cleaned_id,
        "raw_id": str(uuid.uuid4()),
        "title_cleaned": behaviour.title,
        "body_cleaned": behaviour.body,
        "published_at": "2025-01-15T00:00:00Z",
        "source_name": "MINGPAO",
        "source_url": f"https://finance.mingpao.com/{cleaned_id}",
        "created_at": "2025-01-15T00:05:00Z",
    }


class StubSADI:
    """A local HTTP server standing in for SADI's GET /v1/cleaned_news/{cleaned_id}. Every id
    answers 200 with an article unless respond() says otherwise. It can be stopped and started
    again on the same port."""

    def __init__(self) -> None:
        self.requests: list[SADIRequest] = []
        self.behaviours: dict[str, _Behaviour] = {}
        self._server: _SADIServer | None = None
        self._port = 0
        self.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def respond(self, cleaned_id: uuid.UUID, status: int, delay_s: float = 0.0) -> None:
        self.behaviours[str(cleaned_id)] = _Behaviour(status, delay_s)

    def serve(self, cleaned_id: uuid.UUID, title: str, body: str) -> None:
        """Answer cleaned_id with 200 and an article of this title and body."""
        self.behaviours[str(cleaned_id)] = _Behaviour(200, 0.0, title, body)

    def requests_for(self, cleaned_id: uuid.UUID) -> list[SADIRequest]:
        return [r for r in self.requests if r.cleaned_id == str(cleaned_id)]

    def start(self) -> None:
        server = _SADIServer(("127.0.0.1", self._port), _SADIHandler)
        server.stub = self
        self._port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self._server = server

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


@pytest.fixture
def stub_sadi() -> Iterator[StubSADI]:
    stub = StubSADI()
    yield stub
    stub.stop()


@pytest.fixture
def black_hole_url() -> Iterator[str]:
    """A listener that accepts connections and never replies."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"


@pytest.fixture
def start_app(
    infra: Infra, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., contextlib.AbstractAsyncContextManager[AsyncClient]]:
    """Start app.main:app through its real lifespan; keyword arguments set extra env vars."""

    @contextlib.asynccontextmanager
    async def start(**env: str) -> AsyncIterator[AsyncClient]:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        async with main.app.router.lifespan_context(main.app):
            try:
                transport = ASGITransport(app=main.app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    yield client
            finally:
                # Before lifespan shutdown: closing clients against a paused server can block.
                infra.restore_all()

    return start
