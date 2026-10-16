"""Local service management. Never start a fallback server for a failed client."""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from dev_mcp.config import Settings, bearer_token, write_private
from dev_mcp.skills.files import SkillError

UNIT = "dev-mcp.service"


def systemd_unit(settings: Settings) -> str:
    def quote(value: str) -> str:
        if any(c in value for c in "\n\r\x00"):
            raise ValueError("Newlines are not allowed in service paths")
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'

    return "\n".join(
        [
            "[Unit]",
            "Description=Local developer MCP service",
            "",
            "[Service]",
            "Type=simple",
            "UMask=0077",
            "Restart=on-failure",
            "RestartSec=3",
            "Environment=" + quote("DEV_MCP_CONFIG=" + str(settings.config)),
            "Environment=" + quote("DEV_MCP_STATE=" + str(settings.state)),
            "Environment=" + quote("DEV_MCP_LIBRARY=" + str(settings.library)),
            "ExecStart=" + quote(sys.executable) + " -m dev_mcp serve",
            "NoNewPrivileges=true",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Local developer MCP server")
    parser.add_argument("--library", type=Path, help="Authoritative skills directory")
    parser.add_argument("--state", type=Path, help="Private state directory outside all checkouts")
    parser.add_argument("--config", type=Path, help="Private configuration directory")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("stdio", help="Separate compatibility process (default)")
    parser.add_argument("--profile", help="Fixed connection profile (required for stdio)")
    serve = sub.add_parser("serve", help="Run the shared loopback HTTP service")
    serve.add_argument("--port", type=int)
    service = sub.add_parser("service", help="Manage the user-level systemd service")
    service.add_argument("action", choices=["install", "start", "stop", "status"])
    service.add_argument("--port", type=int)
    recovery = sub.add_parser(
        "recovery", help="Inspect or explicitly resolve source recovery conflicts"
    )
    recovery.add_argument("action", choices=["status", "resolve"])
    recovery.add_argument("proposal_id", nargs="?")
    recovery.add_argument(
        "--keep-current-source",
        action="store_true",
        help="Accept inspected source and discard this transaction's backup/proposal",
    )
    workspace = sub.add_parser(
        "workspace", help="Owner workspace configuration and offline backups"
    )
    workspace.add_argument(
        "action", choices=["list", "configure", "backup", "restore", "reconcile", "context-import"]
    )
    workspace.add_argument("name", nargs="?")
    workspace.add_argument("--timezone", default="Europe/Berlin")
    workspace.add_argument("--delegation-enabled", action="store_true")
    workspace.add_argument("--execution-profile", action="append", default=[])
    workspace.add_argument("--path", type=Path)
    workspace.add_argument("--attempt-id")
    workspace.add_argument("--confirmed-stopped", action="store_true")
    profile = sub.add_parser(
        "profile", help="Owner-only profile credentials; secrets are never printed"
    )
    profile.add_argument("action", choices=["create", "rotate", "revoke"])
    profile.add_argument("name")
    profile.add_argument("--workspace", default="personal")
    profile.add_argument(
        "--permissions",
        default="tasks.read,tasks.write,reminders.read,reminders.write,pauses.read,pauses.write",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Request URLs/headers never belong in logs. Uvicorn access logging is disabled below.
    for logger in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(logger).setLevel(logging.ERROR)
    try:
        if args.config:
            os.environ["DEV_MCP_CONFIG"] = str(args.config.expanduser().absolute())
        settings = Settings.load()
        overrides = {
            key: getattr(args, key).expanduser().absolute()
            for key in ("library", "state", "config")
            if getattr(args, key) is not None
        }
        if getattr(args, "port", None) is not None:
            overrides["port"] = args.port
        settings = dataclasses.replace(settings, **overrides)
        settings.validate()
        if args.command in {"workspace", "profile"}:
            from dev_mcp.owner import manage

            manage(settings, args)
            return
        if args.command == "recovery":
            from dev_mcp.skills.recovery import accept_current_source
            from dev_mcp.skills.service import SkillService

            if args.action == "status":
                print(json.dumps(SkillService(settings).call("skills_sessions", {}), indent=2))
            else:
                if not args.proposal_id or not args.keep_current_source:
                    raise ValueError(
                        "Resolve requires a proposal ID and --keep-current-source "
                        "after inspecting and preserving needed backup/proposal files"
                    )
                print(json.dumps(accept_current_source(settings, args.proposal_id), indent=2))
            return
        if args.command == "service":
            if args.action == "install":
                settings.save()
                bearer_token(settings)
                unit_dir = (
                    Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
                    / "systemd/user"
                )
                unit_dir.mkdir(parents=True, exist_ok=True)
                write_private(unit_dir / UNIT, systemd_unit(settings).encode())
                subprocess.run(
                    ["systemd-analyze", "--user", "verify", str(unit_dir / UNIT)], check=True
                )
                subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
                subprocess.run(["systemctl", "--user", "enable", UNIT], check=True)
                subprocess.run(["systemctl", "--user", "start", UNIT], check=True)
                subprocess.run(["systemctl", "--user", "is-active", "--quiet", UNIT], check=True)
                print(f"Installed {UNIT}; http://127.0.0.1:{settings.port}/mcp")
                print(f"Bearer token file: {settings.config / 'token'}")
            else:
                result = subprocess.run(["systemctl", "--user", args.action, UNIT], check=False)
                raise SystemExit(result.returncode)
            return
        from dev_mcp.server import create_server

        if args.command == "serve":
            import uvicorn

            settings.initialize()
            # One shared daemon for this state root, even if a second caller chooses another port.
            with (settings.state / "locks/daemon").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError(
                        "Shared service is already running for this state directory"
                    ) from None
                from dev_mcp.transport import ProfileHTTP

                app = ProfileHTTP(settings)
                uvicorn.run(
                    app,
                    host="127.0.0.1",
                    port=settings.port,
                    access_log=False,
                    log_level="warning",
                    limit_concurrency=64,
                )
        else:
            if not args.profile:
                raise ValueError(
                    "Stdio requires --profile NAME; use --profile legacy for existing skills"
                )
            create_server(settings, profile=args.profile).run("stdio")
    except (ValueError, OSError, SkillError, subprocess.CalledProcessError) as exc:
        print(f"dev-mcp: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
