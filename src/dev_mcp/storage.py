"""Disk-backed records and bounded cross-process admission for shared/stdio clients."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from dev_mcp.config import Settings
from dev_mcp.skills.files import SkillError

T = TypeVar("T")

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
 operation TEXT NOT NULL, request_id TEXT NOT NULL, arguments TEXT NOT NULL,
 object_id TEXT NOT NULL, result TEXT, PRIMARY KEY(operation, request_id));
CREATE TABLE IF NOT EXISTS sessions (
 id TEXT PRIMARY KEY, client TEXT NOT NULL, checkout TEXT NOT NULL, task TEXT NOT NULL,
 task_ref TEXT, created TEXT NOT NULL, status TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS session_lookup ON sessions(status, checkout, client, task);
CREATE TABLE IF NOT EXISTS acquisitions (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), skill_id TEXT NOT NULL,
 revision TEXT, directory TEXT NOT NULL, manifest TEXT, baseline TEXT, status TEXT NOT NULL,
 created TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS acquisition_session ON acquisitions(session_id, status);
CREATE TABLE IF NOT EXISTS proposals (
 id TEXT PRIMARY KEY, acquisition_id TEXT NOT NULL REFERENCES acquisitions(id),
 skill_id TEXT NOT NULL, before_revision TEXT NOT NULL, after_revision TEXT,
 instruction TEXT NOT NULL, reason TEXT NOT NULL, changes TEXT NOT NULL,
 manifest TEXT, status TEXT NOT NULL, created TEXT NOT NULL, receipt TEXT);
CREATE TABLE IF NOT EXISTS audit (
 id TEXT PRIMARY KEY, created TEXT NOT NULL, kind TEXT NOT NULL, details TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS catalogue (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, compatibility TEXT);
CREATE TABLE IF NOT EXISTS recovery (
 id TEXT PRIMARY KEY, skill_id TEXT NOT NULL, details TEXT NOT NULL);
PRAGMA user_version=1;
"""


@contextmanager
def connect(settings: Settings) -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(settings.state / "state.sqlite3", timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    try:
        with db:
            yield db
    finally:
        db.close()


@contextmanager
def exclusive(settings: Settings) -> Iterator[None]:
    fd = os.open(settings.state / "locks/state", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def initialize(settings: Settings) -> None:
    settings.initialize()
    with exclusive(settings), connect(settings) as db:
        db.execute("PRAGMA journal_mode=WAL")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise ValueError(f"Unsupported state database version: {version}")
        db.executescript(SCHEMA)
    (settings.state / "state.sqlite3").chmod(0o600)


def dump(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)


class Operations:
    """Two threads, a bounded queue, and shared admission slots across all processes.

    SQLite/filesystem mutations are serialized separately by `exclusive`. Slots also
    bound readers and compatibility processes. No package bytes cross the executor.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.executor = ThreadPoolExecutor(
            max_workers=settings.file_workers, thread_name_prefix="files"
        )
        self.pending = 0
        self.guard = threading.Lock()

    def slot(self, prefix: str, count: int) -> int | None:
        for index in range(count):
            path = self.settings.state / "locks" / f"{prefix}-{index}"
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                os.close(fd)
        return None

    async def run(self, fn: Callable[[], T]) -> T:
        total = self.settings.file_workers + self.settings.waiting_operations
        with self.guard:
            if self.pending >= total:
                raise SkillError(
                    "capacity", "Operation queue is full; retry after current work finishes"
                )
            admission = self.slot("admission", total)
            if admission is None:
                raise SkillError("capacity", "Shared operation queue is full; retry later")
            self.pending += 1

        def work() -> T:
            worker = None
            try:
                while worker is None:
                    worker = self.slot("worker", self.settings.file_workers)
                    if worker is None:
                        time.sleep(0.05)
                return fn()
            finally:
                if worker is not None:
                    os.close(worker)
                os.close(admission)
                with self.guard:
                    self.pending -= 1

        # Shield preserves the admission reservation until durable work actually finishes.
        return await asyncio.shield(asyncio.get_running_loop().run_in_executor(self.executor, work))

    def close(self) -> None:
        self.executor.shutdown(wait=True)


def disk_usage(root: Path) -> int:
    total = 0
    for directory, _, filenames in os.walk(root, followlinks=False):
        for filename in filenames:
            total += (Path(directory) / filename).lstat().st_size
    return total


def capacity(settings: Settings, additional: int) -> None:
    used = sum(
        disk_usage(settings.state / name) for name in ("acquisitions", "proposals", "transactions")
    )
    if used + additional > settings.storage_bytes:
        raise SkillError(
            "capacity",
            "Managed package storage is full; release copies or resolve "
            "unfinished work before retrying",
            used_bytes=used,
            requested_bytes=additional,
            limit_bytes=settings.storage_bytes,
        )
