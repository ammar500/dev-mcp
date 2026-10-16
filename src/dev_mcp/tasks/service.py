"""Authorization, bounded results, and atomic idempotency around task features."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import Any

from dev_mcp.access import Access, Profile
from dev_mcp.tasks import execution, pauses, reminders, store, task
from dev_mcp.tasks.common import TaskError, dump, require, response_bytes, text

Handler = Callable[["TaskService", sqlite3.Connection, dict[str, Any]], dict[str, Any]]
OPERATIONS: dict[str, tuple[str, bool, Handler]] = {
    "tasks_create": ("tasks.write", True, task.create),
    "tasks_list": ("tasks.read", False, task.listing),
    "tasks_get": ("tasks.read", False, task.get),
    "tasks_update": ("tasks.write", True, task.update),
    "tasks_transition": ("tasks.write", True, task.transition),
    "tasks_archive": ("tasks.write", True, task.archive),
    "tasks_claim": ("tasks.write", True, execution.claim),
    "tasks_report": ("tasks.write", True, execution.report),
    "reminders_create": ("reminders.write", True, reminders.create),
    "reminders_list": ("reminders.read", False, reminders.listing),
    "reminders_update": ("reminders.write", True, reminders.update),
    "pauses_save": ("pauses.write", True, pauses.save),
    "pauses_find": ("pauses.read", False, pauses.find),
    "pauses_update": ("pauses.write", True, pauses.update),
    "tasks_delegate": ("delegate", True, execution.delegate),
    "delegations_cancel": ("delegate", True, execution.cancel),
    "worker_claim": ("worker", True, execution.worker_claim),
    "worker_heartbeat": ("worker", True, execution.worker_heartbeat),
    "worker_finish": ("worker", True, execution.worker_finish),
}


class TaskService:
    def __init__(self, access: Access, profile: Profile):
        self.access, self.profile = access, profile
        self.workspace = access.workspaces[profile.workspace]
        self.path = store.initialize(access, profile.workspace)

    def context(self) -> dict[str, Any]:
        return {
            "workspace": self.profile.workspace,
            "profile": self.profile.id,
            "permissions": sorted(self.profile.permissions),
            "enabled_features": sorted({p.split(".")[0] for p in self.profile.permissions}),
            "timezone": self.workspace.timezone,
            "delegation_enabled": self.workspace.delegation_enabled,
            "execution_profiles": list(self.workspace.execution_profiles)
            if self.profile.permissions & {"delegate", "worker"}
            else [],
            "production_runners_available": False,
            "notifications_available": False,
            "pause_normalization_available": False,
        }

    @staticmethod
    def listing(cursor: sqlite3.Cursor, offset: int, limit: int) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        more = False
        size = 0
        for row in cursor:
            item = store.decode(dict(row))
            item_size = response_bytes(item)
            if len(items) == limit or size + item_size > 48 * 1024:
                more = True
                break
            items.append(item)
            size += item_size
        require(bool(items) or not more, "Record exceeds response capacity", "response_limit")
        return {"items": items, "next_offset": offset + len(items) if more else None}

    def call(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            self.access = self.access.refresh(self.profile)
            self.workspace = self.access.workspaces[self.profile.workspace]
        except (ValueError, OSError) as exc:
            raise TaskError("forbidden", "Connection profile was revoked or changed") from exc
        require(len(json.dumps(arguments).encode()) <= 64 * 1024, "Arguments exceed 64 KiB")
        if operation == "workspace_context":
            return self.context()
        require(operation in OPERATIONS, "Unknown tool", "forbidden")
        permission, mutation, handler = OPERATIONS[operation]
        require(
            permission in self.profile.permissions,
            "Tool is not permitted by this profile",
            "forbidden",
        )
        args = dict(arguments)
        request = text(args.get("request_id"), "request_id", 256) if mutation else ""
        encoded = dump(args)
        with store.coordination(self.access):
            db = store.connect(self.path)
            try:
                # Persist uncertainty even when the following requested operation is rejected.
                with db:
                    store.expire(db)
                db.execute("BEGIN IMMEDIATE")
                if mutation:
                    old = db.execute(
                        "SELECT arguments,result FROM requests WHERE profile=? "
                        "AND operation=? AND request_id=?",
                        (self.profile.id, operation, request),
                    ).fetchone()
                    if old:
                        require(
                            old["arguments"] == encoded,
                            "request_id was already used with different arguments",
                            "conflict",
                        )
                        return dict(json.loads(old["result"]))
                result = handler(self, db, args)
                require(
                    response_bytes(result) <= 60 * 1024,
                    "Result exceeds wire capacity; narrow the request",
                    "response_limit",
                )
                if mutation:
                    db.execute(
                        "INSERT INTO requests VALUES(?,?,?,?,?)",
                        (self.profile.id, operation, request, encoded, dump(result)),
                    )
                db.commit()
                return result
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise TaskError(
                    "invalid_arguments", "Invalid or missing field; check the tool schema"
                ) from exc
            except sqlite3.IntegrityError as exc:
                raise TaskError(
                    "conflict", "Conflicting ownership or invalid record reference"
                ) from exc
            except sqlite3.OperationalError as exc:
                raise TaskError(
                    "storage_unavailable",
                    "Task database unavailable; check disk space, permissions and other writers",
                ) from exc
            finally:
                db.close()
