from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp import StdioServerParameters
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from dev_mcp.config import Settings, bearer_token
from dev_mcp.server import create_server

TOOLS = {
    "skills_search",
    "skills_inspect",
    "skills_session_open",
    "skills_acquire",
    "skills_sessions",
    "skills_release",
    "skills_update_prepare",
    "skills_update_apply",
}


async def invoke(connection: Client, name: str, **args: Any) -> dict[str, Any]:
    result = await connection.call_tool("skills_" + name, args)
    assert not result.is_error, result
    assert len(result.model_dump_json().encode()) < 64 * 1024
    assert result.structured_content is not None
    return result.structured_content


async def workflow(client: Client, checkout: Path, *, client_name: str = "test") -> None:
    assert {t.name for t in (await client.list_tools()).tools} == TOOLS
    assert (await invoke(client, "search"))["skills"][0]["skill_id"] == "example"
    inspected = await invoke(client, "inspect", skill_id="example")
    session = await invoke(
        client,
        "session_open",
        request_id=str(uuid.uuid4()),
        client=client_name,
        checkout=str(checkout),
        task="e2e",
    )
    acquisition = await invoke(
        client,
        "acquire",
        request_id=str(uuid.uuid4()),
        session_id=session["session_id"],
        skill_id="example",
        revision=inspected["revision"],
    )
    path = Path(acquisition["skill_path"])
    assert "Read assets" in path.read_text()
    assert checkout not in path.parents
    path.write_text(path.read_text() + "Human-requested improvement.\n")
    proposal = await invoke(
        client,
        "update_prepare",
        request_id=str(uuid.uuid4()),
        acquisition_id=acquisition["acquisition_id"],
        changes=[{"path": "SKILL.md", "action": "replace"}],
        human_instruction="Please improve my original example skill.",
        reason="E2E requested edit",
    )
    await invoke(
        client, "update_apply", request_id=str(uuid.uuid4()), proposal_id=proposal["proposal_id"]
    )
    found = await invoke(client, "sessions", checkout=str(checkout), client=client_name)
    assert found["sessions"][0]["id"] == session["session_id"]
    released = await invoke(client, "release", session_id=session["session_id"], close_session=True)
    assert released["session_status"] == "closed"
    assert not path.exists()


async def test_inprocess_all_eight(settings: Settings, package: Path, tmp_path: Path) -> None:
    checkout = tmp_path / "consumer"
    checkout.mkdir()
    async with Client(create_server(settings), raise_exceptions=True) as client:
        await workflow(client, checkout)
        bad = await client.call_tool(
            "skills_update_prepare",
            {
                "request_id": "x",
                "acquisition_id": "x",
                "changes": [],
                "human_instruction": "",
                "reason": "",
            },
        )
        assert bad.is_error
    assert list(checkout.iterdir()) == []


@pytest.mark.integration
async def test_stdio_all_eight(settings: Settings, package: Path, tmp_path: Path) -> None:
    checkout = tmp_path / "consumer"
    checkout.mkdir()
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "dev_mcp", "--profile", "legacy"],
        cwd=Path(__file__).resolve().parents[1],
        env={
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "DEV_MCP_LIBRARY": str(settings.library),
            "DEV_MCP_STATE": str(settings.state),
            "DEV_MCP_CONFIG": str(settings.config),
        },
    )
    async with Client(
        parameters, raise_exceptions=True, read_timeout_seconds=15, mode="legacy"
    ) as client:
        await workflow(client, checkout)
    assert list(checkout.iterdir()) == []


@pytest.fixture
def daemon(settings: Settings) -> Iterator[tuple[Settings, subprocess.Popen[bytes]]]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings = dataclasses.replace(settings, port=port)
    settings.save()
    bearer_token(settings)
    env = {
        **os.environ,
        "DEV_MCP_LIBRARY": str(settings.library),
        "DEV_MCP_STATE": str(settings.state),
        "DEV_MCP_CONFIG": str(settings.config),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "dev_mcp", "serve", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        for _ in range(200):
            if process.poll() is not None:
                raise AssertionError(
                    process.stderr.read().decode() if process.stderr else "daemon exited"
                )
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.025)
        else:
            raise AssertionError("daemon did not start")
        yield settings, process
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        if process.stderr:
            process.stderr.close()


