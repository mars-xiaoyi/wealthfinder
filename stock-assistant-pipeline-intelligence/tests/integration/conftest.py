"""
Real Postgres and Redis containers for the `live` integration suite, driven through the
docker CLI. Host ports are fixed per session (not Docker-assigned) so `docker start` after
`docker stop` brings a container back on the same port the app is configured with.
"""

import contextlib
import dataclasses
import socket
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator

import pytest
from httpx import ASGITransport, AsyncClient

from app import main

POSTGRES_IMAGE = "postgres:16-alpine"
REDIS_IMAGE = "redis:7-alpine"
POSTGRES_PASSWORD = "password"
READY_TIMEOUT_S = 30.0
# Env vars with defaults in app/config.py; cleared so a developer's shell can't change them.
OPTIONAL_ENV = ("DB_POOL_SIZE", "DB_CLOSE_TIMEOUT_S", "HEALTH_CHECK_TIMEOUT_S")


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


def _run(name: str, kind: str, image: str, container_port: int, *env: str) -> Container:
    port = _free_port()
    env_args = [arg for item in env for arg in ("-e", item)]
    _docker(
        "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:{container_port}", *env_args, image
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
        started.append(_run(f"{prefix}-redis-stream", "redis", REDIS_IMAGE, 6379))
        started.append(_run(f"{prefix}-redis-state", "redis", REDIS_IMAGE, 6379))
        infra = Infra(*started)
        for container in infra.all():
            container.wait_ready()
        yield infra
    finally:
        for container in started:
            subprocess.run(["docker", "rm", "-f", container.name], capture_output=True)


@pytest.fixture(autouse=True)
def app_env(infra: Infra, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point app/config.py at the containers, and leave every container running after the test."""
    for key in OPTIONAL_ENV:
        monkeypatch.delenv(key, raising=False)
    for key, value in infra.env().items():
        monkeypatch.setenv(key, value)
    yield
    infra.restore_all()


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
