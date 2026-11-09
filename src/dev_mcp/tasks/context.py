"""Immutable managed context imports and bounded, workspace-authorized reads."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import uuid
from pathlib import Path
from typing import Any

from dev_mcp.access import Access
from dev_mcp.config import private_directory, sync_directory
from dev_mcp.tasks import store
from dev_mcp.tasks.common import now, require


def asset(db: sqlite3.Connection, ident: str) -> dict[str, Any]:
    item = store.get(db, "context_assets", ident)
    require(item["status"] == "ready", "Context import is unfinished; inspect local storage")
    return item


def import_file(access: Access, workspace: str, source: Path) -> dict[str, Any]:
    """Owner CLI holds owner_lock; publish metadata only after a complete streamed capture."""
    path = store.initialize(access, workspace)
    root = access.root(workspace) / "context"
    ident = str(uuid.uuid4())
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as src:
        before = os.fstat(src.fileno())
        require(stat.S_ISREG(before.st_mode), "Context source must be a regular file")
        require(before.st_size <= access.settings.package_bytes, "Context exceeds package_bytes")
        with store.coordination(access):
            db = store.connect(path)
            try:
                with db:
                    total, count = db.execute(
                        "SELECT coalesce(sum(size),0),count(*) FROM context_assets"
                    ).fetchone()
                    require(
                        total + before.st_size <= access.settings.storage_bytes
                        and count < access.settings.package_files,
                        "Context capacity reached; preserve/export data or raise configured limits",
                        "capacity",
                    )
                    db.execute(
                        "INSERT INTO context_assets VALUES(?,?,?,?,?,?,?)",
                        (ident, source.name[:256], before.st_size, None, "pending", now(), None),
                    )
            finally:
                db.close()
        # This copy does not hold the task coordination lock.
        private_directory(root)
        h = hashlib.sha256()
        size = 0
        with (root / ident).open("xb") as dst:
            os.chmod(root / ident, 0o600)
            while chunk := src.read(1024 * 1024):
                size += len(chunk)
                require(size <= before.st_size, "Context changed during capture")
                h.update(chunk)
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        after = os.fstat(src.fileno())
        require(
            (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            and size == before.st_size,
            "Context changed during capture; unfinished import is preserved for owner inspection",
            "conflict",
        )
        info = (root / ident).stat()
        sync_directory(root)
        with store.coordination(access):
            db = store.connect(path)
            try:
                with db:
                    db.execute(
                        "UPDATE context_assets SET sha256=?,status='ready',identity=? WHERE id=?",
                        (
                            h.hexdigest(),
                            f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}:{info.st_ctime_ns}",
                            ident,
                        ),
                    )
                    store.event(db, ident, "context.imported", "local-owner")
                return asset(db, ident)
            finally:
                db.close()


def read(
    access: Access, workspace: str, db: sqlite3.Connection, ident: str, offset: int
) -> dict[str, Any]:
    item = asset(db, ident)
    require(type(offset) is int and offset >= 0, "offset must be a nonnegative byte offset")
    path = access.root(workspace) / "context" / item["id"]
    require(path.parent.resolve() == path.parent, "Managed context path was replaced")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as src:
        info = os.fstat(src.fileno())
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_size == item["size"]
            and f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}:{info.st_ctime_ns}"
            == item["identity"],
            "Managed context was externally changed; import it again as a new snapshot",
            "conflict",
        )
        src.seek(offset)
        chunk = src.read(2048)
    try:
        decoded = chunk.decode("utf-8") if b"\x00" not in chunk else None
    except UnicodeDecodeError:
        decoded = None
    return {
        "id": ident,
        "name": item["name"],
        "size": item["size"],
        "sha256": item["sha256"],
        "path": str(path),
        "text": decoded,
        "offset": offset,
        "next_offset": offset + len(chunk) if offset + len(chunk) < item["size"] else None,
        "guidance": "Immutable local file. UTF-8 previews are bounded to 2048 bytes; "
        "binary/boundary-split previews are omitted. Use the path if filesystem access permits.",
    }
