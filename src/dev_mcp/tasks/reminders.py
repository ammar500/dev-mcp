"""Queryable reminder dates; no timers or notification delivery."""

from __future__ import annotations

import sqlite3
import uuid
from typing import TYPE_CHECKING, Any

from dev_mcp.tasks import store
from dev_mcp.tasks.common import changed_fields, instant, now, page, require, text

if TYPE_CHECKING:
    from dev_mcp.tasks.service import TaskService


def eligible(db: sqlite3.Connection, task_id: str) -> None:
    task = store.get(db, "tasks", task_id)
    require(
        not task["archived"] and task["status"] not in {"done", "cancelled"},
        "Pending reminders need an open, unarchived task",
    )


def create(s: TaskService, db: sqlite3.Connection, a: dict[str, Any]) -> dict[str, Any]:
    eligible(db, a["task_id"])
    zone = a.get("timezone") or s.workspace.timezone
    remind = instant(a["remind_at"], zone, allow_past=a.get("allow_past", False))
    ident, stamp = str(uuid.uuid4()), now()
    db.execute(
        "INSERT INTO reminders VALUES(?,?,?,?,?,?,?,?,?)",
        (
            ident,
            a["task_id"],
            remind,
            zone,
            text(a.get("note", ""), "note", 2048, empty=True),
            "pending",
            1,
            stamp,
            stamp,
        ),
    )
    store.event(db, a["task_id"], "reminder.created", s.profile.id, {"reminder_id": ident})
    return store.get(db, "reminders", ident)


def listing(s: TaskService, db: sqlite3.Connection, a: dict[str, Any]) -> dict[str, Any]:
    offset, limit = page(a)
    kind = a.get("window", "upcoming")
    require(kind in {"all", "upcoming", "due", "overdue"}, "Invalid reminder window")
    where, params = ["1=1"], []
    state = a.get("status", "pending")
    require(state in {"pending", "acknowledged", "cancelled", "all"}, "Invalid reminder status")
    if state != "all":
        where.append("status=?")
        params.append(state)
    if a.get("task_id"):
        store.get(db, "tasks", a["task_id"])
        where.append("task_id=?")
        params.append(a["task_id"])
    if kind != "all":
        where.append("remind_at>?" if kind == "upcoming" else "remind_at<=?")
        params.append(now())
    for key, op in (("from_time", ">="), ("until_time", "<=")):
        if a.get(key):
            where.append(f"remind_at{op}?")
            params.append(instant(a[key], a.get("timezone") or s.workspace.timezone))
    return s.listing(
        db.execute(
            "SELECT * FROM reminders WHERE "
            + " AND ".join(where)
            + " ORDER BY remind_at,id LIMIT ? OFFSET ?",
            (*params, limit + 1, offset),
        ),
        offset,
        limit,
    )


def update(s: TaskService, db: sqlite3.Connection, a: dict[str, Any]) -> dict[str, Any]:
    item = store.get(db, "reminders", a["reminder_id"])
    store.version(item, a["expected_version"])
    patch = changed_fields(a["patch"], {"remind_at", "timezone", "note", "status"})
    zone = patch.get("timezone", item["timezone"])
    require("timezone" not in patch or "remind_at" in patch, "Changing timezone requires remind_at")
    if "remind_at" in patch:
        patch["remind_at"] = instant(
            patch["remind_at"], zone, allow_past=a.get("allow_past", False)
        )
        patch["timezone"] = zone
    if "note" in patch:
        patch["note"] = text(patch["note"], "note", 2048, empty=True)
    state = patch.get("status", item["status"])
    require(state in {"pending", "acknowledged", "cancelled"}, "Invalid reminder status")
    if state == "pending":
        eligible(db, item["task_id"])
        if item["status"] != "pending" and "remind_at" not in patch:
            instant(item["remind_at"], zone, allow_past=a.get("allow_past", False))
    store.update(db, "reminders", item["id"], patch)
    store.event(db, item["task_id"], "reminder.updated", s.profile.id, {"reminder_id": item["id"]})
    return store.get(db, "reminders", item["id"])
