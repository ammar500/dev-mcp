"""MCP construction and transports. Feature registration is separate from startup."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import CallToolResult, InputRequiredResult, TextContent
from pydantic import AnyHttpUrl

from dev_mcp import __version__
from dev_mcp.access import Access
from dev_mcp.config import Settings

if TYPE_CHECKING:
    from dev_mcp.storage import Operations

SERVER_NAME = "dev-tools"


class DeveloperServer(MCPServer[None]):
    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context[None, Any] | None = None
    ) -> CallToolResult | InputRequiredResult:
        """Bound invalid-argument responses too, before SDK validation expands errors."""
        code, message = "request_limit", "Tool arguments exceed 64 KiB"
        if len(json.dumps(arguments).encode()) <= 64 * 1024:
            try:
                return await super().call_tool(name, arguments, context)
            except UnexpectedToolError:
                code, message = (
                    "internal_error",
                    "Operation failed; inspect local recovery status before retrying",
                )
            except ToolError:
                code, message = (
                    "invalid_arguments",
                    "Unknown tool or invalid arguments; check its input schema",
                )
        result = {"error": {"code": code, "message": message}}
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result))],
            structured_content=result,
            is_error=True,
        )


class LocalTokenVerifier:
    def __init__(self, token: str, access: Access | None = None, profile: str = "legacy"):
        self._token = token
        self.access, self.profile = access, profile

    async def verify_token(self, token: str) -> AccessToken | None:
        expected = self._token
        if self.access is not None and self.profile != "legacy":
            try:
                current = Access(self.access.settings)
                if current.profile(self.profile) != self.access.profile(self.profile):
                    return None
                expected = current.token(self.profile)
            except (ValueError, OSError):
                return None
        if not secrets.compare_digest(token.encode(), expected.encode()):
            return None
        return AccessToken(token=token, client_id=self.profile, scopes=["tools"])


def create_server(
    settings: Settings | None = None,
    *,
    token: str | None = None,
    profile: str = "legacy",
    access: Access | None = None,
    skill_admission: Operations | None = None,
    task_admission: Operations | None = None,
) -> MCPServer[None]:
    from dev_mcp.skills.service import SkillService
    from dev_mcp.skills.tools import register
    from dev_mcp.storage import Operations

    settings = settings or Settings.load()
    access = access or Access(settings)
    binding = access.profile(profile)
    skill_settings = access.skill_settings(binding.workspace)
    service: SkillService | None = None
    operations: Operations | None = None
    task_service: Any = None
    task_operations: Operations | None = None
    connections = 0

    @asynccontextmanager
    async def lifespan(server: MCPServer[None]) -> AsyncIterator[None]:
        nonlocal service, operations, connections, task_service, task_operations
        if connections == 0:
            settings.initialize()
            if "skills" in binding.permissions:
                if binding.workspace != "personal":
                    from dev_mcp.config import private_directory

                    private_directory(skill_settings.library)
                service = SkillService(skill_settings)
                operations = skill_admission or Operations(settings)
            if profile != "legacy":
                from dev_mcp.tasks.service import TaskService

                task_service = TaskService(access, binding)
                task_operations = task_admission or Operations(settings, namespace="tasks-")
        connections += 1
        try:
            yield None
        finally:
            connections -= 1
            if connections == 0 and operations is not None:
                if skill_admission is None:
                    operations.close()
                service = operations = None
            if connections == 0 and task_operations is not None:
                if task_admission is None:
                    task_operations.close()
                task_service = task_operations = None

    async def run(operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert operations is not None and service is not None
        try:
            access.refresh(binding)
        except (ValueError, OSError):
            return {"error": {"code": "forbidden", "message": "Connection profile changed"}}
        if profile != "legacy" and "request_id" in arguments:
            from dev_mcp.skills.service import text_field

            text_field(arguments["request_id"], "request_id", 128)
            arguments = {
                **arguments,
                "request_id": hashlib.sha256(
                    json.dumps([profile, arguments["request_id"]]).encode()
                ).hexdigest(),
            }
        active = service
        return await operations.run(lambda: active.call(operation, arguments))

    async def run_task(operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert task_operations is not None and task_service is not None
        active = task_service
        return await task_operations.run(lambda: active.call(operation, arguments))

    url = f"http://127.0.0.1:{settings.port}"
    server = DeveloperServer(
        name=SERVER_NAME,
        title="Local Development Server",
        description="Local workspace-scoped developer tools.",
        instructions=(
            f"This connection is fixed to workspace {binding.workspace}, profile {profile}. "
            "Use workspace_context for available task capabilities. Task creation/claims and "
            "raw pause notes launch nothing. No production execution adapters or normalization "
            "are included. Mutations require unique request IDs; reuse only for exact retries. "
            "Skill files are local: borrow outside your checkout, use SKILL.md explicitly, "
            "keep task outputs elsewhere, and release copies. Update skill originals only on "
            "the human's explicit instruction; quote it in update_prepare."
            if profile != "legacy"
            else "Legacy personal skills connection. Borrow outside your checkout, use the "
            "returned SKILL.md explicitly, keep outputs elsewhere, and release copies. Update "
            "originals only on explicit human instruction; quote it in update_prepare."
        ),
        version=__version__,
        log_level="WARNING",
        lifespan=lifespan,
        token_verifier=LocalTokenVerifier(token, access, profile) if token else None,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(url),
            resource_server_url=AnyHttpUrl(
                url + ("/mcp" if profile == "legacy" else f"/mcp/{profile}")
            ),
            required_scopes=["tools"],
        )
        if token
        else None,
    )
    if "skills" in binding.permissions:
        register(server, run, skill_settings)
    if profile != "legacy":
        from dev_mcp.tasks.tools import register as register_tasks

        register_tasks(server, run_task, binding)
    return server


def main() -> None:
    from dev_mcp.cli import main as cli_main

    cli_main()


__all__ = ["SERVER_NAME", "create_server", "main"]
