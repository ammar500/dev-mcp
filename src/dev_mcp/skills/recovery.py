"""Explicit owner recovery after preserving and reconciling conflicting source edits."""

from __future__ import annotations

import json
from typing import Any

from dev_mcp.config import Settings
from dev_mcp.skills.files import SkillError, capture, metadata
from dev_mcp.skills.service import SkillService
from dev_mcp.storage import connect, exclusive


def accept_current_source(settings: Settings, proposal_id: str) -> dict[str, Any]:
    service = SkillService(settings)
    with exclusive(settings), connect(settings) as db:
        proposal = service.row(db, "proposals", proposal_id)
        if proposal["status"] != "recovery_required":
            raise SkillError("invalid_state", "This proposal does not require conflict recovery")
        source = service.source(proposal["skill_id"])
        metadata(source, proposal["skill_id"])
        manifest = capture(source, settings)
        # The operator explicitly chose the current source after inspecting backups.
        # No source files are written. Frozen content/backups become disposable only now.
        receipt = service.audit(
            db,
            "owner_accepted_recovery",
            {
                "proposal_id": proposal_id,
                "skill_id": proposal["skill_id"],
                "accepted_revision": manifest["revision"],
                "authorization": "local_cli_keep_current_source",
            },
        )
        result = {
            "proposal_id": proposal_id,
            "status": "recovered",
            "revision": manifest["revision"],
            "audit_receipt": receipt,
        }
        db.execute(
            "UPDATE proposals SET status='recovered',receipt=? WHERE id=?",
            (json.dumps(result), proposal_id),
        )
        db.execute("DELETE FROM recovery WHERE id=?", (proposal_id,))
        db.commit()
        service.discard_completed(settings.state / "transactions" / proposal_id, proposal_id)
        return result