@asynccontextmanager
async def http_client(settings: Settings, *, mode: str = "auto") -> AsyncIterator[Client]:
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {bearer_token(settings)}"}
    ) as http:
        transport = streamable_http_client(
            f"http://127.0.0.1:{settings.port}/mcp", http_client=http
        )
        async with Client(
            transport, raise_exceptions=True, read_timeout_seconds=15, mode=mode
        ) as client:
            yield client


@pytest.mark.integration
async def test_http_all_eight_and_two_clients(
    daemon: tuple[Settings, subprocess.Popen[bytes]], package: Path, tmp_path: Path
) -> None:
    settings, process = daemon
    one, two = tmp_path / "consumer-one", tmp_path / "consumer-two"
    one.mkdir()
    two.mkdir()
    async with http_client(settings, mode="legacy") as first, http_client(settings) as second:
        first_session, second_session = await asyncio.gather(
            *[
                invoke(
                    c,
                    "session_open",
                    request_id=str(uuid.uuid4()),
                    client=n,
                    checkout=str(p),
                    task="parallel",
                )
                for c, n, p in [(first, "one", one), (second, "two", two)]
            ]
        )
        copies = await asyncio.gather(
            *[
                invoke(
                    c,
                    "acquire",
                    request_id=str(uuid.uuid4()),
                    session_id=s["session_id"],
                    skill_id="example",
                )
                for c, s in [(first, first_session), (second, second_session)]
            ]
        )
        assert copies[0]["directory"] != copies[1]["directory"]
        # The client explicitly uses the borrowed script; the server never executes it.
        script = Path(copies[0]["directory"]) / "scripts/run.py"
        used = subprocess.run(
            [sys.executable, str(script)], cwd=one, check=True, capture_output=True
        )
        assert b"explicitly run by client" in used.stdout
        await workflow(first, one, client_name="publisher")
        assert "Human-requested improvement" not in Path(copies[1]["skill_path"]).read_text()
        await asyncio.gather(
            *[
                invoke(c, "release", session_id=s["session_id"], close_session=True)
                for c, s in [(first, first_session), (second, second_session)]
            ]
        )
        assert process.poll() is None
    assert list(one.iterdir()) == list(two.iterdir()) == []


@pytest.mark.integration
async def test_http_auth_host_origin_and_size(
    daemon: tuple[Settings, subprocess.Popen[bytes]],
) -> None:
    settings, _ = daemon
    url = f"http://127.0.0.1:{settings.port}/mcp"
    async with httpx2.AsyncClient() as client:
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
        assert (await client.post(url, json=body, headers=headers)).status_code == 401
        headers["Authorization"] = "Bearer incorrect"
        assert (await client.post(url, json=body, headers=headers)).status_code == 401
        headers["Authorization"] = "Bearer " + bearer_token(settings)
        assert (await client.post(url, json=body, headers=headers)).status_code == 200
        for extra in [{"Origin": "https://evil.example"}, {"Host": "evil.example"}]:
            response = await client.post(url, json=body, headers={**headers, **extra})
            assert response.status_code in (403, 421)
        response = await client.post(
            url,
            content=json.dumps({**body, "padding": "a" * (130 * 1024)}),
            headers={**headers, "Content-Type": "application/json"},
        )
        assert response.status_code == 413


async def test_bounded_response(settings: Settings, package: Path) -> None:
    path = package / "SKILL.md"
    path.write_text(path.read_text() + ('"\\\n' * 40000))
    async with Client(create_server(settings), raise_exceptions=True) as client:
        result = await client.call_tool("skills_inspect", {"skill_id": "example"})
        assert len(result.model_dump_json().encode()) < 64 * 1024
        assert result.structured_content["result_truncated"]


async def test_invalid_arguments_are_also_bounded(settings: Settings) -> None:
    async with Client(create_server(settings), raise_exceptions=True) as client:
        result = await client.call_tool(
            "skills_update_prepare",
            {
                "request_id": "bad",
                "acquisition_id": "bad",
                "changes": [{"path": False, "action": 2}] * 1000,
                "human_instruction": "quote",
                "reason": "reason",
            },
        )
        assert result.is_error
        assert len(result.model_dump_json().encode()) < 64 * 1024
        missing = await client.call_tool(
            "skills_update_prepare", {"acquisition_id": "missing-quote"}
        )
        assert missing.is_error
