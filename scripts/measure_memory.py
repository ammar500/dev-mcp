"""Reproducible Linux RSS benchmark; creates only temporary state and sample packages."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import httpx2
from mcp import StdioServerParameters
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from dev_mcp.config import Settings, bearer_token


def rss(pid: int) -> dict[str, float]:
    rows = Path(f"/proc/{pid}/status").read_text().splitlines()
    return {
        line.split(":")[0]: round(int(line.split()[1]) / 1024, 2)
        for line in rows
        if line.startswith(("VmRSS:", "VmHWM:"))
    }


async def call(connection: Client, name: str, **args: Any) -> dict[str, Any]:
    if name in ("session_open", "acquire", "update_prepare", "update_apply"):
        args["request_id"] = str(uuid.uuid4())
    result = await connection.call_tool("skills_" + name, args)
    if result.is_error:
        raise RuntimeError(result.content)
    return dict(result.structured_content or {})


async def measure(root: Path) -> dict[str, Any]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings = Settings(
        library=root / "library", state=root / "state", config=root / "config", port=port
    )
    settings.library.mkdir()
    settings.save()
    token = bearer_token(settings)
    env = {
        **os.environ,
        "DEV_MCP_CONFIG": str(settings.config),
        "DEV_MCP_STATE": str(settings.state),
        "DEV_MCP_LIBRARY": str(settings.library),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "dev_mcp", "serve"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    report: dict[str, Any] = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "limits": {k: v for k, v in dataclasses.asdict(settings).items() if isinstance(v, int)},
        "units": "MiB from Linux /proc RSS/HWM counters; peak sampled every 20 ms",
        "samples": {},
    }
    samples = report["samples"]
    peak = {"VmRSS": 0.0, "VmHWM": 0.0}
    stop_sampling = asyncio.Event()

    async def sample_peak() -> None:
        while not stop_sampling.is_set():
            for key, value in rss(process.pid).items():
                peak[key] = max(peak[key], value)
            await asyncio.sleep(0.02)

    sampler = asyncio.create_task(sample_peak())
    try:
        for _ in range(200):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                await asyncio.sleep(0.025)
        await asyncio.sleep(0.3)
        samples["empty_service_idle"] = rss(process.pid)
        for name, size in (("small", 4096), ("large", 128 * 1024 * 1024)):
            package = settings.library / name
            package.mkdir()
            (package / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: Memory benchmark\n---\nUse assets.\n"
            )
            with (package / "asset.bin").open("wb") as output:
                chunk = bytes(min(size, 1024 * 1024))
                for _ in range(size // len(chunk)):
                    output.write(chunk)
        async with AsyncExitStack() as stack:
            clients = []
            for _ in range(2):
                http = await stack.enter_async_context(
                    httpx2.AsyncClient(headers={"Authorization": "Bearer " + token})
                )
                clients.append(
                    await stack.enter_async_context(
                        Client(
                            streamable_http_client(
                                f"http://127.0.0.1:{port}/mcp", http_client=http
                            ),
                            cache=None,
                        )
                    )
                )
            await asyncio.gather(*(call(c, "search") for c in clients))
            samples["warmed_two_clients_idle"] = rss(process.pid)

            async def cycle(client: Client, size: str, index: int) -> None:
                checkout = root / f"checkout-{index}"
                checkout.mkdir(exist_ok=True)
                session = await call(
                    client,
                    "session_open",
                    client=f"benchmark-{index}",
                    checkout=str(checkout),
                    task=size,
                )
                acquisition = await call(
                    client, "acquire", session_id=session["session_id"], skill_id=size
                )
                # Read instructions as a consuming agent would; never load the binary.
                Path(acquisition["skill_path"]).read_text()
                await call(client, "release", session_id=session["session_id"], close_session=True)

            for count in (10, 50, 100):
                start = 0 if count == 10 else 10 if count == 50 else 50
                for _ in range(start, count, 2):
                    await asyncio.gather(*(cycle(c, "small", i) for i, c in enumerate(clients)))
                samples[f"after_{count}_small_sessions"] = rss(process.pid)
            before = time.monotonic()
            await asyncio.gather(*(cycle(c, "large", i) for i, c in enumerate(clients)))
            report["two_large_cycles_seconds"] = round(time.monotonic() - before, 2)
            samples["after_two_128MiB_packages"] = rss(process.pid)
            session = await call(
                clients[0],
                "session_open",
                client="benchmark-publisher",
                checkout=str(root / "checkout-0"),
                task="large update",
            )
            acquired = await call(
                clients[0], "acquire", session_id=session["session_id"], skill_id="large"
            )
            with (Path(acquired["directory"]) / "asset.bin").open("r+b") as edited:
                edited.write(b"x")
            prepared = await call(
                clients[0],
                "update_prepare",
                acquisition_id=acquired["acquisition_id"],
                changes=[{"path": "asset.bin", "action": "replace"}],
                human_instruction="Update this disposable benchmark skill asset.",
                reason="Memory measurement",
            )
            await call(clients[0], "update_apply", proposal_id=prepared["proposal_id"])
            await call(clients[0], "release", session_id=session["session_id"], close_session=True)
            samples["after_128MiB_binary_publication"] = rss(process.pid)
            for _ in range(20):
                await asyncio.gather(*(cycle(c, "small", i) for i, c in enumerate(clients)))
            samples["after_40_more_small_sessions"] = rss(process.pid)
            # Separate stdio processes deliberately share the same durable state.
            pids_before = set(
                Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children").read_text().split()
            )
            for _ in range(2):
                c = await stack.enter_async_context(
                    Client(
                        StdioServerParameters(
                            command=sys.executable, args=["-m", "dev_mcp"], env=env
                        ),
                        mode="legacy",
                        cache=None,
                    )
                )
                await call(c, "search")
            pids_after = set(
                Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children").read_text().split()
            )
            report["two_stdio_compatibility_processes"] = [
                rss(int(pid)) for pid in sorted(pids_after - pids_before)
            ]
            samples["http_with_two_stdio_processes"] = rss(process.pid)
            report["client_benchmark_process"] = rss(os.getpid())
        await asyncio.sleep(0.3)
        samples["after_clients_disconnect"] = rss(process.pid)
        report["remaining_package_bytes"] = sum(
            p.stat().st_size for p in (settings.state / "acquisitions").rglob("*") if p.is_file()
        )
        report["consumer_checkouts_unchanged"] = all(
            not list(p.iterdir()) for p in root.glob("checkout-*")
        )
        report["peak_sampled_during_operations"] = peak
        return report
    finally:
        stop_sampling.set()
        await sampler
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="dev-mcp-memory-") as temp:
        result = asyncio.run(measure(Path(temp)))
    print(json.dumps(result, indent=2))
