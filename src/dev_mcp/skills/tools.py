"""The skill feature's eight MCP tools; business logic lives in SkillService."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from dev_mcp.config import Settings, write_private
from dev_mcp.skills.files import SkillError

Runner = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def register(server: MCPServer[None], run: Runner, settings: Settings) -> None:
    async def invoke(operation: str, arguments: dict[str, Any]) -> CallToolResult:
        arguments.pop("invoke", None)
        try:
            if len(json.dumps(arguments).encode()) > 64 * 1024:
                raise SkillError(
                    "request_limit", "Tool arguments exceed 64 KiB; select fewer changed paths"
                )
            result = await run(operation, arguments)
        except SkillError as exc:
            result = {"error": {"code": exc.code, "message": str(exc), **exc.details}}
        encoded = json.dumps(result, ensure_ascii=True, separators=(",", ":"))
        if len(json.dumps({"text": encoded, "structured": result}).encode()) > 60 * 1024:
            path = settings.state / "manifests" / f"latest-{operation}.json"
            write_private(path, encoded.encode())
            result = {
                k: result[k]
                for k in (
                    "session_id",
                    "acquisition_id",
                    "proposal_id",
                    "status",
                    "revision",
                    "next_offset",
                    "remaining",
                    "session_status",
                    "error",
                )
                if k in result
            }
            if "error" in result:
                error = result["error"]
                result["error"] = {"code": error["code"], "message": error["message"][:1000]}
            result.update(
                {
                    "result_truncated": True,
                    "result_path": str(path),
                    "guidance": "Read the local result file; it is replaced on the next "
                    "oversized response from this tool. Use smaller search pages if needed.",
                }
            )
            encoded = json.dumps(result)
        return CallToolResult(
            content=[TextContent(type="text", text=encoded)],
            structured_content=result,
            is_error="error" in result,
        )

    read = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    write = ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    )

    @server.tool(annotations=read)
    async def skills_search(query: str = "", offset: int = 0, limit: int = 20) -> CallToolResult:
        """Search skill names/descriptions, or list with an empty query. Pages: 20 by default, 50
        max.
        """
        return await invoke("skills_search", locals())

    @server.tool(annotations=read)
    async def skills_inspect(skill_id: str) -> CallToolResult:
        """Inspect instructions, prerequisites, revision, size and local file manifest before
        borrowing.
        """
        return await invoke("skills_inspect", locals())

    @server.tool(annotations=write)
    async def skills_session_open(
        request_id: str, client: str, checkout: str, task: str, task_ref: str | None = None
    ) -> CallToolResult:
        """Open a durable task session for an absolute local checkout. Reuse request_id only for
        retries.
        """
        return await invoke("skills_session_open", locals())

    @server.tool(annotations=write)
    async def skills_acquire(
        request_id: str, session_id: str, skill_id: str, revision: str | None = None
    ) -> CallToolResult:
        """Borrow a private package outside the checkout. Use explicitly; write outputs
        elsewhere; release afterward.
        """
        return await invoke("skills_acquire", locals())

    @server.tool(annotations=read)
    async def skills_sessions(
        session_id: str | None = None,
        checkout: str | None = None,
        client: str | None = None,
        task: str | None = None,
        outstanding_only: bool = True,
        offset: int = 0,
        limit: int = 20,
    ) -> CallToolResult:
        """Recover sessions by checkout/client/task, or inspect one session's copies and recovery
        conflicts.
        """
        return await invoke("skills_sessions", locals())

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
        )
    )
    async def skills_release(
        acquisition_id: str | None = None,
        session_id: str | None = None,
        close_session: bool = False,
    ) -> CallToolResult:
        """Release one copy or all session copies. Preserve edits/unexpected files; close only
        after successful cleanup.
        """
        return await invoke("skills_release", locals())

    @server.tool(annotations=write)
    async def skills_update_prepare(
        request_id: str,
        acquisition_id: str,
        changes: list[dict[str, str]],
        human_instruction: str,
        reason: str,
    ) -> CallToolResult:
        """Freeze explicit {path, action: add|replace|delete} edits. Record quoted human
        authorization and reason.
        """
        return await invoke("skills_update_prepare", locals())

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
        )
    )
    async def skills_update_apply(request_id: str, proposal_id: str) -> CallToolResult:
        """Publish the exact prepared edits if the original revision is current; journal recovery
        and an audit receipt.
        """
        return await invoke("skills_update_apply", locals())
