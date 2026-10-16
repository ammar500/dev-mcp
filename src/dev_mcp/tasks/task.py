"""Task CRUD and lifecycle; ownership is implemented separately in execution."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from dev_mcp.tasks import context as managed_context
from dev_mcp.tasks import store
from dev_mcp.tasks.common import (
    changed_fields,
    checkout,
    due,
    dump,
    now,
    page,
    record_capacity,
    require,
    text,
)

if TYPE_CHECKING:
    from dev_mcp.tasks.service import TaskService

STATUSES = {"open", "in_progress", "blocked", "done", "cancelled"}


def context(value: Any, db: sqlite3.Connection) -> list[dict[str, Any]]:
    require(isinstance(value, list) and len(value) <= 32, "context must contain at most 32 entries")
    require(len(json.dumps(value).encode()) <= 8192, "Inline context exceeds 8 KiB; use references")
    for entry in value:
        require(
            isinstance(entry, dict) and set(entry) <= {"kind", "value", "label"},
            "Context entries use kind, value and optional label",
        )
        require(
            entry.get("kind")
            in {"snapshot", "managed", "file", "url", "skill", "retrieval", "credential_ref"},
            "Unknown context kind; use snapshot/managed/file/url/skill/retrieval/credential_ref",
        )
        text(entry.get("value"), "context value")
        if entry["kind"] == "managed":
            managed_context.asset(db, entry["value"])
        if "label" in entry:
            text(entry["label"], "context label", 256)
    return list(value)


def priority(value: Any) -> int:
    require(type(value) is int and 0 <= value <= 9, "priority must be an integer from 0 to 9")
    return int(value)


def summary(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if k not in {"instructions", "context", "snapshot"}}


def create(s: TaskService, db: sqlite3.Connection, a: dict[str, Any]) -> dict[str, Any]:
    ident, stamp = str(uuid.uuid4()), now()
    fields = (
        ident,
        text(a["title"], "title", 512),
        text(a.get("instructions", ""), "instructions", empty=True),
        "open",
        priority(a.get("priority", 0)),
        due(a.get("due"), s.workspace.timezone),
        checkout(a.get("checkout")),
        dump(context(a.get("context", []), db)),
        1,
        stamp,
        stamp,
    )
    db.execute(
        "INSERT INTO tasks(id,title,instructions,status,priority,due,checkout,context,"
        "version,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        fields,
    )
    store.event(db, ident, "task.created", s.profile.id)
    record_capacity(store.get(db, "tasks", ident))
    return summary(store.get(db, "tasks", ident))


def listing(s: TaskService, db: sqlite3.Connection, a: dict[str, Any]) -> dict[str, Any]:
    conditions, params = ["1=1"], []
    if not a.get("include_archived", False):
        conditions.append("archived=0")
    if a.get("status"):
        require(a["status"] in STATUSES, "Invalid task status")
        conditions.append("status=?")
        params.append(a["status"])
    if a.get("query"):
        conditions.append(
            "(instr(lower(title),lower(?))>0 OR instr(lower(instructions),lower(?))>0)"
        )
        params.extend([text(a["query"], "query", 512)] * 2)
    if a.get("checkout"):
        conditions.append("checkout=?")
        params.append(checkout(a["checkout"]))
    if a.get("due_before"):
        cutoff = due(a["due_before"], s.workspace.timezone)
        assert cutoff is not None
        if len(cutoff) == 10:
            # A date filter includes the entire named local day, including timed due dates.
            end = (
                datetime.combine(
                    date.fromisoformat(cutoff) + timedelta(days=1),
                    time.min,
                    ZoneInfo(s.workspace.timezone),
                )
                .astimezone(UTC)
                .isoformat(timespec="microseconds")
            )
            conditions.append("((length(due)=10 AND due<=?) OR (length(due)>10 AND due<?))")
            params.extend([cutoff, end])
        else:
            conditions.append("due IS NOT NULL AND due<=?")
            params.append(cutoff)
    offset, limit = page(a)
    cursor = db.execute(
        "SELECT id,title,status,priority,due,checkout,archived,version,created,updated "
        "FROM tasks WHERE "
        + " AND ".join(conditions)
        + " ORDER BY priority DESC,created,id LIMIT ? OFFSET ?",
        (*params, limit + 1, offset),
    )
    return s.listing(cursor, offset, limit)


def get(s: TaskService, db: sqlite3.Connection, a: dict[str, Any]) -> dict[str, Any]:
    task = store.get(db, "tasks", a["task_id"])
    view = a.get("view", "task")
    if view == "context":
        index = a.get("context_index", 0)
        require(type(index) is int and 0 <= index < len(task["context"]), "Invalid context_index")
        entry = task["context"][index]
        require(entry["kind"] == "managed", "Only managed context has server-owned contents")
        return managed_context.read(
            s.access, s.profile.workspace, db, entry["value"], a.get("offset", 0)
        )
    if view == "task":
        ownership = store.active(db, task["id"])
        return {
            **task,
            "execution": summary({k: v for k, v in ownership.items() if k != "result"})
            if ownership
            else None,
        }
    views = {
        "history": ("events", "entity_id", "sequence", "*"),
        "attempts": (
            "attempts",
            "task_id",
            "created,id",
            "id,task_id,delegation_id,owner,mode,"
            "status,version,session_ref,result,heartbeat,expires,created,updated",
        ),
        "delegations": (
            "delegations",
            "task_id",
            "created,id",
            "id,task_id,task_version,"
            "execution_profile,not_before,priority,status,version,created,updated",
        ),
    }
    require(view in views, "view must be task/context/history/attempts/delegations")
    table, key, order, columns = views[view]
    offset, limit = page(a)
    return s.listing(
        db.execute(
            f"SELECT {columns} FROM {table} WHERE {key}=? ORDER BY {order} LIMIT ? OFFSET ?",
            (task["id"], limit + 1, offset),
        ),
        offset,
        limit,
    )


def update(s: TaskService, db: sqlite3.Connection, a: dict[str, Any]) -> dict[str, Any]:
    item = store.get(db, "tasks", a["task_id"])
    store.version(item, a["expected_version"])
    store.no_execution(db, item["id"])
    patch = changed_fields(
        a["patch"], {"title", "instructions", "priority", "due", "checkout", "context"}
    )
    fields: dict[str, Any] = {}
    for key, value in patch.items():
        if key == "title":
            fields[key] = text(value, key, 512)
        elif key == "instructions":
            fields[key] = text(value, key, empty=True)
        elif key == "priority":
            fields[key] = priority(value)
        elif key == "due":
            fields[key] = due(value, s.workspace.timezone)
        elif key == "checkout":
            fields[key] = checkout(value)
        else:
            fields[key] = dump(context(value, db))
    store.update(db, "tasks", item["id"], fields)
    record_capacity(store.get(db, "tasks", item["id"]))
    store.event(db, item["id"], "task.updated", s.profile.id, {"fields": sorted(fields)})
    return summary(store.get(db, "tasks", item["id"]))


def transition(s: TaskService, db: sqlite3.Connection, a: dict[str, Any]) -> dict[str, Any]:
    item = store.get(db, "tasks", a["task_id"])
    store.version(item, a["expected_version"])
    store.no_execution(db, item["id"])
    require(not item["archived"], "Restore the task before changing its status")
    states = {"complete": "done", "cancel": "cancelled", "block": "blocked", "reopen": "open"}
    require(
        a["action"] in states,
        "action must be complete/cancel/block/reopen; use tasks_claim to start",
    )
    state = states[a["action"]]
    require(item["status"] != state, "Task is already in this state", "conflict")
    if item["status"] in {"done", "cancelled"}:
        require(state == "open", "Reopen a finished task first")
    store.update(db, "tasks", item["id"], {"status": state})
    if state in {"done", "cancelled"}:
        store.cancel_reminders(db, item["id"])
    store.event(db, item["id"], "task." + a["action"], s.profile.id)
    return summary(store.get(db, "tasks", item["id"]))


def archive(s: TaskService, db: sqlite3.Connection, a: dict[str, Any]) -> dict[str, Any]:
    a = {"archived": True, **a}
    item = store.get(db, "tasks", a["task_id"])
    store.version(item, a["expected_version"])
    store.no_execution(db, item["id"])
    require(type(a["archived"]) is bool, "archived must be boolean")
    store.update(db, "tasks", item["id"], {"archived": a["archived"]})
    if a["archived"]:
        store.cancel_reminders(db, item["id"])
    store.event(db, item["id"], "task.archived" if a["archived"] else "task.restored", s.profile.id)
    return summary(store.get(db, "tasks", item["id"]))
