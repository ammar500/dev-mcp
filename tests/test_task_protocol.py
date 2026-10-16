from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp import StdioServerParameters
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from dev_mcp.access import EDITOR, Access
from dev_mcp.config import Settings, bearer_token, write_private
from dev_mcp.server import create_server
from dev_mcp.tasks.context import import_file
from dev_mcp.tasks.service import OPERATIONS
from tests.test_server import daemon  # noqa: F401


@pytest.fixture
def profiles(settings: Settings) -> Access:
    settings.initialize()
    a = Access(settings)
    a.configure_workspace("personal", "Europe/Berlin", True, ["fake"])
    a.create_profile("editor", "personal", sorted(EDITOR | {"delegate", "skills"}))
    a.create_profile("work", "work", sorted(EDITOR | {"skills"}))
    a.create_profile("reader", "personal", ["tasks.read"])
    a.create_profile("worker", "personal", ["worker"])
    return a


@pytest.fixture
def profile_daemon(profiles: Access, request: pytest.FixtureRequest) -> tuple[Settings, Any]:
    return request.getfixturevalue("daemon")  # type: ignore[no-any-return]


async def invoke(c: Client, name: str, **args: Any) -> dict[str, Any]:
    if name in OPERATIONS and OPERATIONS[name][1]:
        args.setdefault("request_id", str(uuid.uuid4()))
    result = await c.call_tool(name, args)
    assert not result.is_error, result
    assert len(result.model_dump_json().encode()) <= 65536
    assert result.structured_content is not None
    return result.structured_content


async def workflow(c: Client, worker: Client, checkout: Path) -> str:
    names = {t.name for t in (await c.list_tools()).tools}
    assert {n for n, (p, _, _) in OPERATIONS.items() if p != "worker"} <= names
    assert not any(n.startswith("worker_") for n in names)
    assert {t.name for t in (await worker.list_tools()).tools} == {
        "workspace_context",
        "worker_claim",
        "worker_heartbeat",
        "worker_finish",
    }
    assert (await invoke(c, "workspace_context"))["workspace"] == "personal"
    t = await invoke(c, "tasks_create", title="Protocol task", checkout=str(checkout))
    assert (await invoke(c, "tasks_list", query="Protocol"))["items"][0]["id"] == t["id"]
    t = await invoke(
        c,
        "tasks_update",
        task_id=t["id"],
        expected_version=1,
        patch={"instructions": "Frozen directions"},
    )
    reminder = await invoke(
        c, "reminders_create", task_id=t["id"], remind_at="2099-01-01T12:00:00+01:00"
    )
    assert (await invoke(c, "reminders_list"))["items"]
    await invoke(
        c,
        "reminders_update",
        reminder_id=reminder["id"],
        expected_version=1,
        patch={"status": "acknowledged"},
    )
    raw = "  stopped here\r\n\tdebug Ω  "
    pause = await invoke(c, "pauses_save", raw_text=raw, task_id=t["id"])
    assert (await invoke(c, "pauses_find", pause_id=pause["id"]))["raw_text"] == raw
    await invoke(
        c, "pauses_update", pause_id=pause["id"], expected_version=1, patch={"resumed": True}
    )
    run = await invoke(
        c,
        "tasks_claim",
        task_id=t["id"],
        expected_version=t["version"],
        session_ref="existing-chat",
    )
    assert run["snapshot"]["instructions"] == "Frozen directions"
    await invoke(c, "tasks_report", attempt_id=run["id"], expected_version=1, action="release")
    t = await invoke(c, "tasks_get", task_id=t["id"])
    d = await invoke(
        c,
        "tasks_delegate",
        task_id=t["id"],
        expected_version=t["version"],
        execution_profile="fake",
    )
    assert d["execution_state"] == "waiting_for_worker"
    await invoke(c, "delegations_cancel", delegation_id=d["id"], expected_version=1)
    t = await invoke(c, "tasks_get", task_id=t["id"])
    await invoke(
        c,
        "tasks_delegate",
        task_id=t["id"],
        expected_version=t["version"],
        execution_profile="fake",
    )
    run = await invoke(worker, "worker_claim", execution_profiles=["fake"], session_ref="fake")
    assert run["snapshot"]["instructions"] == "Frozen directions"
    run = await invoke(worker, "worker_heartbeat", attempt_id=run["id"])
    await invoke(
        worker,
        "worker_finish",
        attempt_id=run["id"],
        expected_version=run["version"],
        action="complete",
        result={"summary": "Fake worker records completion; no agent launched"},
    )
    t = await invoke(c, "tasks_get", task_id=t["id"])
    t = await invoke(
        c, "tasks_transition", task_id=t["id"], expected_version=t["version"], action="reopen"
    )
    t = await invoke(c, "tasks_archive", task_id=t["id"], expected_version=t["version"])
    await invoke(c, "tasks_archive", task_id=t["id"], expected_version=t["version"], archived=False)
    for view in ("history", "attempts", "delegations"):
        assert (await invoke(c, "tasks_get", task_id=t["id"], view=view))["items"]
    assert list(checkout.iterdir()) == []
    return str(t["id"])


