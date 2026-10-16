"""Short SQLite transactions and a coordination lock independent of skill I/O."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from dev_mcp.access import Access
from dev_mcp.config import private_directory
from dev_mcp.tasks.common import TaskError, dump, now, require

SCHEMA = """
CREATE TABLE tasks (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, instructions TEXT NOT NULL,
 status TEXT NOT NULL, priority INTEGER NOT NULL, due TEXT, checkout TEXT,
 context TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0,
 version INTEGER NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE INDEX tasks_list ON tasks(archived,status,priority,created,id);
CREATE INDEX tasks_due ON tasks(due);
CREATE TABLE reminders (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), remind_at TEXT NOT NULL,
 timezone TEXT NOT NULL, note TEXT NOT NULL, status TEXT NOT NULL,
 version INTEGER NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE INDEX reminders_due ON reminders(status,remind_at,id);
CREATE INDEX reminders_task ON reminders(task_id,status);
CREATE TABLE pauses (
 id TEXT PRIMARY KEY, raw_text TEXT NOT NULL, task_id TEXT REFERENCES tasks(id),
 checkout TEXT, session_ref TEXT, supersedes TEXT REFERENCES pauses(id),
 resumed INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
 version INTEGER NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE INDEX pauses_find ON pauses(archived,resumed,created,id);
CREATE INDEX pauses_supersedes ON pauses(supersedes,archived);
CREATE INDEX pauses_context ON pauses(task_id,checkout,session_ref);
CREATE TABLE delegations (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), task_version INTEGER NOT NULL,
 snapshot TEXT NOT NULL, execution_profile TEXT NOT NULL, not_before TEXT NOT NULL,
 priority INTEGER NOT NULL, status TEXT NOT NULL, version INTEGER NOT NULL,
 created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE INDEX queue ON delegations(status,priority,created,id);
CREATE UNIQUE INDEX one_delegation ON delegations(task_id)
 WHERE status IN ('queued','running','cancel_requested','uncertain');
CREATE TABLE attempts (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
 delegation_id TEXT REFERENCES delegations(id), owner TEXT NOT NULL, mode TEXT NOT NULL,
 snapshot TEXT NOT NULL, checkout TEXT, status TEXT NOT NULL, version INTEGER NOT NULL,
 session_ref TEXT, result TEXT, heartbeat TEXT NOT NULL, expires TEXT NOT NULL,
 created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE UNIQUE INDEX one_attempt ON attempts(task_id)
 WHERE status IN ('running','cancel_requested','uncertain');
CREATE INDEX active_attempts ON attempts(status,checkout,mode);
CREATE TABLE events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, entity_id TEXT NOT NULL,
 kind TEXT NOT NULL, actor TEXT NOT NULL, created TEXT NOT NULL, details TEXT NOT NULL);
CREATE INDEX event_entity ON events(entity_id,sequence);
CREATE TABLE context_assets (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT,
 status TEXT NOT NULL, created TEXT NOT NULL, identity TEXT);
CREATE TABLE requests (
 profile TEXT NOT NULL, operation TEXT NOT NULL, request_id TEXT NOT NULL,
 arguments TEXT NOT NULL, result TEXT NOT NULL,
 PRIMARY KEY(profile,operation,request_id));
PRAGMA user_version=1;
"""
ACTIVE = ("running", "cancel_requested", "uncertain")


@contextmanager
def coordination(access: Access) -> Iterator[None]:
    private_directory(access.settings.state)
    lock = access.settings.state / "tasks.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def connect(path: Path) -> sqlite3.Connection:
    require(not path.is_symlink(), "Database path must not be a symlink")
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA synchronous=FULL")
    return db


def initialize(access: Access, workspace: str) -> Path:
    root = access.root(workspace)
    private_directory(root)
    private_directory(root / "context")
    path = root / "tasks.sqlite3"
    with coordination(access):
        db = connect(path)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")
            elif version != 1:
                raise ValueError("Unsupported tasks database version")
        finally:
            db.close()
        path.chmod(0o600)
    return path


def get(db: sqlite3.Connection, table: str, ident: str) -> dict[str, Any]:
    # Only internal constant table names are passed here.
    row = db.execute(f"SELECT * FROM {table} WHERE id=?", (ident,)).fetchone()
    require(row is not None, "Record not found in this workspace", "not_found")
    return decode(dict(row))


def decode(item: dict[str, Any]) -> dict[str, Any]:
    for key in ("context", "snapshot", "details", "result"):
        if key in item and item[key] is not None:
            item[key] = json.loads(item[key])
    for key in ("archived", "resumed"):
        if key in item:
            item[key] = bool(item[key])
    return item


def version(item: dict[str, Any], expected: Any) -> None:
    require(
        type(expected) is int and item["version"] == expected,
        "Version conflict; retrieve the record and retry with a new request ID",
        "conflict",
    )


def update(db: sqlite3.Connection, table: str, ident: str, fields: dict[str, Any]) -> None:
    fields = {**fields, "updated": now()}
    assignments = ",".join(f"{key}=?" for key in fields)
    db.execute(
        f"UPDATE {table} SET {assignments},version=version+1 WHERE id=?", (*fields.values(), ident)
    )


def event(
    db: sqlite3.Connection,
    entity: str,
    kind: str,
    actor: str,
    details: dict[str, Any] | None = None,
) -> None:
    db.execute(
        "INSERT INTO events(entity_id,kind,actor,created,details) VALUES(?,?,?,?,?)",
        (entity, kind, actor, now(), dump(details or {})),
    )


def active(db: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT * FROM attempts WHERE task_id=? AND status IN (?,?,?)", (task_id, *ACTIVE)
    ).fetchone()
    return decode(dict(row)) if row else None


def no_execution(db: sqlite3.Connection, task_id: str) -> None:
    require(
        active(db, task_id) is None,
        "An execution owns this task; report or reconcile it first",
        "busy",
    )
    row = db.execute(
        "SELECT id FROM delegations WHERE task_id=? AND status='queued'", (task_id,)
    ).fetchone()
    require(row is None, "Cancel queued delegation before changing this task", "busy")


def cancel_reminders(db: sqlite3.Connection, task_id: str) -> None:
    db.execute(
        "UPDATE reminders SET status='cancelled',version=version+1,updated=? "
        "WHERE task_id=? AND status='pending'",
        (now(), task_id),
    )


def expire(db: sqlite3.Connection) -> None:
    # No requeue: late/lost workers may still be modifying files.
    db.execute(
        "INSERT INTO events(entity_id,kind,actor,created,details) "
        "SELECT task_id,'execution.uncertain','server',?,json_object('attempt_id',id) "
        "FROM attempts WHERE expires<? AND status IN ('running','cancel_requested')",
        (now(), now()),
    )
    db.execute(
        "UPDATE delegations SET status='uncertain',version=version+1,updated=? "
        "WHERE id IN (SELECT delegation_id FROM attempts WHERE expires<? "
        "AND status IN ('running','cancel_requested'))",
        (now(), now()),
    )
    db.execute(
        "UPDATE attempts SET status='uncertain',version=version+1,updated=? "
        "WHERE expires<? AND status IN ('running','cancel_requested')",
        (now(), now()),
    )


def reservations(access: Access) -> Iterator[dict[str, Any]]:
    """Called only while holding coordination; never expose other workspace rows."""
    for workspace in access.workspaces:
        path = access.root(workspace) / "tasks.sqlite3"
        if not path.exists():
            continue
        db = connect(path)
        try:
            for row in db.execute(
                "SELECT checkout,mode FROM attempts WHERE status IN (?,?,?)", ACTIVE
            ):
                yield dict(row)
        finally:
            db.close()


def reserve(access: Access, checkout: str | None, *, worker: bool) -> None:
    for row in reservations(access):
        if worker and row["mode"] == "worker":
            raise TaskError("busy", "The deployment's worker execution slot is occupied")
        if checkout and row["checkout"]:
            a, b = Path(checkout), Path(row["checkout"])
            require(
                a != b and a not in b.parents and b not in a.parents,
                "Checkout overlaps an active or uncertain execution; use another worktree",
                "checkout_busy",
            )
