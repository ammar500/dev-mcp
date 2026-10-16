"""Local owner CLI. No server tool may configure or expand its own access."""

from __future__ import annotations

import argparse
import json
import secrets

from dev_mcp.access import Access, owner_lock
from dev_mcp.config import Settings, write_private
from dev_mcp.tasks.common import TaskError
from dev_mcp.tasks.maintenance import backup, reconcile, restore


def manage(settings: Settings, args: argparse.Namespace) -> None:
    settings.initialize()
    try:
        with owner_lock(settings):
            access = Access(settings)
            if args.command == "profile":
                if args.action == "create":
                    path = access.create_profile(
                        args.name, args.workspace, args.permissions.split(",")
                    )
                else:
                    access.profile(args.name)
                    if args.name == "legacy":
                        raise ValueError(
                            "Legacy credential management remains outside profile commands"
                        )
                    path = access.token_path(args.name)
                    if args.action == "rotate":
                        write_private(path, (secrets.token_urlsafe(48) + "\n").encode())
                    else:
                        del access.profiles[args.name]
                        access.save()
                        path.unlink(missing_ok=True)
                print(
                    json.dumps(
                        {
                            "profile": args.name,
                            "action": args.action,
                            "credential_file": str(path),
                            "endpoint": f"http://127.0.0.1:{settings.port}/mcp/{args.name}",
                            "guidance": "Restart the shared service to add endpoints. "
                            "Rotation/revocation takes effect immediately.",
                        }
                    )
                )
            elif args.action == "list":
                print(json.dumps(access.public(), indent=2))
            elif not args.name:
                raise ValueError("Workspace name is required")
            elif args.action == "configure":
                access.configure_workspace(
                    args.name, args.timezone, args.delegation_enabled, args.execution_profile
                )
                print(json.dumps({"workspace": args.name, "configured": True}))
            elif args.action == "reconcile":
                if not args.attempt_id:
                    raise ValueError("--attempt-id is required")
                print(
                    json.dumps(
                        reconcile(
                            access,
                            args.name,
                            args.attempt_id,
                            confirmed_stopped=args.confirmed_stopped,
                        )
                    )
                )
            elif not args.path:
                raise ValueError("--path is required")
            elif args.action == "context-import":
                from dev_mcp.tasks.context import import_file

                print(json.dumps(import_file(access, args.name, args.path.expanduser().absolute())))
            elif args.action == "backup":
                print(json.dumps(backup(access, args.name, args.path.expanduser().absolute())))
            else:
                print(
                    json.dumps(
                        restore(
                            access,
                            args.name,
                            args.path.expanduser().absolute(),
                            confirmed_stopped=args.confirmed_stopped,
                        )
                    )
                )
    except TaskError as exc:
        raise ValueError(str(exc)) from exc
