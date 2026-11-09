"""Explicit local-owner recovery; no timeout, process control, or Git writes."""

from __future__ import annotations

import os
from typing import Any

from dev_mcp.access import Access
from dev_mcp.tasks import store
from dev_mcp.tasks.common import now, require, text
from dev_mcp.testing.queue import HOLDING, PENDING


def recover(
    access: Access,
    workspace: str,
    ticket_id: str,
    *,
    reason: str,
    stack_stopped: bool = False,
    cleanup_complete: bool = False,
) -> dict[str, Any]:
    reason = text(reason, "reason", 4096)
    ticket_id = text(ticket_id, "ticket_id", 100)
    path = store.initialize(access, workspace)
    with store.coordination(access):
        db = store.connect(path)
        try:
            with db:
                item = store.get(db, "testing_tickets", ticket_id)
                if item["status"] in HOLDING:
                    require(
                        stack_stopped is True and cleanup_complete is True,
                        "Inspect the stack and checkout; confirm both stopped and cleanup complete",
                    )
                if item["status"] in PENDING:
                    stamp = now()
                    db.execute(
                        "UPDATE testing_tickets SET status='reconciled',outcome=?,"
                        "finished=?,updated=? "
                        "WHERE id=?",
                        (reason, stamp, stamp, ticket_id),
                    )
                    store.event(
                        db,
                        ticket_id,
                        "testing.owner_reconciled",
                        f"local-owner:{os.getuid()}",
                        {
                            "reason": reason,
                            "previous_status": item["status"],
                            "stack_stopped": stack_stopped,
                            "cleanup_complete": cleanup_complete,
                        },
                    )
                return {
                    "ticket_id": ticket_id,
                    "status": store.get(db, "testing_tickets", ticket_id)["status"],
                    "guidance": "No process or checkout was changed; the next waiter must check.",
                }
        finally:
            db.close()
