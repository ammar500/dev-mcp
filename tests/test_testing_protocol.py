from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp.client import Client

from dev_mcp.access import Access
from dev_mcp.config import Settings
from dev_mcp.server import create_server
from tests import test_testing
from tests.test_server import daemon  # noqa: F401
from tests.test_task_protocol import connected, invoke, stdio

queue_access = test_testing.queue_access
repo = test_testing.repo


@pytest.fixture
def testing_daemon(queue_access: Access, request: pytest.FixtureRequest) -> tuple[Settings, Any]:
    return request.getfixturevalue("daemon")  # type: ignore[no-any-return]


async def workflow(first: Client, second: Client, repo: Path) -> str:
    request_id = str(uuid.uuid4())
    names = {t.name: t for t in (await first.list_tools()).tools}
    assert {"testing_join", "testing_check", "testing_done", "testing_list"} <= names.keys()
    assert names["testing_check"].annotations.read_only_hint is False
    tickets = await asyncio.gather(
        *[
            invoke(
                c,
                "testing_join",
                source_worktree=str(repo),
                session_ref=f"chat-{i}",
                request_id=request_id,
            )
            for i, c in enumerate((first, second))
        ]
    )
    assert tickets[0]["id"] != tickets[1]["id"]
    responses = await asyncio.gather(
        *[
            invoke(c, "testing_check", ticket_id=t["id"])
            for c, t in zip((first, second), tickets, strict=True)
        ]
    )
    assert sum(t["can_test"] for t in responses) == 1
    active = next(i for i, t in enumerate(responses) if t["can_test"])
    holder, waiter = (first, second)[active], (first, second)[1 - active]
    own, waiting = tickets[active], tickets[1 - active]
    denied = await waiter.call_tool(
        "testing_done",
        {
            "request_id": "guess",
            "ticket_id": own["id"],
            "stack_stopped": True,
            "cleanup_complete": True,
        },
    )
    assert denied.is_error
    assert (await invoke(waiter, "testing_list"))["items"][0]["id"] == waiting["id"]
    for c, t in ((holder, waiting), (waiter, own)):
        assert (await c.call_tool("testing_list", {"ticket_id": t["id"]})).is_error
    failed = await holder.call_tool(
        "testing_done", {"request_id": "unclean", "ticket_id": own["id"]}
    )
    assert failed.is_error
    assert not (await invoke(waiter, "testing_check", ticket_id=waiting["id"]))["can_test"]
    await invoke(
        holder,
        "testing_done",
        ticket_id=own["id"],
        stack_stopped=True,
        cleanup_complete=True,
        outcome="Fake stack stopped; test run completed",
    )
    assert not (await invoke(holder, "testing_check", ticket_id=own["id"]))["can_test"]
    assert (await invoke(waiter, "testing_check", ticket_id=waiting["id"]))["can_test"]
    await invoke(
        waiter, "testing_done", ticket_id=waiting["id"], stack_stopped=True, cleanup_complete=True
    )
    assert (repo / "tracked.txt").read_text() == "unchanged\n"
    assert subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"]) == b""
    return str(own["id"])


async def test_testing_inprocess_permissions(queue_access: Access, repo: Path) -> None:
    async with (
        Client(create_server(queue_access.settings, profile="tester")) as first,
        Client(create_server(queue_access.settings, profile="work")) as second,
    ):
        await workflow(first, second, repo)
    for profile, expected in (("reader", {"testing_list"}), ("worker", set()), ("legacy", set())):
        async with Client(create_server(queue_access.settings, profile=profile)) as client:
            assert {
                t.name for t in (await client.list_tools()).tools if t.name.startswith("testing_")
            } == expected
            assert (await client.call_tool("testing_check", {"ticket_id": "guess"})).is_error


@pytest.mark.integration
async def test_testing_real_stdio_restart(queue_access: Access, repo: Path) -> None:
    settings = queue_access.settings
    async with (
        Client(stdio(settings, "tester"), mode="legacy") as first,
        Client(stdio(settings, "work"), mode="legacy") as second,
    ):
        await workflow(first, second, repo)
        ticket = await invoke(
            first, "testing_join", source_worktree=str(repo), session_ref="restart"
        )
        assert (await invoke(first, "testing_check", ticket_id=ticket["id"]))["can_test"]
    async with (
        Client(stdio(settings, "tester"), mode="legacy") as first,
        Client(stdio(settings, "work"), mode="legacy") as second,
    ):
        next_ticket = await invoke(
            second, "testing_join", source_worktree=str(repo), session_ref="next"
        )
        assert not (await invoke(second, "testing_check", ticket_id=next_ticket["id"]))["can_test"]
        assert (await invoke(first, "testing_check", ticket_id=ticket["id"]))["can_test"]
        await invoke(
            first, "testing_done", ticket_id=ticket["id"], stack_stopped=True, cleanup_complete=True
        )
        assert (await invoke(second, "testing_check", ticket_id=next_ticket["id"]))["can_test"]


@pytest.mark.integration
async def test_testing_http_and_stdio_share_fifo(
    testing_daemon: tuple[Settings, Any], repo: Path
) -> None:
    settings, _ = testing_daemon
    async with connected(settings, "tester") as first, connected(settings, "work") as second:
        await workflow(first, second, repo)
    async with (
        connected(settings, "tester") as first,
        Client(stdio(settings, "work"), mode="legacy") as second,
    ):
        await workflow(first, second, repo)


@pytest.mark.integration
async def test_testing_endpoint_auth_and_origins(testing_daemon: tuple[Settings, Any]) -> None:
    settings, _ = testing_daemon
    access = Access(settings)
    url = f"http://127.0.0.1:{settings.port}/mcp/tester"
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "queue-test", "version": "1"},
        },
    }
    headers = {"Accept": "application/json, text/event-stream"}
    async with httpx2.AsyncClient() as http:
        for token in ("bad", access.token("legacy"), access.token("work")):
            response = await http.post(
                url, json=body, headers={**headers, "Authorization": "Bearer " + token}
            )
            assert response.status_code == 401
        headers["Authorization"] = "Bearer " + access.token("tester")
        assert (await http.post(url, json=body, headers=headers)).status_code == 200
        for extra in ({"Origin": "https://evil.example"}, {"Host": "evil.example"}):
            assert (await http.post(url, json=body, headers={**headers, **extra})).status_code in {
                403,
                421,
            }


def test_owner_testing_recovery_cli(queue_access: Access, repo: Path) -> None:
    from dev_mcp.tasks.service import TaskService
    from tests.test_testing import check, join

    service = TaskService(queue_access, queue_access.profile("tester"))
    ticket = join(service, repo)
    assert check(service, ticket)["can_test"]

    def command(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "dev_mcp",
                "workspace",
                "testing-recover",
                "personal",
                "--ticket-id",
                ticket["id"],
                *args,
            ],
            env={
                **os.environ,
                "DEV_MCP_STATE": str(queue_access.settings.state),
                "DEV_MCP_CONFIG": str(queue_access.settings.config),
                "DEV_MCP_LIBRARY": str(queue_access.settings.library),
            },
            capture_output=True,
            text=True,
            timeout=10,
        )

    assert command().returncode == 1
    assert command("--reason", "Inspect").returncode == 1
    result = command(
        "--reason", "Stopped stack and restored checkout", "--stack-stopped", "--cleanup-complete"
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "reconciled"
    assert not check(service, ticket)["can_test"]
