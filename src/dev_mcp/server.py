"""MCP construction and transports. Feature registration is separate from startup."""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from mcp.types import CallToolResult, InputRequiredResult, TextContent
from pydantic import AnyHttpUrl

from dev_mcp import __version__
from dev_mcp.config import Settings

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
    def __init__(self, token: str):
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not secrets.compare_digest(token.encode(), self._token.encode()):
            return None
        return AccessToken(token=token, client_id="local-user", scopes=["tools"])


def create_server(settings: Settings | None = None, *, token: str | None = None) -> MCPServer[None]:
    from dev_mcp.skills.service import SkillService
    from dev_mcp.skills.tools import register
    from dev_mcp.storage import Operations

    settings = settings or Settings.load()
    service: SkillService | None = None
    operations: Operations | None = None
    connections = 0

    @asynccontextmanager
    async def lifespan(server: MCPServer[None]) -> AsyncIterator[None]:
        nonlocal service, operations, connections
        if connections == 0:
            service = SkillService(settings)
            operations = Operations(settings)
        connections += 1
        try:
            yield None
        finally:
            connections -= 1
            if connections == 0 and operations is not None:
                operations.close()
                service = operations = None

    async def run(operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert operations is not None and service is not None
        active = service
        return await operations.run(lambda: active.call(operation, arguments))

    url = f"http://127.0.0.1:{settings.port}"
    server = DeveloperServer(
        name=SERVER_NAME,
        title="Local Development Server",
        description="Local development tools with durable skill borrowing and updates.",
        instructions="Skill files are local to this computer. Borrow outside your checkout, "
        "use the returned SKILL.md explicitly, keep task outputs elsewhere, and release copies. "
        "Update originals only on the human's explicit instruction; quote it in update_prepare.",
        version=__version__,
        log_level="WARNING",
        lifespan=lifespan,
        token_verifier=LocalTokenVerifier(token) if token else None,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(url),
            resource_server_url=AnyHttpUrl(url + "/mcp"),
            required_scopes=["tools"],
        )
        if token
        else None,
    )
    register(server, run, settings)
    return server


def main() -> None:
    from dev_mcp.cli import main as cli_main

    cli_main()


__all__ = ["SERVER_NAME", "create_server", "main"]
