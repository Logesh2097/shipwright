"""Shared fixtures.

Tests run against a real PostgreSQL database (``asea_test`` in the Docker Compose ``db``
service, schema created by Alembic), the real FastAPI app, the real LangGraph workflow and
real MCP servers (in-process by default, stdio where marked). Only the LLM is scripted and
GitHub's HTTP API is mocked.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="asea-tests-"))
_REPOS_ROOT = _TMP_ROOT / "repos"
_REPOS_ROOT.mkdir()


def _test_database_url() -> str:
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    from app.config import Settings

    base = Settings().database_url
    head, _, _name = base.rpartition("/")
    return f"{head}/asea_test"


os.environ["TEST_DATABASE_URL"] = _test_database_url()
os.environ.update(
    {
        "ENVIRONMENT": "test",
        "DATABASE_URL": os.environ["TEST_DATABASE_URL"],
        "REPOS_ALLOWED_ROOTS": str(_REPOS_ROOT),
        "SANDBOX_MODE": "local",
        "LLM_PROVIDER": "openai",
        "OPENAI_API_KEY": "",
        "GEMINI_API_KEY": "",
        "LLM_REQUESTS_PER_MINUTE": "0",
        "LLM_MODEL": "",
        "ALLOW_REGISTRATION": "true",
        "LOGIN_MAX_ATTEMPTS": "5",
        "LOG_LEVEL": "WARNING",
        "AGENT_MAX_TOOL_STEPS": "12",
    }
)

import httpx  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.agent.llm import ScriptedChatModel  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.session import Database, set_database  # noqa: E402
from app.evaluation.fixtures import create_sample_repository  # noqa: E402
from app.mcp_client.manager import SessionSpec  # noqa: E402
from app.mcp_server.context import ServerContext  # noqa: E402
from app.mcp_server.server import build_server  # noqa: E402
from app.runtime import Runtime, set_runtime  # noqa: E402
from app.sandbox.runner import SandboxConfig  # noqa: E402

PASSWORD = "correct-horse-battery"


class Script:
    """Holds the scripted model behaviour and the mocked GitHub transport for the current test."""

    def __init__(self) -> None:
        self.responder: Callable[..., AIMessage] | None = None
        self.github_transport: httpx.AsyncBaseTransport | None = None
        self.github_push_url: str | None = None
        self.llm_calls = 0

    def respond(self, messages: list[Any], tools: list[dict[str, Any]], tool_choice: Any) -> AIMessage:
        self.llm_calls += 1
        if self.responder is None:
            raise AssertionError("This test called the LLM without setting script.responder")
        return self.responder(messages, tools, tool_choice)


@pytest.fixture(scope="session")
def settings() -> Any:
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture(scope="session", autouse=True)
async def _database(settings: Any) -> AsyncIterator[Database]:
    """Create the test database if needed and build its schema with Alembic."""
    url = settings.database_url
    admin_url, _, name = url.rpartition("/")
    assert name.endswith("_test"), "refusing to run tests against a non-test database"
    admin = create_async_engine(f"{admin_url}/postgres", isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        exists = await conn.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name})
        if not exists:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    await admin.dispose()

    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()

    from alembic.config import Config

    from alembic import command

    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["configure_logger"] = False
    await asyncio.to_thread(command.upgrade, config, "head")

    database = Database(url)
    set_database(database)
    yield database
    await database.dispose()
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


@pytest.fixture
def script() -> Script:
    return Script()


@pytest.fixture
async def runtime(_database: Database, settings: Any, script: Script) -> AsyncIterator[Runtime]:
    from app.api.routes.auth import reset_throttle

    def server_factory(spec: SessionSpec) -> Any:
        context = ServerContext(
            workspace_root=Path(spec.workspace_root) if spec.workspace_root else None,
            toolsets=("github",) if spec.key.kind == "github" else ("fs", "git", "terminal", "testing"),
            approval_secret=settings.approval_token_secret.get_secret_value(),
            sandbox_config=SandboxConfig(mode="local"),
            github_token=spec.github_token,
            github_transport=script.github_transport,
            github_push_url=script.github_push_url,
        )
        return build_server(context)

    async with _database.engine.begin() as conn:
        tables = await conn.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version'"))
        names = ", ".join(f'"{t}"' for t in tables)
        if names:
            await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    reset_throttle()
    rt = Runtime(
        settings,
        _database,
        llm_factory=lambda: ScriptedChatModel(responder=script.respond),
        mcp_server_factory=server_factory,
        checkpointer=InMemorySaver(),
    )
    set_runtime(rt)
    await rt.startup()
    yield rt
    await rt.shutdown()
    set_runtime(None)


@pytest.fixture
async def client(runtime: Runtime) -> AsyncIterator[httpx.AsyncClient]:
    from app.main import app

    app.state.github_transport = None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test/api/v1") as c:
        yield c


async def register(client: httpx.AsyncClient, email: str, name: str = "") -> dict[str, str]:
    response = await client.post("/auth/register", json={"email": email, "full_name": name or email.split("@")[0], "password": PASSWORD})
    assert response.status_code == 201, response.text
    login = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest.fixture
async def admin(client: httpx.AsyncClient) -> dict[str, str]:
    return await register(client, "admin@example.com", "Admin")


@pytest.fixture
async def developer(client: httpx.AsyncClient, admin: dict[str, str]) -> dict[str, str]:
    return await register(client, "dev@example.com", "Dev")


@pytest.fixture
def repo_path() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="shop-", dir=_REPOS_ROOT))
    path.rmdir()
    create_sample_repository(path)
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
async def project(client: httpx.AsyncClient, admin: dict[str, str]) -> dict[str, Any]:
    response = await client.post("/projects", json={"name": "Shop", "description": "Sample"}, headers=admin)
    assert response.status_code == 201, response.text
    return response.json()


async def wait_for(predicate: Callable[[], Any], timeout: float = 30.0, interval: float = 0.05) -> Any:
    """Poll an async predicate until it returns something truthy."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        value = await predicate()
        if value:
            return value
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(interval)


@pytest.fixture
async def repository(client: httpx.AsyncClient, admin: dict[str, str], project: dict[str, Any], repo_path: Path) -> dict[str, Any]:
    response = await client.post(f"/projects/{project['id']}/repositories", json={"local_path": str(repo_path)}, headers=admin)
    assert response.status_code == 201, response.text
    repo_id = response.json()["id"]

    async def ready() -> Any:
        current = (await client.get(f"/repositories/{repo_id}", headers=admin)).json()
        assert current["status"] != "error", current
        return current if current["status"] == "ready" else None

    return await wait_for(ready)


async def run_task(
    client: httpx.AsyncClient, headers: dict[str, str], repository_id: str, request: str, until: tuple[str, ...]
) -> dict[str, Any]:
    """Create a task and wait until it reaches one of the given statuses."""
    response = await client.post("/tasks", json={"repository_id": repository_id, "request": request}, headers=headers)
    assert response.status_code == 201, response.text
    task_id = response.json()["id"]
    return await wait_status(client, headers, task_id, until)


async def wait_status(
    client: httpx.AsyncClient, headers: dict[str, str], task_id: str, until: tuple[str, ...], timeout: float = 60.0
) -> dict[str, Any]:
    async def reached() -> Any:
        task = (await client.get(f"/tasks/{task_id}", headers=headers)).json()
        if task["status"] in until:
            return task
        assert task["status"] not in ("failed", "cancelled") or task["status"] in until, task["latest_run"]
        return None

    return await wait_for(reached, timeout=timeout)