async def test_inprocess_task_tools_and_revocation(profiles: Access, tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    async with (
        Client(create_server(profiles.settings, profile="editor")) as c,
        Client(create_server(profiles.settings, profile="worker")) as w,
    ):
        await workflow(c, w, checkout)
        denied = await c.call_tool(
            "worker_claim",
            {"request_id": "x", "execution_profiles": ["fake"], "session_ref": "wrong"},
        )
        assert denied.is_error
        profiles.configure_workspace("personal", "Europe/Berlin", False, ["fake"])
        assert not (await invoke(c, "workspace_context"))["delegation_enabled"]
        del profiles.profiles["editor"]
        profiles.save()
        assert (await c.call_tool("workspace_context", {})).is_error
        assert (await c.call_tool("skills_search", {})).is_error


def stdio(settings: Settings, profile: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "dev_mcp", "--profile", profile, "stdio"],
        env={
            **os.environ,
            "DEV_MCP_CONFIG": str(settings.config),
            "DEV_MCP_STATE": str(settings.state),
            "DEV_MCP_LIBRARY": str(settings.library),
        },
    )


@pytest.mark.integration
async def test_real_stdio_task_and_fake_worker(profiles: Access, tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    async with (
        Client(stdio(profiles.settings, "editor"), mode="legacy") as c,
        Client(stdio(profiles.settings, "worker"), mode="legacy") as w,
    ):
        await workflow(c, w, checkout)
    params = stdio(profiles.settings, "editor")
    failed = subprocess.run(
        [sys.executable, "-m", "dev_mcp", "stdio"], env=params.env, capture_output=True, timeout=10
    )
    assert failed.returncode == 1 and failed.stdout == b""
    assert b"requires --profile" in failed.stderr


@asynccontextmanager
async def connected(settings: Settings, name: str) -> AsyncIterator[Client]:
    async with httpx2.AsyncClient(
        headers={"Authorization": "Bearer " + Access(settings).token(name)}
    ) as http:
        async with Client(
            streamable_http_client(f"http://127.0.0.1:{settings.port}/mcp/{name}", http_client=http)
        ) as c:
            yield c


@pytest.mark.integration
async def test_real_http_isolation_and_concurrent_clients(
    profile_daemon: tuple[Settings, Any], package: Path, tmp_path: Path
) -> None:
    settings, process = profile_daemon
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    source = tmp_path / "context.txt"
    source.write_text("Private context")
    asset = import_file(Access(settings), "personal", source)
    async with (
        connected(settings, "editor") as c,
        connected(settings, "worker") as w,
        connected(settings, "work") as other,
    ):
        task_id = await workflow(c, w, checkout)
        for view in ("task", "history", "attempts", "delegations", "context"):
            result = await other.call_tool("tasks_get", {"task_id": task_id, "view": view})
            assert result.is_error and result.structured_content["error"]["code"] == "not_found"
        assert (await invoke(other, "tasks_list"))["items"] == []
        assert (await invoke(other, "pauses_find"))["items"] == []
        assert (await invoke(other, "skills_search"))["skills"] == []
        assert (await invoke(c, "skills_search"))["skills"][0]["skill_id"] == "example"
        context = [{"kind": "managed", "value": asset["id"]}]
        made = await invoke(c, "tasks_create", title="Context", context=context)
        assert (await invoke(c, "tasks_get", task_id=made["id"], view="context"))[
            "text"
        ] == "Private context"
        result = await other.call_tool(
            "tasks_create", {"request_id": "guess", "title": "Guess", "context": context}
        )
        assert result.is_error
        # Concurrent clients share the same database and cannot independently claim one task.
        async with connected(settings, "editor") as second:
            args = {"task_id": made["id"], "expected_version": 1, "session_ref": "racer"}
            results = await asyncio.gather(
                *[
                    client.call_tool("tasks_claim", {"request_id": str(uuid.uuid4()), **args})
                    for client in (c, second)
                ]
            )
            assert sum(not result.is_error for result in results) == 1
        assert process.poll() is None
    assert list(checkout.iterdir()) == []


@pytest.mark.integration
async def test_profile_http_auth_rotation_and_origins(profile_daemon: tuple[Settings, Any]) -> None:
    settings, _ = profile_daemon
    access = Access(settings)
    url = f"http://127.0.0.1:{settings.port}/mcp/editor"
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    headers = {"Accept": "application/json, text/event-stream"}
    async with httpx2.AsyncClient() as http:
        for token in ("incorrect", bearer_token(settings), access.token("work")):
            response = await http.post(
                url, json=body, headers={**headers, "Authorization": "Bearer " + token}
            )
            assert response.status_code == 401
        valid = {**headers, "Authorization": "Bearer " + access.token("editor")}
        assert (await http.post(url, json=body, headers=valid)).status_code == 200
        for extra in ({"Origin": "https://evil.example"}, {"Host": "evil.example"}):
            response = await http.post(url, json=body, headers={**valid, **extra})
            assert response.status_code in {403, 421}
        replacement = "rotated-" + uuid.uuid4().hex
        write_private(access.token_path("editor"), replacement.encode())
        assert (await http.post(url, json=body, headers=valid)).status_code == 401
        valid["Authorization"] = "Bearer " + replacement
        assert (await http.post(url, json=body, headers=valid)).status_code == 200
        del access.profiles["editor"]
        access.save()
        assert (await http.post(url, json=body, headers=valid)).status_code == 401


async def test_escaped_results_remain_retrievable_and_retries_are_scoped(
    profiles: Access, tmp_path: Path
) -> None:
    async with (
        Client(create_server(profiles.settings, profile="editor")) as c,
        Client(create_server(profiles.settings, profile="work")) as w,
    ):
        raw = ('"\\\n' * 1800) + " preserved"
        pause = await invoke(c, "pauses_save", raw_text=raw, request_id="same")
        other = await invoke(w, "pauses_save", raw_text="work", request_id="same")
        assert pause["id"] != other["id"]
        assert (await invoke(c, "pauses_find", pause_id=pause["id"]))["raw_text"] == raw
        result = await c.call_tool("pauses_find", {"limit": 50})
        assert not result.is_error and len(result.model_dump_json().encode()) <= 65536
        args = {"client": "test", "checkout": str(tmp_path), "task": "scope"}
        bad = await c.call_tool("skills_session_open", {"request_id": "", **args})
        assert bad.is_error
        personal = await invoke(c, "skills_session_open", request_id="same", **args)
        work = await invoke(w, "skills_session_open", request_id="same", **args)
        assert personal["session_id"] != work["session_id"]
        denied = await w.call_tool("skills_sessions", {"session_id": personal["session_id"]})
        assert denied.is_error


def test_owner_cli_credentials_and_permissions(settings: Settings) -> None:
    environment = {
        **os.environ,
        "DEV_MCP_CONFIG": str(settings.config),
        "DEV_MCP_STATE": str(settings.state),
        "DEV_MCP_LIBRARY": str(settings.library),
    }

    def command(*args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, "-m", "dev_mcp", *args],
            env=environment,
            capture_output=True,
            timeout=10,
        )

    configured = command("workspace", "configure", "work", "--timezone", "Europe/Berlin")
    assert configured.returncode == 0
    created = command("profile", "create", "work-client", "--workspace", "work")
    assert created.returncode == 0
    access = Access(settings)
    old = access.token("work-client")
    assert old.encode() not in created.stdout + created.stderr
    assert access.token_path("work-client").stat().st_mode & 0o777 == 0o600
    rotated = command("profile", "rotate", "work-client")
    assert rotated.returncode == 0 and access.token("work-client") != old
    assert command("profile", "create", "bad", "--permissions", "worker,tasks.read").returncode == 1
    assert command("profile", "create", "legacy").returncode == 1
    assert command("profile", "revoke", "work-client").returncode == 0
    assert "work-client" not in Access(settings).profiles
