from __future__ import annotations

import asyncio
import dataclasses
import os
import threading
from pathlib import Path

import pytest

from dev_mcp.cli import systemd_unit
from dev_mcp.config import Settings, bearer_token
from dev_mcp.skills.files import SkillError
from dev_mcp.storage import Operations


def test_private_credentials_and_unit(settings: Settings) -> None:
    token = bearer_token(settings)
    assert token == bearer_token(settings)
    assert (settings.config / "token").stat().st_mode & 0o777 == 0o600
    assert settings.state.stat().st_mode & 0o777 == 0o700
    text = systemd_unit(settings)
    assert "WantedBy=default.target" in text and "Restart=on-failure" in text
    assert "linger" not in text and token not in text
    assert "After=default.target" not in text  # avoid a default.target ordering cycle
    (settings.config / "token").chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        bearer_token(settings)


def test_reject_runtime_in_checkout(settings: Settings, tmp_path: Path) -> None:
    repo = tmp_path / "consumer"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: /some/worktree")
    with pytest.raises(ValueError, match="outside Git"):
        dataclasses.replace(settings, state=repo / "state").initialize()


async def test_bounded_queue_including_cancelled_clients(settings: Settings) -> None:
    settings = dataclasses.replace(settings, file_workers=1, waiting_operations=1)
    settings.initialize()
    first, second = Operations(settings), Operations(settings)
    started, unblock = threading.Event(), threading.Event()

    def busy() -> int:
        started.set()
        assert unblock.wait(timeout=5)
        return 1

    one = asyncio.create_task(first.run(busy))
    while not started.is_set():
        await asyncio.sleep(0.001)
    two = asyncio.create_task(first.run(lambda: 2))
    await asyncio.sleep(0.01)
    with pytest.raises(SkillError, match="queue is full"):
        await first.run(lambda: 3)
    with pytest.raises(SkillError, match="queue is full"):
        await second.run(lambda: 3)
    one.cancel()
    with pytest.raises(asyncio.CancelledError):
        await one
    with pytest.raises(SkillError, match="queue is full"):
        await second.run(lambda: 3)
    unblock.set()
    assert await two == 2
    assert await second.run(lambda: 3) == 3
    first.close()
    second.close()


def test_paths_outside_git_are_independent_of_cwd(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.save()
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setenv("DEV_MCP_CONFIG", str(settings.config))
    monkeypatch.chdir(checkout)
    assert Settings.load() == settings
    assert not list(checkout.iterdir())
    assert os.getuid() == (settings.config / "settings.json").stat().st_uid
