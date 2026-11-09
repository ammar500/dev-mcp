"""One HTTP process, fixed profile endpoints, independent MCP authorization."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from dev_mcp.access import Access
from dev_mcp.config import Settings
from dev_mcp.server import create_server
from dev_mcp.storage import Operations


class ProfileHTTP:
    def __init__(self, settings: Settings):
        access = Access(settings)
        self.skill_admission = Operations(settings)
        self.task_admission = Operations(settings, namespace="tasks-")
        self.apps: dict[str, Starlette] = {}
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[f"127.0.0.1:{settings.port}", f"localhost:{settings.port}"],
            allowed_origins=[
                f"http://127.0.0.1:{settings.port}",
                f"http://localhost:{settings.port}",
            ],
        )
        for name in ("legacy", *access.profiles):
            path = "/mcp" if name == "legacy" else f"/mcp/{name}"
            server = create_server(
                settings,
                token=access.token(name),
                profile=name,
                access=access,
                skill_admission=self.skill_admission,
                task_admission=self.task_admission,
            )
            self.apps[path] = server.streamable_http_app(
                streamable_http_path=path,
                host="127.0.0.1",
                json_response=True,
                stateless_http=True,
                max_request_body_size=128 * 1024,
                transport_security=security,
            )
        self.lifecycle = Starlette(lifespan=self.lifespan)

    @asynccontextmanager
    async def lifespan(self, app: Starlette) -> AsyncIterator[None]:
        try:
            async with AsyncExitStack() as stack:
                for child in self.apps.values():
                    await stack.enter_async_context(child.router.lifespan_context(child))
                yield
        finally:
            self.skill_admission.close()
            self.task_admission.close()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.lifecycle(scope, receive, send)
            return
        app = self.apps.get(scope.get("path", ""))
        if app is None:
            await JSONResponse({"error": "not_found"}, status_code=404)(scope, receive, send)
        else:
            await app(scope, receive, send)
