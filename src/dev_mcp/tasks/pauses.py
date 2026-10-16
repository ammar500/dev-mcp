"""Immutable raw bookmarks. This module never inspects credentials or invokes AI."""

from __future__ import annotations

import sqlite3
import uuid
from typing import TYPE_CHECKING, Any

from dev_mcp.tasks import store
from dev_mcp.tasks.common import changed_fields, checkout, now, page, record_capacity, require, text

if TYPE_CHECKING:
    from dev_mcp.tasks.service import TaskService


def save(s: TaskService, db: sqlite3.Connection, a: dict[str, Any]) -> dict[str, Any]:
    task_id = a.get("task_id")
    if task_id:
        store.get(db, "tasks", task_id)
    if a.get("supersedes"):
        store.get(db, "pauses", a["supersedes"])
    raw = text(a["raw_text"], "raw_text", 16384)
    session = text(a["session_ref"], "session_ref", 1024) if a.get("session_ref") else None
    ident, stamp = str(uuid.uuid4()), now()
    db.execute(
        "INSERT INTO pauses(id,raw_text,task_id,checkout,session_ref,supersedes,version,"
        "created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            ident,
            raw,
            task_id,
            checkout(a.get("checkout")),
            session,
            a.get("supersedes"),
            1,
            stamp,
            stamp,
        ),
    )
    store.event(db, ident, "pause.saved", s.profile.id)
    record_capacity(store.get(db, "pauses", ident))
    return {"id": ident, "version": 1, "created": stamp, "saved": True}


def find(s: TaskService, db: sqlite3.Connection, a: dict[str, Any]) -> dict[str, Any]:
    if a.get("pause_id"):
        return store.get(db, "pauses", a["pause_id"])
    where, params = ["1=1"], []
    if not a.get("include_archived", False):
        where.append("archived=0")
    if a.get("unresolved_only", True):
        where.append("resumed=0")
        where.append(
            "NOT EXISTS(SELECT 1 FROM pauses newer WHERE newer.supersedes=pauses.id "
            "AND newer.archived=0)"
        )
    for key in ("task_id", "session_ref", "checkout"):
        if a.get(key):
            if key == "task_id":
                store.get(db, "tasks", a[key])
            where.append(f"{key}=?")
            params.append(checkout(a[key]) if key == "checkout" else a[key])
    if a.get("query"):
        where.append("instr(lower(raw_text),lower(?))>0")
        params.append(text(a["query"], "query", 512))
    offset, limit = page(a)
    return s.listing(
        db.execute(
            "SELECT * FROM pauses WHERE "
            + " AND ".join(where)
            + " ORDER BY created DESC,id DESC LIMIT ? OFFSET ?",
            (*params, limit + 1, offset),
        ),
        offset,
        limit,
    )


def update(s: TaskService, db: sqlite3.Connection, a: dict[str, Any]) -> dict[str, Any]:
    item = store.get(db, "pauses", a["pause_id"])
    store.version(item, a["expected_version"])
    patch = changed_fields(
        a["patch"], {"task_id", "checkout", "session_ref", "resumed", "archived"}
    )
    if patch.get("task_id"):
        store.get(db, "tasks", patch["task_id"])
    if "checkout" in patch:
        patch["checkout"] = checkout(patch["checkout"])
    if patch.get("session_ref") is not None:
        patch["session_ref"] = text(patch["session_ref"], "session_ref", 1024)
    for key in ("resumed", "archived"):
        if key in patch:
            require(type(patch[key]) is bool, f"{key} must be boolean")
    store.update(db, "pauses", item["id"], patch)
    record_capacity(store.get(db, "pauses", item["id"]))
    store.event(db, item["id"], "pause.updated", s.profile.id, {"fields": sorted(patch)})
    return {"id": item["id"], "version": item["version"] + 1, "updated": True}
