"""Durable ownership and delegation contracts; deliberately no agent subprocesses."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from dev_mcp.tasks import context as managed_context
from dev_mcp.tasks import store, task
from dev_mcp.tasks.common import TaskError, dump, instant, now, require, text

if TYPE_CHECKING:
    from dev_mcp.tasks.service import TaskService

LEASE_SECONDS = 300


def expires() -> str:
    return (datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)).isoformat(timespec="microseconds")


def runnable(item: dict[str, Any]) -> None:
    require(
        not item["archived"] and item["status"] in {"open", "blocked"},
        "Only open/blocked unarchived tasks can be claimed or delegated",
    )


def start(
    s: TaskService,
    db: sqlite3.Connection,
    item: dict[str, Any],
    session: str,
    delegation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    worker = delegation is not None
    store.reserve(s.access, item["checkout"], worker=worker)
    ident, stamp = str(uuid.uuid4()), now()
    snapshot = delegation["snapshot"] if delegation else item
    db.execute(
        "INSERT INTO attempts(id,task_id,delegation_id,owner,mode,snapshot,checkout,status,"
        "version,session_ref,heartbeat,expires,created,updated) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            ident,
            item["id"],
            delegation["id"] if delegation else None,
            s.profile.id,
            "worker" if worker else "external",
            dump(snapshot),
            item["checkout"],
            "running",
            1,
            text(session, "session_ref", 1024),
            stamp,
            expires(),
            stamp,
            stamp,
        ),
    )
    store.update(db, "tasks", item["id"], {"status": "in_progress"})
    if delegation:
        store.update(db, "delegations", delegation["id"], {"status": "running"})
    store.event(
        db,
        item["id"],
        "execution.started",
        s.profile.id,
        {"attempt_id": ident, "mode": "worker" if worker else "external"},
    )
    return {
        **store.get(db, "attempts", ident),
        "execution_profile": delegation["execution_profile"] if delegation else None,
    }


def claim(s: TaskService, db: sqlite3.Connection, a: dict[str, Any]) -> dict[str, Any]:
    item = store.get(db, "tasks", a["task_id"])
    store.version(item, a["expected_version"])
    runnable(item)
    store.no_execution(db, item["id"])
    return start(s, db, item, a["session_ref"])


def report(s: TaskService, db: sqlite3.Connection, a: dict[str, Any]) -> dict[str, Any]:
    attempt = store.get(db, "attempts", a["attempt_id"])
    require(attempt["owner"] == s.profile.id, "Attempt belongs to another profile", "forbidden")
    worker = "worker" in s.profile.permissions
    require((attempt["mode"] == "worker") == worker, "Wrong attempt interface", "forbidden")
    store.version(attempt, a["expected_version"])
    require(
        attempt["status"] in {"running", "cancel_requested"},
        "Attempt is terminal or uncertain; owner recovery is required",
        "conflict",
    )
    action = a["action"]
    require(
        action in {"heartbeat", "progress", "complete", "block", "release", "cancel", "fail"},
        "Invalid report action",
    )
    result = a.get("result", {})
    require(
        isinstance(result, dict) and len(dump(result).encode()) <= 8192,
        "result must be an object at most 8 KiB",
    )
    require(set(result) <= {"summary", "references"}, "Result supports summary and references")
    if "summary" in result:
        text(result["summary"], "summary", 4096, empty=True)
    if "references" in result:
        require(
            isinstance(result["references"], list) and len(result["references"]) <= 32,
            "references must be a list of at most 32 strings",
        )
        for reference in result["references"]:
            text(reference, "reference", 1024)
    fields: dict[str, Any] = {"heartbeat": now(), "expires": expires()}
    if result:
        fields["result"] = dump(result)
    if action not in {"heartbeat", "progress"}:
        if attempt["status"] == "cancel_requested":
            require(action == "cancel", "Confirm cancellation before releasing this execution")
        task_state, run_state = {
            "complete": ("done", "completed"),
            "block": ("blocked", "blocked"),
            "release": ("open", "released"),
            "cancel": ("cancelled", "cancelled"),
            "fail": ("blocked", "failed"),
        }[action]
        fields["status"] = run_state
        store.update(db, "tasks", attempt["task_id"], {"status": task_state})
        if task_state in {"done", "cancelled"}:
            store.cancel_reminders(db, attempt["task_id"])
        if attempt["delegation_id"]:
            store.update(db, "delegations", attempt["delegation_id"], {"status": run_state})
    store.update(db, "attempts", attempt["id"], fields)
    if action != "heartbeat":
        store.event(
            db,
            attempt["task_id"],
            "execution." + action,
            s.profile.id,
            {"attempt_id": attempt["id"], **result},
        )
    item = store.get(db, "attempts", attempt["id"])
    return {k: v for k, v in item.items() if k != "snapshot"}


def delegate(s: TaskService, db: sqlite3.Connection, a: dict[str, Any]) -> dict[str, Any]:
    require(s.workspace.delegation_enabled, "Delegation is disabled for this workspace", "disabled")
    item = store.get(db, "tasks", a["task_id"])
    store.version(item, a["expected_version"])
    runnable(item)
    store.no_execution(db, item["id"])
    profile = a["execution_profile"]
    require(profile in s.workspace.execution_profiles, "Execution profile is not configured")
    when = (
        instant(a["not_before"], a.get("timezone") or s.workspace.timezone)
        if a.get("not_before")
        else now()
    )
    ident, stamp = str(uuid.uuid4()), now()
    db.execute(
        "INSERT INTO delegations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            ident,
            item["id"],
            item["version"],
            dump(item),
            profile,
            when,
            item["priority"],
            "queued",
            1,
            stamp,
            stamp,
        ),
    )
    # Version changes make the reservation visible to concurrent editors.
    store.update(db, "tasks", item["id"], {})
    store.event(db, item["id"], "delegation.queued", s.profile.id, {"delegation_id": ident})
    return {
        **task.summary(store.get(db, "delegations", ident)),
        "execution_state": "waiting_for_worker",
        "agent_started": False,
    }


def cancel(s: TaskService, db: sqlite3.Connection, a: dict[str, Any]) -> dict[str, Any]:
    delegation = store.get(db, "delegations", a["delegation_id"])
    store.version(delegation, a["expected_version"])
    require(
        delegation["status"] in {"queued", "running", "cancel_requested"},
        "Delegation is terminal or uncertain; reconcile uncertain attempts first",
        "conflict",
    )
    attempt = store.active(db, delegation["task_id"])
    if attempt:
        store.update(db, "attempts", attempt["id"], {"status": "cancel_requested"})
    store.update(
        db,
        "delegations",
        delegation["id"],
        {"status": "cancel_requested" if attempt else "cancelled"},
    )
    if not attempt:
        store.update(db, "tasks", delegation["task_id"], {})
    store.event(
        db,
        delegation["task_id"],
        "delegation.cancel_requested",
        s.profile.id,
        {"delegation_id": delegation["id"]},
    )
    return task.summary(store.get(db, "delegations", delegation["id"]))


def worker_claim(s: TaskService, db: sqlite3.Connection, a: dict[str, Any]) -> dict[str, Any]:
    require(s.workspace.delegation_enabled, "Delegation is disabled for this workspace", "disabled")
    supported = a["execution_profiles"]
    require(
        isinstance(supported, list)
        and 1 <= len(supported) <= 32
        and all(isinstance(p, str) and p in s.workspace.execution_profiles for p in supported),
        "Worker must name configured execution profiles",
    )
    marks = ",".join("?" for _ in supported)
    rows = db.execute(
        "SELECT * FROM delegations WHERE status='queued' AND not_before<=? "
        f"AND execution_profile IN ({marks}) ORDER BY priority DESC,created,id LIMIT 50",
        (now(), *supported),
    )
    blocked = False
    for row in rows:
        delegation = store.decode(dict(row))
        item = store.get(db, "tasks", delegation["task_id"])
        runnable(item)
        # Existing queued snapshots cannot be changed through the editing tools.
        try:
            return start(s, db, item, a["session_ref"], delegation)
        except TaskError as exc:
            if exc.code != "checkout_busy":
                raise
            blocked = True
    require(
        not blocked,
        "First 50 eligible requests have occupied checkouts; release/reconcile owners "
        "or assign separate worktrees before polling again",
        "checkout_busy",
    )
    return {"status": "idle", "guidance": "Poll with a new request ID; no agent was started"}


def worker_heartbeat(s: TaskService, db: sqlite3.Connection, a: dict[str, Any]) -> dict[str, Any]:
    # Read-current heartbeat lets a worker observe cancellation, which changed its version.
    attempt = store.get(db, "attempts", a["attempt_id"])
    result = report(s, db, {**a, "expected_version": attempt["version"], "action": "heartbeat"})
    if a.get("context_index") is not None:
        index = a["context_index"]
        entries = attempt["snapshot"]["context"]
        require(type(index) is int and 0 <= index < len(entries), "Invalid context_index")
        entry = entries[index]
        require(entry["kind"] == "managed", "Only managed context has server-owned contents")
        result["context_file"] = managed_context.read(
            s.access, s.profile.workspace, db, entry["value"], a.get("offset", 0)
        )
    return result


def worker_finish(s: TaskService, db: sqlite3.Connection, a: dict[str, Any]) -> dict[str, Any]:
    require(
        a["action"] in {"complete", "block", "release", "cancel", "fail"},
        "Finish needs a terminal action",
    )
    return report(s, db, a)
