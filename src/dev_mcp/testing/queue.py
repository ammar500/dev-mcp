"""Strict FIFO tickets. Only an explicit finish or owner recovery releases a ticket."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dev_mcp.access import Access
from dev_mcp.tasks import store
from dev_mcp.tasks.common import TaskError, now, page, record_capacity, require, text
from dev_mcp.testing.repository import discover

if TYPE_CHECKING:
    from dev_mcp.tasks.service import TaskService

PENDING = ("waiting", "active", "recovery_required")
HOLDING = ("active", "recovery_required")
STATES = {*PENDING, "done", "cancelled", "reconciled"}


def databases(access: Access) -> Iterator[sqlite3.Connection]:
    """At most 32 databases; called under the deployment coordination lock."""
    for workspace in access.workspaces:
        path = access.root(workspace) / "tasks.sqlite3"
        if not path.exists():
            continue
        db = store.connect(path)
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            require(version in {1, store.SCHEMA_VERSION}, "Unsupported workspace database schema")
            if version == store.SCHEMA_VERSION:
                yield db
        finally:
            db.close()


def order(access: Access, repository: str) -> tuple[int, str | None]:
    maximum = 0
    head: tuple[int, str] | None = None
    for db in databases(access):
        maximum = max(
            maximum,
            db.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM testing_tickets WHERE repository=?",
                (repository,),
            ).fetchone()[0],
        )
        row = db.execute(
            "SELECT sequence,id FROM testing_tickets WHERE repository=? "
            "AND status IN ('waiting','active','recovery_required') ORDER BY sequence,id LIMIT 1",
            (repository,),
        ).fetchone()
        if row and (head is None or (row["sequence"], row["id"]) < head):
            head = (row["sequence"], row["id"])
    return maximum, head[1] if head else None


def repository_held(access: Access, repository: str) -> bool:
    # A restored holder must block even if another workspace now has an earlier sequence.
    for db in databases(access):
        if db.execute(
            "SELECT 1 FROM testing_tickets WHERE repository=? "
            "AND status IN ('active','recovery_required') LIMIT 1",
            (repository,),
        ).fetchone():
            return True
    return False


def owned(s: TaskService, db: sqlite3.Connection, ident: Any) -> dict[str, Any]:
    item = store.get(db, "testing_tickets", text(ident, "ticket_id", 100))
    require(item["owner"] == s.profile.id, "Ticket belongs to another profile", "forbidden")
    return item


def linked_attempt(
    s: TaskService, db: sqlite3.Connection, item: dict[str, Any]
) -> tuple[str, str] | None:
    if not item.get("attempt_id"):
        return None
    require(
        "tasks.write" in s.profile.permissions,
        "Linking an attempt requires tasks.write",
        "forbidden",
    )
    attempt = store.get(db, "attempts", text(item["attempt_id"], "attempt_id", 100))
    require(attempt["owner"] == s.profile.id, "Attempt belongs to another profile", "forbidden")
    require(attempt["task_id"] == item["task_id"], "Attempt must belong to the linked task")
    require(attempt["session_ref"] == item["session_ref"], "Attempt must belong to this session")
    require(
        attempt["checkout"] in {item["source_worktree"], item["testing_checkout"]},
        "Attempt must reserve this source worktree or testing checkout",
    )
    require(
        attempt["status"] == "running",
        "Linked attempt is no longer running; reconcile or finish it first",
        "conflict",
    )
    return s.profile.workspace, str(attempt["id"])


def join(s: TaskService, db: sqlite3.Connection, a: dict[str, Any]) -> dict[str, Any]:
    location = discover(a["source_worktree"])
    task_id = a.get("task_id")
    if task_id is not None:
        require(
            "tasks.read" in s.profile.permissions, "Linking a task requires tasks.read", "forbidden"
        )
        store.get(db, "tasks", text(task_id, "task_id", 100))
    item: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        **location,
        "owner": s.profile.id,
        "session_ref": text(a["session_ref"], "session_ref", 1024),
        "task_id": task_id,
        "attempt_id": a.get("attempt_id"),
        "additional_info": text(a.get("additional_info", ""), "additional_info", 4096, empty=True),
        "sequence": order(s.access, location["repository"])[0] + 1,
        "status": "waiting",
        "outcome": "",
        "created": now(),
        "updated": now(),
        "last_checked": None,
        "granted": None,
        "finished": None,
    }
    linked_attempt(s, db, item)
    record_capacity(item)
    db.execute(
        f"INSERT INTO testing_tickets({','.join(item)}) VALUES({','.join('?' for _ in item)})",
        tuple(item.values()),
    )
    store.event(db, item["id"], "testing.joined", s.profile.id)
    return {**item, "can_test": False, "poll_after_seconds": 30}


def check(s: TaskService, db: sqlite3.Connection, a: dict[str, Any]) -> dict[str, Any]:
    item = owned(s, db, a["ticket_id"])
    stamp = now()
    db.execute("UPDATE testing_tickets SET last_checked=? WHERE id=?", (stamp, item["id"]))
    item["last_checked"] = stamp
    reason: str | None = None
    detail: str | None = None
    if item["status"] == "waiting":
        if order(s.access, item["repository"])[1] != item["id"] or repository_held(
            s.access, item["repository"]
        ):
            reason = "unavailable"
        else:
            try:
                location = discover(item["source_worktree"])
                require(
                    all(item[k] == v for k, v in location.items()),
                    "Repository layout changed; withdraw and enroll again",
                    "repository_unavailable",
                )
                ignore = linked_attempt(s, db, item)
                store.reserve(
                    s.access, item["testing_checkout"], worker=False, ignore_attempt=ignore
                )
            except TaskError as exc:
                reason = "unavailable" if exc.code == "checkout_busy" else exc.code
                if exc.code != "checkout_busy":
                    detail = str(exc)
            else:
                db.execute(
                    "UPDATE testing_tickets SET status='active',granted=?,updated=? WHERE id=?",
                    (stamp, stamp, item["id"]),
                )
                store.event(db, item["id"], "testing.granted", s.profile.id)
                item.update(status="active", granted=stamp, updated=stamp)
    elif item["status"] == "recovery_required":
        reason = "owner_recovery_required"
    return {
        **item,
        "can_test": item["status"] == "active",
        "waiting_reason": reason,
        "waiting_detail": detail,
        "poll_after_seconds": 30 if item["status"] == "waiting" else None,
        "guidance": "Poll only while waiting. When active, stop the stack and finish cleanup "
        "before testing_done. No heartbeat or automatic expiry.",
    }


def done(s: TaskService, db: sqlite3.Connection, a: dict[str, Any]) -> dict[str, Any]:
    item = owned(s, db, a["ticket_id"])
    require(
        item["status"] != "recovery_required",
        "Restored holder needs explicit owner recovery",
        "conflict",
    )
    if item["status"] in {"waiting", "active"}:
        if item["status"] == "active":
            require(
                a.get("stack_stopped") is True and a.get("cleanup_complete") is True,
                "Stop the stack and confirm cleanup_complete before releasing the slot",
                "cleanup_required",
            )
        status = "done" if item["status"] == "active" else "cancelled"
        outcome = text(a.get("outcome", ""), "outcome", 4096, empty=True)
        stamp = now()
        db.execute(
            "UPDATE testing_tickets SET status=?,outcome=?,finished=?,updated=? WHERE id=?",
            (status, outcome, stamp, stamp, item["id"]),
        )
        store.event(
            db,
            item["id"],
            "testing." + status,
            s.profile.id,
            {
                "stack_stopped": a.get("stack_stopped", False),
                "cleanup_complete": a.get("cleanup_complete", False),
                "outcome": outcome,
            },
        )
        item = store.get(db, "testing_tickets", item["id"])
    return {**item, "can_test": False, "poll_after_seconds": None}


def listing(s: TaskService, db: sqlite3.Connection, a: dict[str, Any]) -> dict[str, Any]:
    if a.get("ticket_id") is not None:
        # Reading never grants permission. Only testing_check can return can_test=true.
        return store.get(db, "testing_tickets", text(a["ticket_id"], "ticket_id", 100))
    offset, limit = page(a)
    where, values = [], []
    status = a.get("status", "pending")
    if status == "pending":
        where.append("status IN ('waiting','active','recovery_required')")
    elif status != "all":
        require(status in STATES, "Invalid testing status")
        where.append("status=?")
        values.append(status)
    for name in ("repository", "source_worktree", "session_ref"):
        if a.get(name) is not None:
            value = text(a[name], name, 4096 if name != "session_ref" else 1024)
            if name != "session_ref":
                require(Path(value).is_absolute(), f"{name} must be an absolute path")
                value = str(Path(value).resolve())
            where.append(name + "=?")
            values.append(value)
    return s.listing(
        db.execute(
            "SELECT * FROM testing_tickets WHERE "
            + (" AND ".join(where) or "1")
            + " ORDER BY sequence,id LIMIT ? OFFSET ?",
            (*values, limit + 1, offset),
        ),
        offset,
        limit,
    )
