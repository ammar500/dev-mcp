"""Focused, profile-filtered MCP schemas for the task feature."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from dev_mcp.access import Profile
from dev_mcp.skills.files import SkillError
from dev_mcp.tasks.common import TaskError
from dev_mcp.tasks.service import OPERATIONS

Runner = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def register(server: MCPServer[None], run: Runner, profile: Profile) -> None:
    def expose(fn: Callable[..., Any]) -> Callable[..., Any]:
        name = fn.__name__
        permission, mutation = OPERATIONS[name][:2] if name in OPERATIONS else (None, False)
        if permission is None or permission in profile.permissions:
            server.tool(
                annotations=ToolAnnotations(
                    read_only_hint=not mutation,
                    destructive_hint=False,
                    idempotent_hint=True,
                    open_world_hint=False,
                )
            )(fn)
        return fn

    async def invoke(name: str, args: dict[str, Any]) -> CallToolResult:
        args.pop("invoke", None)
        # Optional collections use None in the schema but have empty defaults in the service.
        for key in ("context", "result"):
            if args.get(key, False) is None:
                args.pop(key)
        try:
            result = await run(name, args)
        except (TaskError, SkillError) as exc:
            result = {"error": {"code": exc.code, "message": str(exc)}}
        answer = CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result))],
            structured_content=result,
            is_error="error" in result,
        )
        if len(answer.model_dump_json().encode()) > 64 * 1024:
            result = {"error": {"code": "response_limit", "message": "Use a smaller page or view"}}
            answer = CallToolResult(
                content=[TextContent(type="text", text=json.dumps(result))],
                structured_content=result,
                is_error=True,
            )
        return answer

    @expose
    async def workspace_context() -> CallToolResult:
        """Report this connection's fixed workspace, profile and enabled capabilities."""
        return await invoke("workspace_context", locals())

    @expose
    async def tasks_create(
        request_id: str,
        title: str,
        instructions: str = "",
        priority: int = 0,
        due: str | None = None,
        checkout: str | None = None,
        context: list[dict[str, Any]] | None = None,
    ) -> CallToolResult:
        """Save a general task. Never launches an agent."""
        return await invoke("tasks_create", locals())

    @expose
    async def tasks_list(
        query: str = "",
        status: str | None = None,
        checkout: str | None = None,
        due_before: str | None = None,
        include_archived: bool = False,
        offset: int = 0,
        limit: int = 20,
    ) -> CallToolResult:
        """Search tasks in this workspace; paginated summaries, maximum 50."""
        return await invoke("tasks_list", locals())

    @expose
    async def tasks_get(
        task_id: str, view: str = "task", offset: int = 0, limit: int = 20, context_index: int = 0
    ) -> CallToolResult:
        """Get task/history/attempts/delegations or managed context (index, byte offset)."""
        return await invoke("tasks_get", locals())

    @expose
    async def tasks_update(
        request_id: str, task_id: str, expected_version: int, patch: dict[str, Any]
    ) -> CallToolResult:
        """Edit selected task fields. Active ownership and queued delegation block editing."""
        return await invoke("tasks_update", locals())

    @expose
    async def tasks_transition(
        request_id: str, task_id: str, expected_version: int, action: str
    ) -> CallToolResult:
        """Complete, cancel, block, or reopen an unclaimed task; claim to start work."""
        return await invoke("tasks_transition", locals())

    @expose
    async def tasks_archive(
        request_id: str, task_id: str, expected_version: int, archived: bool = True
    ) -> CallToolResult:
        """Archive or restore a task. Archiving cancels pending reminders."""
        return await invoke("tasks_archive", locals())

    @expose
    async def tasks_claim(
        request_id: str, task_id: str, expected_version: int, session_ref: str
    ) -> CallToolResult:
        """Claim for your existing agent; launches nothing. Heartbeat within 300 seconds."""
        return await invoke("tasks_claim", locals())

    @expose
    async def tasks_report(
        request_id: str,
        attempt_id: str,
        expected_version: int,
        action: str,
        result: dict[str, Any] | None = None,
    ) -> CallToolResult:
        """Report heartbeat/progress/complete/block/release/cancel/fail for your own attempt."""
        return await invoke("tasks_report", locals())

    @expose
    async def reminders_create(
        request_id: str,
        task_id: str,
        remind_at: str,
        timezone: str | None = None,
        note: str = "",
        allow_past: bool = False,
    ) -> CallToolResult:
        """Save a queryable reminder with an explicit offset. Does not schedule execution."""
        return await invoke("reminders_create", locals())

    @expose
    async def reminders_list(
        window: str = "upcoming",
        status: str = "pending",
        task_id: str | None = None,
        from_time: str | None = None,
        until_time: str | None = None,
        timezone: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> CallToolResult:
        """List upcoming/due/overdue/all reminders. Reading never acknowledges them."""
        return await invoke("reminders_list", locals())

    @expose
    async def reminders_update(
        request_id: str,
        reminder_id: str,
        expected_version: int,
        patch: dict[str, Any],
        allow_past: bool = False,
    ) -> CallToolResult:
        """Reschedule, acknowledge, or cancel a reminder without completing its task."""
        return await invoke("reminders_update", locals())

    @expose
    async def pauses_save(
        request_id: str,
        raw_text: str,
        task_id: str | None = None,
        checkout: str | None = None,
        session_ref: str | None = None,
        supersedes: str | None = None,
    ) -> CallToolResult:
        """Save exactly where you left off. No AI, task changes, or agent control."""
        return await invoke("pauses_save", locals())

    @expose
    async def pauses_find(
        pause_id: str | None = None,
        query: str = "",
        task_id: str | None = None,
        checkout: str | None = None,
        session_ref: str | None = None,
        unresolved_only: bool = True,
        include_archived: bool = False,
        offset: int = 0,
        limit: int = 20,
    ) -> CallToolResult:
        """Find recent unresolved checkpoints or fetch one by ID. Reading does not resume work."""
        return await invoke("pauses_find", locals())

    @expose
    async def pauses_update(
        request_id: str, pause_id: str, expected_version: int, patch: dict[str, Any]
    ) -> CallToolResult:
        """Update flags/context. Correct raw text using a new superseding note."""
        return await invoke("pauses_update", locals())

    @expose
    async def tasks_delegate(
        request_id: str,
        task_id: str,
        expected_version: int,
        execution_profile: str,
        not_before: str | None = None,
        timezone: str | None = None,
    ) -> CallToolResult:
        """Queue a frozen task for a configured worker. No production runners ship yet."""
        return await invoke("tasks_delegate", locals())

    @expose
    async def delegations_cancel(
        request_id: str, delegation_id: str, expected_version: int
    ) -> CallToolResult:
        """Cancel queued work or request an active worker stop; stopping needs confirmation."""
        return await invoke("delegations_cancel", locals())

    @expose
    async def worker_claim(
        request_id: str, execution_profiles: list[str], session_ref: str
    ) -> CallToolResult:
        """Worker-only atomic queue claim. New request ID per poll; no subprocess is launched."""
        return await invoke("worker_claim", locals())

    @expose
    async def worker_heartbeat(
        request_id: str, attempt_id: str, context_index: int | None = None, offset: int = 0
    ) -> CallToolResult:
        """Heartbeat, cancellation state, and optional managed context of your own attempt."""
        return await invoke("worker_heartbeat", locals())

    @expose
    async def worker_finish(
        request_id: str,
        attempt_id: str,
        expected_version: int,
        action: str,
        result: dict[str, Any] | None = None,
    ) -> CallToolResult:
        """Worker-only terminal report: complete/block/release/cancel/fail."""
        return await invoke("worker_finish", locals())
