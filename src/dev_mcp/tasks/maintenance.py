"""Explicit local-owner reconciliation and stopped-service workspace backups."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from dev_mcp.access import Access
from dev_mcp.config import private_directory, sync_directory, write_private
from dev_mcp.tasks import store
from dev_mcp.tasks.common import now, require


@contextmanager
def offline(access: Access) -> Iterator[None]:
    """Shared service owns this lock; caller must also stop compatibility clients."""
    access.settings.initialize()
    fd = os.open(
        access.settings.state / "locks/daemon", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Stop the shared service before backup/restore") from exc
        with store.coordination(access):
            yield
    finally:
        os.close(fd)


def reconcile(
    access: Access, workspace: str, attempt_id: str, *, confirmed_stopped: bool
) -> dict[str, Any]:
    require(confirmed_stopped, "Inspect the process/session and confirm it has stopped first")
    path = store.initialize(access, workspace)
    with store.coordination(access):
        db = store.connect(path)
        try:
            with db:
                attempt = store.get(db, "attempts", attempt_id)
                require(attempt["status"] in store.ACTIVE, "Attempt is already terminal")
                store.update(db, "attempts", attempt_id, {"status": "reconciled"})
                store.update(db, "tasks", attempt["task_id"], {"status": "blocked"})
                if attempt["delegation_id"]:
                    store.update(
                        db, "delegations", attempt["delegation_id"], {"status": "reconciled"}
                    )
                store.event(
                    db,
                    attempt["task_id"],
                    "execution.owner_reconciled",
                    "local-owner",
                    {"attempt_id": attempt_id, "confirmed_stopped": True},
                )
            return {"attempt_id": attempt_id, "status": "reconciled", "task_status": "blocked"}
        finally:
            db.close()


def copy_file(source: Path, target: Path) -> str:
    require(source.is_file() and not source.is_symlink(), "Backup files must be regular files")
    h = hashlib.sha256()
    with source.open("rb") as src, target.open("xb") as dst:
        target.chmod(0o600)
        while chunk := src.read(1024 * 1024):
            h.update(chunk)
            dst.write(chunk)
        dst.flush()
        os.fsync(dst.fileno())
    return h.hexdigest()


def backup(access: Access, workspace: str, destination: Path) -> dict[str, Any]:
    root = access.root(workspace)
    require(not destination.exists(), "Backup destination must not exist")
    require(
        not any((p / ".git").exists() for p in (destination, *destination.parents)),
        "Backups must be outside Git checkouts",
    )
    with offline(access):
        require((root / "tasks.sqlite3").exists(), "Workspace has no task database")
        private_directory(destination)
        source = store.connect(root / "tasks.sqlite3")
        target = sqlite3.connect(destination / "tasks.sqlite3")
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        (destination / "tasks.sqlite3").chmod(0o600)
        manifest: dict[str, str] = {}
        # Scan only this workspace's managed context, never arbitrary task references.
        context = root / "context"
        if context.exists():
            for path in context.rglob("*"):
                require(not path.is_symlink(), "Resolve symlinks before backing up context")
                relative = path.relative_to(root)
                if path.is_dir():
                    private_directory(destination / relative)
                else:
                    private_directory((destination / relative).parent)
                    manifest[str(relative)] = copy_file(path, destination / relative)
        write_private(
            destination / "backup.json",
            json.dumps(
                {"version": 1, "workspace": workspace, "created": now(), "files": manifest}
            ).encode(),
        )
    return {
        "backup": str(destination),
        "workspace": workspace,
        "includes": "tasks, reminders, checkpoints, attempts, audits and managed context",
        "excludes": "credentials, skill packages/state, external context references",
    }


def restore(
    access: Access, workspace: str, source: Path, *, confirmed_stopped: bool
) -> dict[str, Any]:
    require(confirmed_stopped, "Stop workers and all stdio clients before restoring")
    require(source.is_dir() and not source.is_symlink(), "Backup must be a regular directory")
    meta_path = source / "backup.json"
    require(
        meta_path.is_file()
        and not meta_path.is_symlink()
        and meta_path.stat().st_size < 1024 * 1024,
        "Invalid backup manifest",
    )
    meta = json.loads(meta_path.read_text())
    require(
        meta.get("version") == 1 and meta.get("workspace") == workspace,
        "Backup must belong to the same workspace",
    )
    root = access.root(workspace)
    # Restore only into an empty workspace. Never discard newer work or execution reservations.
    with offline(access):
        require(
            not root.exists(),
            "Restore requires an absent workspace directory; preserve existing data first",
        )
        staging = root.with_name(root.name + ".restore")
        require(not staging.exists(), "Unfinished restore exists; inspect it before retrying")
        private_directory(staging)
        try:
            copy_file(source / "tasks.sqlite3", staging / "tasks.sqlite3")
            db = store.connect(staging / "tasks.sqlite3")
            try:
                require(
                    db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "Invalid database"
                )
                require(
                    db.execute("PRAGMA user_version").fetchone()[0] == 1,
                    "Unsupported backup schema",
                )
                with db:
                    db.execute(
                        "UPDATE attempts SET status='uncertain',version=version+1 "
                        "WHERE status IN ('running','cancel_requested')"
                    )
                    db.execute(
                        "UPDATE delegations SET status='uncertain',version=version+1 "
                        "WHERE status IN ('running','cancel_requested')"
                    )
            finally:
                db.close()
            for name, expected in meta["files"].items():
                relative = Path(name)
                require(
                    not relative.is_absolute()
                    and ".." not in relative.parts
                    and relative.parts[0] == "context",
                    "Unsafe backup path",
                )
                require(
                    (source / relative).resolve().is_relative_to(source.resolve()),
                    "Unsafe backup link",
                )
                private_directory((staging / relative).parent)
                require(
                    copy_file(source / relative, staging / relative) == expected,
                    "Backup content checksum mismatch",
                )
            # Restored files have new inode identities; verify their recorded content first.
            db = store.connect(staging / "tasks.sqlite3")
            try:
                with db:
                    for row in db.execute("SELECT * FROM context_assets WHERE status='ready'"):
                        file = staging / "context" / row["id"]
                        require(
                            meta["files"].get("context/" + row["id"]) == row["sha256"]
                            and file.stat().st_size == row["size"],
                            "Context metadata does not match the backup",
                        )
                        info = file.stat()
                        db.execute(
                            "UPDATE context_assets SET identity=? WHERE id=?",
                            (
                                f"{info.st_dev}:{info.st_ino}:"
                                f"{info.st_mtime_ns}:{info.st_ctime_ns}",
                                row["id"],
                            ),
                        )
            finally:
                db.close()
            private_directory(staging / "context")
            os.replace(staging, root)
            sync_directory(root.parent)
        except Exception:
            # Only our unpublished restore staging is disposable.
            shutil.rmtree(staging)
            raise
    return {
        "workspace": workspace,
        "restored": True,
        "guidance": "Reconcile uncertain executions before retrying any work",
    }
