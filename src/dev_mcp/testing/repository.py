"""Bounded read-only Git metadata discovery, without inherited Git overrides."""

from __future__ import annotations

import os
import selectors
import subprocess
import time
from pathlib import Path

from dev_mcp.tasks.common import TaskError, checkout, require


def git(path: str, *arguments: str) -> bytes:
    # No shell, user config, hooks, pagers, network, or checkout writes. Do not inherit
    # GIT_DIR/GIT_WORK_TREE from the MCP process's own launch environment.
    env = {
        "PATH": os.defpath,
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        with subprocess.Popen(
            ["git", "--no-optional-locks", "-C", path, *arguments],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as proc:
            assert proc.stdout is not None
            result = bytearray()
            deadline = time.monotonic() + 3
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(proc.stdout, selectors.EVENT_READ)
                    while True:
                        remaining = deadline - time.monotonic()
                        require(
                            remaining > 0, "Git metadata lookup timed out", "repository_unavailable"
                        )
                        if not selector.select(remaining):
                            continue
                        chunk = os.read(proc.stdout.fileno(), 4096)
                        if not chunk:
                            break
                        result.extend(chunk)
                        require(len(result) <= 65536, "Git metadata exceeds 64 KiB")
                require(
                    proc.wait(timeout=max(0.01, deadline - time.monotonic())) == 0,
                    "Cannot read Git metadata; check repository access",
                    "repository_unavailable",
                )
                return bytes(result)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TaskError("repository_unavailable", "Git metadata lookup failed") from exc


def identity(path: str) -> tuple[str, str, str]:
    raw = git(
        path,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
        "--show-toplevel",
        "--absolute-git-dir",
    )
    parts = os.fsdecode(raw).splitlines()
    require(len(parts) == 3, "Only non-bare local repositories with plain paths are supported")
    common, top, git_dir = (str(Path(p).resolve()) for p in parts)
    require(Path(common).is_dir() and Path(top).is_dir(), "Repository directories are unavailable")
    return common, top, git_dir


def discover(value: object) -> dict[str, str]:
    source = checkout(value)
    require(
        source is not None and "\n" not in source and "\r" not in source, "Invalid worktree path"
    )
    assert source is not None
    common, top, git_dir = identity(source)
    require(top == source, "source_worktree must be the checkout root")
    # The primary checkout uses the common Git directory directly. Git's worktree
    # list can misidentify it for --separate-git-dir without core.worktree configured.
    main = source
    if git_dir != common:
        record = git(source, "worktree", "list", "--porcelain", "-z").split(b"\x00\x00", 1)[0]
        fields = record.split(b"\x00")
        require(
            fields[0].startswith(b"worktree ") and b"bare" not in fields,
            "Bare repositories are unsupported",
        )
        main = str(Path(os.fsdecode(fields[0][9:])).resolve())
        require(
            identity(main) == (common, main, common),
            "Cannot validate the primary checkout; check Git worktree metadata",
        )
    return {"repository": common, "source_worktree": source, "testing_checkout": main}
