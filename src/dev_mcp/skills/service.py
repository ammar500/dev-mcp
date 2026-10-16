"""Transport-independent local skill management, with a durable operation ledger."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import uuid
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dev_mcp.config import Settings, sync_directory, write_private
from dev_mcp.skills.files import (
    SkillError,
    capture,
    current_file,
    file_record,
    identity,
    metadata,
    parent_fd,
    reader,
    relative_path,
    same_content,
    skill_id,
    stamp,
)
from dev_mcp.storage import capacity, connect, dump, exclusive, initialize


def now() -> str:
    return datetime.now(UTC).isoformat()


def identifier() -> str:
    return str(uuid.uuid4())


def text_field(value: str, name: str, maximum: int = 1024) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise SkillError(
            "invalid_argument", f"{name} must be nonempty text, at most {maximum} characters"
        )
    return value


def page_args(offset: int, limit: int) -> None:
    if offset < 0 or not 1 <= limit <= 50:
        raise SkillError(
            "invalid_argument", "offset must be nonnegative; limit must be between 1 and 50"
        )


def indexed(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f["path"]: f for f in manifest["files"]}


def checkout_path(value: str) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise SkillError(
            "invalid_checkout", "checkout must be an existing absolute directory on this computer"
        )
    return str(path.resolve())


class SkillService:
    def __init__(self, settings: Settings):
        self.settings = settings
        initialize(settings)
        with exclusive(settings), connect(settings) as db:
            self.recover(db)

    def call(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # A single cross-process lock coordinates source updates, quota reservations,
        # retries and cleanup, including compatibility stdio processes.
        with exclusive(self.settings), connect(self.settings) as db:
            method = getattr(self, operation)
            try:
                return dict(method(db, **arguments))
            except (SkillError, OSError) as exc:
                error = (
                    exc
                    if isinstance(exc, SkillError)
                    else SkillError(
                        "filesystem_error",
                        "Filesystem operation failed; check permissions and recovery records",
                        detail=str(exc)[:500],
                    )
                )
                request_id = arguments.get("request_id")
                if request_id:
                    pending = db.execute(
                        "SELECT object_id FROM requests WHERE operation=? "
                        "AND request_id=? AND result IS NULL",
                        (operation.removeprefix("skills_"), request_id),
                    ).fetchone()
                    if pending is not None:
                        error.details["object_id"] = pending["object_id"]
                        if operation == "skills_acquire":
                            staging = (
                                self.settings.state / "acquisitions" / f".building-{pending[0]}"
                            )
                            if staging.exists():
                                shutil.rmtree(staging)
                            db.execute(
                                "UPDATE acquisitions SET status='interrupted' WHERE id=? "
                                "AND status='preparing'",
                                (pending[0],),
                            )
                        elif operation == "skills_update_prepare":
                            stage = self.settings.state / "proposals" / pending[0]
                            if stage.exists():
                                shutil.rmtree(stage)
                            db.execute(
                                "UPDATE proposals SET status='failed' WHERE id=?", (pending[0],)
                            )
                        elif operation == "skills_update_apply":
                            transaction = (
                                self.settings.state / "transactions" / arguments["proposal_id"]
                            )
                            if transaction.is_dir() and not (transaction / "journal.json").exists():
                                shutil.rmtree(transaction)
                    db.execute(
                        "UPDATE requests SET result=? WHERE operation=? AND request_id=? "
                        "AND result IS NULL",
                        (
                            dump(
                                {
                                    "error": {
                                        "code": error.code,
                                        "message": str(error),
                                        **error.details,
                                    }
                                }
                            ),
                            operation.removeprefix("skills_"),
                            request_id,
                        ),
                    )
                db.commit()
                raise error from exc

    def source(self, value: str) -> Path:
        path = self.settings.library / skill_id(value)
        if not path.exists():
            raise SkillError("not_found", "Skill does not exist", skill_id=value)
        if (
            not stat.S_ISDIR(path.lstat().st_mode)
            or self.settings.library.resolve() != self.settings.library
        ):
            raise SkillError("unsafe_file", "Library and skill roots must be real directories")
        return path

    def row(self, db: sqlite3.Connection, table: str, object_id: str) -> dict[str, Any]:
        if table not in {"sessions", "acquisitions", "proposals"}:
            raise ValueError("invalid internal table")
        row = db.execute(f"SELECT * FROM {table} WHERE id=?", (object_id,)).fetchone()
        if row is None:
            raise SkillError("not_found", f"Unknown {table} ID")
        return dict(row)

    def begin(
        self, db: sqlite3.Connection, op: str, request_id: str, arguments: dict[str, Any]
    ) -> tuple[str, dict[str, Any] | None]:
        text_field(request_id, "request_id", 128)
        encoded = dump(arguments)
        prior = db.execute(
            "SELECT * FROM requests WHERE operation=? AND request_id=?", (op, request_id)
        ).fetchone()
        if prior:
            if prior["arguments"] != encoded:
                raise SkillError(
                    "request_conflict", "Request ID was already used with different arguments"
                )
            if prior["result"] is None:
                raise SkillError(
                    "interrupted_request",
                    "This request was interrupted. Inspect outstanding "
                    "sessions/recovery records before using a new request ID.",
                    object_id=prior["object_id"],
                )
            result = json.loads(prior["result"])
            if "error" in result:
                error = result["error"]
                raise SkillError(error.pop("code"), error.pop("message"), **error)
            return prior["object_id"], result
        object_id = identifier()
        db.execute(
            "INSERT INTO requests VALUES(?,?,?,?,NULL)", (op, request_id, encoded, object_id)
        )
        db.commit()
        return object_id, None

    def finish(
        self, db: sqlite3.Connection, op: str, request_id: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        db.execute(
            "UPDATE requests SET result=? WHERE operation=? AND request_id=?",
            (dump(result), op, request_id),
        )
        db.commit()
        return result

    def audit(self, db: sqlite3.Connection, kind: str, details: dict[str, Any]) -> str:
        event = identifier()
        db.execute("INSERT INTO audit VALUES(?,?,?,?)", (event, now(), kind, dump(details)))
        return event

    def unblocked(self, db: sqlite3.Connection, value: str) -> None:
        rows = db.execute("SELECT id FROM recovery WHERE skill_id=?", (value,)).fetchall()
        if rows:
            raise SkillError(
                "recovery_required",
                "An interrupted source update has conflicts. Resolve "
                "its local journal before changing this skill.",
                transactions=[r[0] for r in rows],
            )

    def skills_search(
        self, db: sqlite3.Connection, query: str = "", offset: int = 0, limit: int = 20
    ) -> dict[str, Any]:
        page_args(offset, limit)
        if len(query) > 1024:
            raise SkillError("invalid_argument", "Query exceeds 1024 characters")
        # Refresh only metadata, on demand; never hash package contents at startup.
        db.execute("DELETE FROM catalogue")
        invalid: list[dict[str, str]] = []
        invalid_count = 0
        if self.settings.library.exists():
            with os.scandir(self.settings.library) as entries:
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    try:
                        root = self.source(entry.name)
                        meta, _ = metadata(root, entry.name)
                        db.execute(
                            "INSERT INTO catalogue VALUES(?,?,?,?)",
                            (
                                entry.name,
                                meta["name"],
                                meta["description"],
                                meta.get("compatibility"),
                            ),
                        )
                    except (SkillError, OSError) as exc:
                        invalid_count += 1
                        if len(invalid) < 10:
                            invalid.append({"skill_id": entry.name, "error": str(exc)[:300]})
        pattern = query.lower()
        rows = db.execute(
            "SELECT id AS skill_id,name,description FROM catalogue WHERE "
            "instr(lower(name),?) OR instr(lower(description),?) ORDER BY id LIMIT ? OFFSET ?",
            (pattern, pattern, limit + 1, offset),
        ).fetchall()
        return {
            "skills": [dict(r) for r in rows[:limit]],
            "next_offset": offset + limit if len(rows) > limit else None,
            "invalid_count": invalid_count,
            "invalid_skills": invalid,
        }

    def skills_inspect(self, db: sqlite3.Connection, skill_id: str) -> dict[str, Any]:
        root = self.source(skill_id)
        meta, instructions = metadata(root, skill_id)
        manifest = capture(root, self.settings)
        path = self.settings.state / "manifests" / f"{skill_id}.json"
        write_private(path, dump(manifest).encode())
        return {
            "skill_id": skill_id,
            "revision": manifest["revision"],
            "instructions": instructions[:12000],
            "instructions_truncated": len(instructions) > 12000,
            "instructions_path": str(root / "SKILL.md"),
            "declared_prerequisites": {
                "compatibility": meta.get("compatibility"),
                "metadata": meta.get("metadata", {}),
            },
            "size_bytes": manifest["size"],
            "file_count": len(manifest["files"]),
            "manifest_path": str(path),
            "guidance": "Prerequisites are declarations only. "
            "The server never executes skill scripts or installs dependencies.",
        }

    def skills_session_open(
        self,
        db: sqlite3.Connection,
        request_id: str,
        client: str,
        checkout: str,
        task: str,
        task_ref: str | None = None,
    ) -> dict[str, Any]:
        client, task = text_field(client, "client", 128), text_field(task, "task", 512)
        checkout = checkout_path(checkout)
        if task_ref is not None:
            text_field(task_ref, "task_ref")
        args = {"client": client, "checkout": checkout, "task": task, "task_ref": task_ref}
        session, prior = self.begin(db, "session_open", request_id, args)
        if prior is not None:
            return prior
        db.execute(
            "INSERT INTO sessions VALUES(?,?,?,?,?,?,?)",
            (session, client, checkout, task, task_ref, now(), "open"),
        )
        self.audit(db, "session_opened", {"session_id": session, **args})
        return self.finish(
            db, "session_open", request_id, {"session_id": session, **args, "status": "open"}
        )

    def skills_acquire(
        self,
        db: sqlite3.Connection,
        request_id: str,
        session_id: str,
        skill_id: str,
        revision: str | None = None,
    ) -> dict[str, Any]:
        acquisition, prior = self.begin(
            db,
            "acquire",
            request_id,
            {"session_id": session_id, "skill_id": skill_id, "revision": revision},
        )
        if prior is not None:
            return prior
        session = self.row(db, "sessions", session_id)
        if session["status"] != "open":
            raise SkillError("session_closed", "Open a new session before borrowing more skills")
        self.unblocked(db, skill_id)
        source = self.source(skill_id)
        metadata(source, skill_id)
        original = capture(source, self.settings)
        if revision is not None and revision != original["revision"]:
            raise SkillError(
                "revision_conflict", "Requested revision is no longer current; inspect again"
            )
        capacity(self.settings, original["size"])
        directory = self.settings.state / "acquisitions" / acquisition
        staging = directory.with_name(f".building-{acquisition}")
        db.execute(
            "INSERT INTO acquisitions VALUES(?,?,?,?,?,?,?,?,?)",
            (
                acquisition,
                session_id,
                skill_id,
                original["revision"],
                str(directory),
                None,
                None,
                "preparing",
                now(),
            ),
        )
        db.commit()
        staging.mkdir(mode=0o700)
        try:
            manifest = capture(
                source, replace(self.settings, package_bytes=original["size"]), staging
            )
        except BaseException:
            # Never issued to a client. On a real process crash startup performs the same cleanup.
            shutil.rmtree(staging)
            raise
        metadata(staging, skill_id)
        if manifest["revision"] != original["revision"]:
            raise SkillError(
                "capture_conflict", "Original changed before copying; use a new request ID"
            )
        manifest_path = self.settings.state / "manifests" / f"{acquisition}.json"
        write_private(manifest_path, dump(manifest).encode())
        db.execute(
            "UPDATE acquisitions SET manifest=?,baseline=? WHERE id=?",
            (dump(manifest), dump(manifest), acquisition),
        )
        db.commit()
        os.replace(staging, directory)
        sync_directory(directory.parent)
        db.execute(
            "UPDATE acquisitions SET manifest=?,baseline=?,status='issued' WHERE id=?",
            (dump(manifest), dump(manifest), acquisition),
        )
        self.audit(
            db,
            "copy_issued",
            {
                "acquisition_id": acquisition,
                "session_id": session_id,
                "skill_id": skill_id,
                "revision": manifest["revision"],
            },
        )
        return self.finish(
            db,
            "acquire",
            request_id,
            {
                "acquisition_id": acquisition,
                "session_id": session_id,
                "skill_id": skill_id,
                "revision": manifest["revision"],
                "directory": str(directory),
                "skill_path": str(directory / "SKILL.md"),
                "manifest_path": str(manifest_path),
                "status": "issued",
                "cleanup": "Call skills_release with acquisition_id or session_id when finished. "
                "Write task outputs outside this directory. Edited/unexpected files are preserved. "
                "Use these files explicitly; no native installation occurred. If your sandbox "
                "cannot access this directory, request filesystem access. "
                "Keep it outside your checkout.",
            },
        )

    def skills_sessions(
        self,
        db: sqlite3.Connection,
        session_id: str | None = None,
        checkout: str | None = None,
        client: str | None = None,
        task: str | None = None,
        outstanding_only: bool = True,
        offset: int = 0,
        limit: int = 20,
    ) -> dict[str, Any]:
        page_args(offset, limit)
        clauses, params = [], []
        for key, value in (
            ("id", session_id),
            ("checkout", checkout_path(checkout) if checkout else None),
            ("client", client),
            ("task", task),
        ):
            if value is not None:
                clauses.append(f"{key}=?")
                params.append(value)
        if outstanding_only and session_id is None:
            clauses.append(
                "(status='open' OR EXISTS(SELECT 1 FROM proposals p JOIN acquisitions a "
                "ON p.acquisition_id=a.id WHERE a.session_id=sessions.id "
                "AND p.status IN ('prepared','preparing','applying','recovery_required')))"
            )
        rows = db.execute(
            "SELECT * FROM sessions WHERE "
            + (" AND ".join(clauses) or "1")
            + " ORDER BY created,id LIMIT ? OFFSET ?",
            (*params, limit + 1, 0 if session_id else offset),
        ).fetchall()
        sessions = [dict(r) for r in rows[:limit]]
        for session in sessions:
            session["outstanding_acquisitions"] = db.execute(
                "SELECT count(*) FROM acquisitions WHERE session_id=? AND status!='released'",
                (session["id"],),
            ).fetchone()[0]
        acquisitions = []
        next_acquisition = None
        if session_id:
            self.row(db, "sessions", session_id)
            copies = db.execute(
                "SELECT id AS acquisition_id,skill_id,revision,directory,status "
                "FROM acquisitions WHERE session_id=? AND (?=0 OR status!='released') "
                "ORDER BY created,id LIMIT ? OFFSET ?",
                (session_id, outstanding_only, limit + 1, offset),
            ).fetchall()
            acquisitions = [dict(r) for r in copies[:limit]]
            next_acquisition = offset + limit if len(copies) > limit else None
        proposals = [
            dict(r)
            for r in db.execute(
                "SELECT p.id AS proposal_id,p.acquisition_id,p.skill_id,p.status,p.before_revision,"
                "p.after_revision FROM proposals p JOIN acquisitions a ON p.acquisition_id=a.id "
                "WHERE (? IS NULL OR a.session_id=?) "
                "AND p.status NOT IN ('applied','rolled_back','recovered','failed') "
                "ORDER BY p.created,p.id LIMIT ? OFFSET ?",
                (session_id, session_id, limit + 1, offset),
            )
        ]
        recovery = [dict(r) for r in db.execute("SELECT * FROM recovery ORDER BY id LIMIT 20")]
        return {
            "sessions": sessions,
            "next_offset": offset + limit if len(rows) > limit else None,
            "acquisitions": acquisitions,
            "next_acquisition_offset": next_acquisition,
            "proposals": proposals[:limit],
            "next_proposal_offset": offset + limit if len(proposals) > limit else None,
            "proposal_directory": str(self.settings.state / "proposals"),
            "recovery": recovery,
            "recovery_directory": str(self.settings.state / "transactions"),
        }

    def cleanup(self, acquisition: dict[str, Any]) -> list[str]:
        root = Path(acquisition["directory"])
        if not root.exists() and not root.is_symlink():
            return []
        baseline = json.loads(acquisition["baseline"]) if acquisition["baseline"] else None
        if (
            not baseline
            or not stat.S_ISDIR(root.lstat().st_mode)
            or identity(root.lstat()) != baseline["root_identity"]
        ):
            return [str(root)]
        unresolved = []
        for rel, expected in indexed(baseline).items():
            try:
                actual = current_file(root, rel, self.settings.package_bytes)
                if actual is None:
                    continue
                if not same_content(actual, expected) or actual["identity"] != expected["identity"]:
                    unresolved.append(str(root / rel))
                    continue
                with parent_fd(root, rel) as (fd, name):
                    fresh = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if (
                        identity(fresh) == expected["identity"]
                        and list(stamp(fresh)) == actual["stamp"]
                    ):
                        os.unlink(name, dir_fd=fd)
                        os.fsync(fd)
                    else:
                        unresolved.append(str(root / rel))
            except (OSError, SkillError):
                unresolved.append(str(root / rel))
        # Only recorded directory inodes are ours. Unexpected empty directories
        # and replaced parents are preserved just like unexpected files.
        owned_dirs = {d["path"]: d for d in baseline.get("directories", [])}
        for rel, expected in sorted(owned_dirs.items(), reverse=True):
            try:
                with parent_fd(root, rel) as (fd, name):
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode) and identity(info) == expected["identity"]:
                        os.rmdir(name, dir_fd=fd)
                        os.fsync(fd)
            except (OSError, SkillError):
                pass
        inspected = 0
        for rel in ["", *owned_dirs]:
            try:
                directory = root / rel
                expected_id = owned_dirs[rel]["identity"] if rel else baseline["root_identity"]
                if not directory.exists() or identity(directory.lstat()) != expected_id:
                    if directory.exists() or directory.is_symlink():
                        unresolved.append(str(directory))
                    continue
                if rel:
                    with parent_fd(root, rel) as (fd, name):
                        scan_fd = os.open(
                            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                        )
                else:
                    scan_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    with os.scandir(scan_fd) as entries:
                        for entry in entries:
                            inspected += 1
                            if inspected > self.settings.package_files * 2:
                                unresolved.append(str(root))
                                break
                            entry_rel = f"{rel}/{entry.name}" if rel else entry.name
                            known = owned_dirs.get(entry_rel)
                            if (
                                known is None
                                or identity(entry.stat(follow_symlinks=False)) != known["identity"]
                            ):
                                unresolved.append(str(root / entry_rel))
                finally:
                    os.close(scan_fd)
                if inspected > self.settings.package_files * 2:
                    break
            except (OSError, SkillError):
                unresolved.append(str(root / rel))
        try:
            if identity(root.lstat()) == baseline["root_identity"]:
                root.rmdir()
        except OSError:
            if not unresolved:
                unresolved.append(str(root))
        return sorted(set(unresolved))

    def skills_release(
        self,
        db: sqlite3.Connection,
        acquisition_id: str | None = None,
        session_id: str | None = None,
        close_session: bool = False,
    ) -> dict[str, Any]:
        if (acquisition_id is None) == (session_id is None):
            raise SkillError("invalid_argument", "Provide exactly one acquisition_id or session_id")
        acquisitions: Iterable[dict[str, Any]]
        if acquisition_id:
            acquisitions = [self.row(db, "acquisitions", acquisition_id)]
            session_id = self.row(db, "acquisitions", acquisition_id)["session_id"]
        else:
            acquisitions = (
                dict(r)
                for r in db.execute(
                    "SELECT * FROM acquisitions WHERE session_id=? AND status!='released'",
                    (session_id,),
                )
            )
        assert session_id is not None
        self.row(db, "sessions", session_id)
        results: list[dict[str, Any]] = []
        processed = 0
        for acquisition in acquisitions:
            processed += 1
            unresolved = [] if acquisition["status"] == "released" else self.cleanup(acquisition)
            status = "unresolved" if unresolved else "released"
            db.execute("UPDATE acquisitions SET status=? WHERE id=?", (status, acquisition["id"]))
            if acquisition["status"] != status:
                self.audit(
                    db, "copy_cleanup", {"acquisition_id": acquisition["id"], "status": status}
                )
            report = self.settings.state / "manifests" / f"{acquisition['id']}-cleanup.json"
            write_private(report, dump({"unresolved_paths": unresolved}).encode())
            if len(results) < 20:
                results.append(
                    {
                        "acquisition_id": acquisition["id"],
                        "status": status,
                        "unresolved_paths": unresolved[:20],
                        "unresolved_count": len(unresolved),
                        "report_path": str(report),
                    }
                )
        remaining = db.execute(
            "SELECT count(*) FROM acquisitions WHERE session_id=? AND status!='released'",
            (session_id,),
        ).fetchone()[0]
        if close_session and remaining == 0:
            db.execute("UPDATE sessions SET status='closed' WHERE id=?", (session_id,))
        return {
            "session_id": session_id,
            "session_status": self.row(db, "sessions", session_id)["status"],
            "remaining": remaining,
            "acquisitions": results,
            "processed_count": processed,
            "results_truncated": processed > len(results),
            "guidance": "Preserved files require your attention. Move valuable edits/outputs to "
            "a safe location, then retry release. Never delete an unresolved tree blindly.",
        }

    def skills_update_prepare(
        self,
        db: sqlite3.Connection,
        request_id: str,
        acquisition_id: str,
        changes: list[dict[str, str]],
        human_instruction: str,
        reason: str,
    ) -> dict[str, Any]:
        text_field(human_instruction, "human_instruction", 4096)
        text_field(reason, "reason", 2048)
        if not changes or len(changes) > self.settings.package_files:
            raise SkillError("invalid_argument", "Supply a nonempty explicit list of file changes")
        seen = set()
        for change in changes:
            if set(change) != {"path", "action"} or change["action"] not in {
                "add",
                "replace",
                "delete",
            }:
                raise SkillError(
                    "invalid_argument",
                    "Each change requires path and action: add, replace or delete",
                )
            rel = relative_path(change["path"])
            if rel in seen:
                raise SkillError("invalid_argument", "Duplicate changed file", path=rel)
            if rel == "SKILL.md" and change["action"] == "delete":
                raise SkillError("invalid_metadata", "SKILL.md cannot be deleted")
            seen.add(rel)
        proposal, prior = self.begin(
            db,
            "update_prepare",
            request_id,
            {
                "acquisition_id": acquisition_id,
                "changes": changes,
                "human_instruction": human_instruction,
                "reason": reason,
            },
        )
        if prior is not None:
            return prior
        acquisition = self.row(db, "acquisitions", acquisition_id)
        if acquisition["status"] not in {"issued", "unresolved"}:
            raise SkillError("invalid_state", "Updates require an issued acquisition")
        self.unblocked(db, acquisition["skill_id"])
        source = self.source(acquisition["skill_id"])
        original = capture(source, self.settings)
        if original["revision"] != acquisition["revision"]:
            raise SkillError(
                "revision_conflict",
                "Original changed since acquisition; borrow its current revision",
            )
        borrowed = Path(acquisition["directory"])
        baseline = json.loads(acquisition["baseline"])
        if (
            not stat.S_ISDIR(borrowed.lstat().st_mode)
            or identity(borrowed.lstat()) != baseline["root_identity"]
        ):
            raise SkillError("unsafe_path", "Borrowed package directory was replaced")
        before = indexed(original)
        selected: dict[str, dict[str, Any]] = {}
        delta = 0
        preview_budget = 32 * 1024
        for change in changes:
            rel, action = change["path"], change["action"]
            if (action == "add") == (rel in before):
                raise SkillError(
                    "invalid_change", "Action does not match original file presence", path=rel
                )
            if action != "delete":
                candidate = current_file(borrowed, rel, self.settings.package_bytes)
                if candidate is None:
                    raise SkillError(
                        "invalid_change", "Selected borrowed file is missing", path=rel
                    )
                delta += candidate["size"] - before.get(rel, {}).get("size", 0)
                selected[rel] = candidate
            else:
                delta -= before[rel]["size"]
            preview_budget += 2 * sum(
                min(record.get("size", 0), 64 * 1024)
                for record in (before.get(rel, {}), selected.get(rel, {}))
            )
            preview_budget += len(rel.encode()) * 8 + 2048
        if original["size"] + delta > self.settings.package_bytes:
            raise SkillError("package_limit", "Prepared package would exceed its byte limit")
        new_count = len(before) + sum(
            1 if c["action"] == "add" else -1 if c["action"] == "delete" else 0 for c in changes
        )
        if new_count > self.settings.package_files:
            raise SkillError("package_limit", "Prepared package would exceed its file limit")
        capacity(
            self.settings,
            max(original["size"], original["size"] + delta)
            + preview_budget
            + len(dump(original).encode()) * 2,
        )
        stage = self.settings.state / "proposals" / proposal
        stage.mkdir(mode=0o700)
        package = stage / "package"
        package.mkdir(mode=0o700)
        db.execute(
            "INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                proposal,
                acquisition_id,
                acquisition["skill_id"],
                original["revision"],
                None,
                human_instruction,
                reason,
                dump(changes),
                None,
                "preparing",
                now(),
                None,
            ),
        )
        db.commit()
        frozen = capture(source, replace(self.settings, package_bytes=original["size"]), package)
        if frozen["revision"] != original["revision"]:
            raise SkillError("revision_conflict", "Original changed while preparing the proposal")
        for change in changes:
            rel = change["path"]
            if change["action"] != "add":
                with parent_fd(package, rel) as (fd, name):
                    os.unlink(name, dir_fd=fd)
        for change in changes:
            rel = change["path"]
            if change["action"] != "delete":
                copied = file_record(
                    borrowed, rel, destination=package, remaining=selected[rel]["size"]
                )
                if not same_content(copied, selected[rel]):
                    raise SkillError(
                        "capture_conflict", "Selected file changed during preparation", path=rel
                    )
        metadata(package, acquisition["skill_id"])
        proposed = capture(package, self.settings)
        if capture(source, self.settings)["revision"] != original["revision"]:
            raise SkillError("revision_conflict", "Original changed during preparation")
        preview_path = stage / "preview.txt"
        after = indexed(proposed)
        with preview_path.open("x", encoding="utf-8") as preview:
            preview.write(
                "Human authorization is an agent assertion, not independently verified.\n"
            )
            preview.write(f"Instruction: {human_instruction}\nReason: {reason}\n\n")
            for change in changes:
                rel = change["path"]
                preview.write(f"{change['action'].upper()} {rel}\n")
                old, new = before.get(rel), after.get(rel)
                preview.write(f"Before: {dump(old)}\nAfter: {dump(new)}\n")
                texts: list[str | None] = []
                for root, record in ((source, old), (package, new)):
                    if record is None:
                        texts.append("")
                    elif record["size"] <= 64 * 1024:
                        with reader(root, rel) as stream:
                            raw = stream.read(64 * 1024 + 1)
                        try:
                            if b"\x00" in raw:
                                raise UnicodeError
                            texts.append(raw.decode("utf-8"))
                        except UnicodeError:
                            texts.append(None)
                    else:
                        texts.append(None)
                if all(t is not None for t in texts):
                    # Bound pathological difflib work by line/byte count.
                    old_text, new_text = texts
                    assert old_text is not None and new_text is not None
                    if len(old_text.splitlines()) + len(new_text.splitlines()) <= 2000:
                        preview.writelines(
                            difflib.unified_diff(
                                old_text.splitlines(keepends=True),
                                new_text.splitlines(keepends=True),
                                fromfile=f"before/{rel}",
                                tofile=f"after/{rel}",
                            )
                        )
                    else:
                        preview.write(
                            "Text diff omitted (more than 2,000 lines); inspect frozen package.\n"
                        )
                else:
                    preview.write(
                        "Binary or large file: inspect hashes, sizes and frozen package.\n"
                    )
                preview.write("\n")
            preview.flush()
            os.fsync(preview.fileno())
        preview_path.chmod(0o600)
        write_private(stage / "manifest.json", dump(proposed).encode())
        db.execute(
            "UPDATE proposals SET after_revision=?,manifest=?,status='prepared' WHERE id=?",
            (proposed["revision"], dump(proposed), proposal),
        )
        self.audit(
            db,
            "update_prepared",
            {
                "proposal_id": proposal,
                "session_id": acquisition["session_id"],
                "skill_id": acquisition["skill_id"],
                "affected_paths": sorted(seen),
                "before_revision": original["revision"],
                "after_revision": proposed["revision"],
            },
        )
        with preview_path.open() as preview:
            snippet = preview.read(10000)
        return self.finish(
            db,
            "update_prepare",
            request_id,
            {
                "proposal_id": proposal,
                "status": "prepared",
                "before_revision": original["revision"],
                "after_revision": proposed["revision"],
                "changes": changes[:30],
                "change_count": len(changes),
                "preview": snippet,
                "preview_truncated": preview_path.stat().st_size > len(snippet.encode()),
                "preview_path": str(preview_path),
                "manifest_path": str(stage / "manifest.json"),
                "authorization": "agent_assertion",
                "guidance": "Apply this immutable proposal with "
                "skills_update_apply. Later borrowed-copy edits will not change it.",
            },
        )

    def save_journal(self, directory: Path, journal: dict[str, Any]) -> None:
        write_private(directory / "journal.json", dump(journal).encode())

    def install_file(self, package: Path, source: Path, rel: str, temporary: str) -> dict[str, Any]:
        """Create a durable source-side temp. Its inode is journaled before rename."""
        with reader(package, rel) as inp, parent_fd(source, rel, create=True) as (fd, _):
            initial = os.fstat(inp.fileno())
            mode = 0o700 if initial.st_mode & 0o111 else 0o600
            out_fd = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=fd
            )
            digest = hashlib.sha256()
            total = 0
            with os.fdopen(out_fd, "wb") as out:
                while chunk := inp.read(1024 * 1024):
                    total += len(chunk)
                    if total > min(initial.st_size, self.settings.package_bytes):
                        raise SkillError("capture_conflict", "Staged file grew during publication")
                    out.write(chunk)
                    digest.update(chunk)
                out.flush()
                os.fsync(out.fileno())
                info = identity(os.fstat(out.fileno()))
                if stamp(initial) != stamp(os.fstat(inp.fileno())):
                    raise SkillError("capture_conflict", "Staged file changed during publication")
            os.fsync(fd)
        return {
            "path": rel,
            "size": total,
            "sha256": digest.hexdigest(),
            "executable": bool(initial.st_mode & 0o111),
            "identity": info,
        }

    def skills_update_apply(
        self, db: sqlite3.Connection, request_id: str, proposal_id: str
    ) -> dict[str, Any]:
        _, prior = self.begin(db, "update_apply", request_id, {"proposal_id": proposal_id})
        if prior is not None:
            return prior
        proposal = self.row(db, "proposals", proposal_id)
        if proposal["status"] == "applied":
            return self.finish(db, "update_apply", request_id, json.loads(proposal["receipt"]))
        if proposal["status"] != "prepared":
            raise SkillError(
                "invalid_state", "Proposal is not prepared; inspect its recovery state"
            )
        self.unblocked(db, proposal["skill_id"])
        acquisition = self.row(db, "acquisitions", proposal["acquisition_id"])
        if acquisition["status"] not in {"issued", "unresolved"}:
            raise SkillError("invalid_state", "Acquisition must remain issued until publication")
        source = self.source(proposal["skill_id"])
        original = capture(source, self.settings)
        if original["revision"] != proposal["before_revision"]:
            raise SkillError(
                "revision_conflict", "Original changed after preparation; nothing was overwritten"
            )
        package = self.settings.state / "proposals" / proposal_id / "package"
        frozen = capture(package, self.settings)
        if frozen["revision"] != proposal["after_revision"]:
            raise SkillError(
                "proposal_tampered", "Frozen proposal contents were altered; prepare a new proposal"
            )
        metadata(package, proposal["skill_id"])
        changes = json.loads(proposal["changes"])
        before, after = indexed(original), indexed(frozen)
        capacity(self.settings, sum(before.get(c["path"], {}).get("size", 0) for c in changes))
        transaction = self.settings.state / "transactions" / proposal_id
        transaction.mkdir(mode=0o700)
        backup = transaction / "backup"
        backup.mkdir(mode=0o700)
        journal: dict[str, Any] = {
            "proposal_id": proposal_id,
            "skill_id": proposal["skill_id"],
            "source": str(source),
            "root_identity": identity(source.lstat()),
            "before_revision": proposal["before_revision"],
            "changes": [],
        }
        for change in changes:
            rel = change["path"]
            saved = (
                file_record(source, rel, destination=backup, remaining=before[rel]["size"])
                if rel in before
                else None
            )
            if not same_content(saved, before.get(rel)):
                raise SkillError(
                    "revision_conflict", "Source changed during backup; nothing applied"
                )
            journal["changes"].append(
                {
                    "path": rel,
                    "before": before.get(rel),
                    "after": after.get(rel),
                    "installed": None,
                    "temporary": f".dev-mcp-{proposal_id}.tmp",
                    "started": False,
                }
            )
        self.save_journal(transaction, journal)
        db.execute("UPDATE proposals SET status='applying' WHERE id=?", (proposal_id,))
        db.commit()
        try:
            for change in journal["changes"]:
                if identity(source.lstat()) != journal["root_identity"]:
                    raise SkillError(
                        "revision_conflict", "Source directory replaced during publication"
                    )
                rel = change["path"]
                current = current_file(source, rel, self.settings.package_bytes)
                if not same_content(current, change["before"]) or (
                    current is not None and current["identity"] != change["before"]["identity"]
                ):
                    raise SkillError(
                        "revision_conflict", "Source file changed before publication", path=rel
                    )
                change["started"] = True
                self.save_journal(transaction, journal)
                if change["after"] is not None:
                    change["installed"] = self.install_file(
                        package, source, rel, change["temporary"]
                    )
                    self.save_journal(transaction, journal)
                    if not same_content(change["installed"], change["after"]):
                        raise SkillError(
                            "proposal_tampered", "Frozen file changed during publication"
                        )
                with parent_fd(source, rel, create=change["after"] is not None) as (fd, name):
                    # Check again after copying; this closes the long I/O race window.
                    current = current_file(source, rel, self.settings.package_bytes)
                    if not same_content(current, change["before"]) or (
                        current is not None and current["identity"] != change["before"]["identity"]
                    ):
                        raise SkillError(
                            "revision_conflict", "Source changed while writing", path=rel
                        )
                    if change["after"] is None:
                        os.unlink(name, dir_fd=fd)
                    else:
                        os.replace(change["temporary"], name, src_dir_fd=fd, dst_dir_fd=fd)
                    os.fsync(fd)
            if capture(source, self.settings)["revision"] != proposal["after_revision"]:
                raise SkillError(
                    "revision_conflict",
                    "Source changed during publication; rolling back owned edits",
                )
            receipt_id = self.audit(
                db,
                "update_applied",
                {
                    "proposal_id": proposal_id,
                    "session_id": acquisition["session_id"],
                    "client": self.row(db, "sessions", acquisition["session_id"])["client"],
                    "human_instruction": proposal["instruction"],
                    "reason": proposal["reason"],
                    "authorization": "agent_assertion",
                    "affected_paths": [c["path"] for c in changes],
                    "before_revision": proposal["before_revision"],
                    "after_revision": proposal["after_revision"],
                },
            )
            # Only the exact published content is accepted for cleanup. Later edits stay protected.
            baseline = json.loads(acquisition["baseline"])
            accepted = indexed(baseline)
            borrowed = Path(acquisition["directory"])
            if borrowed.exists() and identity(borrowed.lstat()) == baseline["root_identity"]:
                for change in changes:
                    rel = change["path"]
                    try:
                        actual = current_file(borrowed, rel, self.settings.package_bytes)
                    except (SkillError, OSError):
                        continue
                    if same_content(actual, after.get(rel)):
                        if actual is None:
                            accepted.pop(rel, None)
                        else:
                            accepted[rel] = actual
            accepted_dirs = {d["path"]: d for d in baseline.get("directories", [])}
            for rel in accepted:
                for parent in Path(rel).parents:
                    if str(parent) == "." or str(parent) in accepted_dirs:
                        continue
                    try:
                        info = (borrowed / parent).lstat()
                        if stat.S_ISDIR(info.st_mode):
                            accepted_dirs[str(parent)] = {
                                "path": str(parent),
                                "identity": identity(info),
                            }
                    except OSError:
                        pass
            baseline["directories"] = list(accepted_dirs.values())
            baseline["files"] = list(accepted.values())
            baseline["revision"] = proposal["after_revision"]
            db.execute(
                "UPDATE acquisitions SET baseline=?,revision=? WHERE id=?",
                (dump(baseline), proposal["after_revision"], acquisition["id"]),
            )
            result = {
                "proposal_id": proposal_id,
                "status": "applied",
                "revision": proposal["after_revision"],
                "audit_receipt": receipt_id,
                "authorization": "agent_assertion",
                "guidance": "Changes are in the library working tree. "
                "Nothing was staged, committed or pushed.",
            }
            db.execute(
                "UPDATE proposals SET status='applied',receipt=? WHERE id=?",
                (dump(result), proposal_id),
            )
            # Commit publication and retry result atomically. Recovery checks this durable marker.
            self.finish(db, "update_apply", request_id, result)
        except Exception:
            db.rollback()
            self.rollback(db, transaction, journal)
            db.commit()
            raise
        self.discard_completed(transaction, proposal_id)
        return result

    def discard_completed(self, transaction: Path, proposal_id: str) -> None:
        # These are private, never-issued staging areas, not borrowed directories.
        stage = self.settings.state / "proposals" / proposal_id
        if stage.exists():
            shutil.rmtree(stage)
        shutil.rmtree(transaction)

    def rollback(self, db: sqlite3.Connection, transaction: Path, journal: dict[str, Any]) -> None:
        source = Path(journal["source"])
        conflicts = []
        if (
            source != self.settings.library / journal["skill_id"]
            or not source.exists()
            or not stat.S_ISDIR(source.lstat().st_mode)
            or identity(source.lstat()) != journal["root_identity"]
        ):
            conflicts.append(str(source))
        else:
            for change in reversed(journal["changes"]):
                rel = change["path"]
                if not change["started"]:
                    continue
                try:
                    current = current_file(source, rel, self.settings.package_bytes)
                    before, after = change["before"], change["after"]
                    restored = change.get("restored")
                    is_original = same_content(current, before) and (
                        current is None
                        or current["identity"] == before["identity"]
                        or (restored is not None and current["identity"] == restored["identity"])
                    )
                    if not is_original:
                        owned = (after is None and current is None) or (
                            current is not None
                            and change["installed"] is not None
                            and same_content(current, after)
                            and current["identity"] == change["installed"]["identity"]
                        )
                        if not owned:
                            conflicts.append(str(source / rel))
                            continue
                        if before is None:
                            with parent_fd(source, rel) as (fd, name):
                                os.unlink(name, dir_fd=fd)
                                os.fsync(fd)
                        else:
                            saved = current_file(
                                transaction / "backup", rel, self.settings.package_bytes
                            )
                            if not same_content(saved, before):
                                conflicts.append(str(transaction / "backup" / rel))
                                continue
                            temp = change["temporary"] + "-rollback"
                            # A previous recovery may have prepared this temporary already.
                            if change.get("restored") is None:
                                change["restored"] = self.install_file(
                                    transaction / "backup", source, rel, temp
                                )
                                self.save_journal(transaction, journal)
                            with parent_fd(source, rel) as (fd, name):
                                os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
                                os.fsync(fd)
                    # Remove a known transaction temp only when its inode/content match.
                    with parent_fd(source, rel) as (fd, _):
                        temp_rel = str(Path(rel).with_name(change["temporary"]))
                        temp_record = current_file(source, temp_rel, self.settings.package_bytes)
                        if temp_record is not None:
                            installed = change["installed"]
                            if (
                                installed is None
                                or not same_content(temp_record, installed)
                                or (temp_record["identity"] != installed["identity"])
                            ):
                                conflicts.append(str(source / temp_rel))
                            else:
                                os.unlink(change["temporary"], dir_fd=fd)
                                os.fsync(fd)
                except (OSError, SkillError):
                    conflicts.append(str(source / rel))
        if conflicts:
            db.execute(
                "INSERT OR REPLACE INTO recovery VALUES(?,?,?)",
                (
                    journal["proposal_id"],
                    journal["skill_id"],
                    dump({"conflicts": conflicts, "journal": str(transaction / "journal.json")}),
                ),
            )
            db.execute(
                "UPDATE proposals SET status='recovery_required' WHERE id=?",
                (journal["proposal_id"],),
            )
        else:
            db.execute("DELETE FROM recovery WHERE id=?", (journal["proposal_id"],))
            db.execute(
                "UPDATE proposals SET status='rolled_back' WHERE id=?", (journal["proposal_id"],)
            )
            self.audit(db, "update_rolled_back", {"proposal_id": journal["proposal_id"]})
            db.commit()
            self.discard_completed(transaction, journal["proposal_id"])

    def recover(self, db: sqlite3.Connection) -> None:
        """Only pending transactions are scanned. No package catalogue scan at startup."""
        # An unissued capture is disposable. If its final directory was atomically
        # published before a crash, retain it with the recorded baseline for release.
        for row in db.execute("SELECT * FROM acquisitions WHERE status='preparing'"):
            directory = Path(row["directory"])
            staging = directory.with_name(f".building-{row['id']}")
            if staging.exists():
                shutil.rmtree(staging)
            db.execute(
                "UPDATE acquisitions SET status=? WHERE id=?",
                ("interrupted" if directory.exists() else "released", row["id"]),
            )
        for transaction in (self.settings.state / "transactions").iterdir():
            journal_path = transaction / "journal.json"
            if not journal_path.is_file():
                # Journal publication strictly precedes all original-file writes.
                # An incomplete backup is disposable; frozen proposal content remains.
                if transaction.is_dir() and not transaction.is_symlink():
                    shutil.rmtree(transaction)
                continue
            journal = json.loads(journal_path.read_text())
            proposal = self.row(db, "proposals", journal["proposal_id"])
            if proposal["status"] in {"applied", "recovered"}:
                self.discard_completed(transaction, proposal["id"])
            else:
                self.rollback(db, transaction, journal)
