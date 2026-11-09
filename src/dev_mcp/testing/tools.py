"""Small, explicitly enabled MCP interface for testing coordination."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server import MCPServer
from mcp.types import CallToolResult, ToolAnnotations

from dev_mcp.access import Profile
from dev_mcp.tasks.service import OPERATIONS

Invoker = Callable[[str, dict[str, Any]], Awaitable[CallToolResult]]


def register(server: MCPServer[None], invoke: Invoker, profile: Profile) -> None:
    def expose(fn: Callable[..., Any]) -> Callable[..., Any]:
        if OPERATIONS[fn.__name__][0] in profile.permissions:
            server.tool(
                annotations=ToolAnnotations(
                    read_only_hint=fn.__name__ == "testing_list",
                    destructive_hint=False,
                    idempotent_hint=True,
                    open_world_hint=False,
                )
            )(fn)
        return fn

    @expose
    async def testing_join(
        request_id: str,
        source_worktree: str,
        session_ref: str,
        additional_info: str = "",
        task_id: str | None = None,
        attempt_id: str | None = None,
    ) -> CallToolResult:
        """Join strict FIFO for a Git repo's main checkout. Returns a ticket, never permission."""
        return await invoke("testing_join", locals())

    @expose
    async def testing_check(ticket_id: str) -> CallToolResult:
        """Poll ONLY while waiting (~30s). can_test grants the slot until explicit testing_done."""
        return await invoke("testing_check", locals())

    @expose
    async def testing_done(
        request_id: str,
        ticket_id: str,
        outcome: str = "",
        stack_stopped: bool = False,
        cleanup_complete: bool = False,
    ) -> CallToolResult:
        """Withdraw or finish. Holders must confirm stack stopped and cleanup complete."""
        return await invoke("testing_done", locals())

    @expose
    async def testing_list(
        ticket_id: str | None = None,
        repository: str | None = None,
        source_worktree: str | None = None,
        session_ref: str | None = None,
        status: str = "pending",
        offset: int = 0,
        limit: int = 20,
    ) -> CallToolResult:
        """Recover workspace tickets; default pending. Repository means Git common directory."""
        return await invoke("testing_list", locals())
