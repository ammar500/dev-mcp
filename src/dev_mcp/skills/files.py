"""Bounded package I/O. Never execute package contents or follow package links."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

import yaml

from dev_mcp.config import Settings, sync_directory

CHUNK = 1024 * 1024
MAX_INSTRUCTIONS = 1024 * 1024
MAX_METADATA = 16 * 1024


class SkillError(Exception):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code, self.details = code, details


class MetadataLoader(yaml.SafeLoader):
    """Reject aliases and duplicate keys; keep untrusted YAML parsing bounded."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise SkillError("invalid_metadata", "YAML aliases are not supported")
        if len(self.anchors) > 32:
            raise SkillError("invalid_metadata", "Too many YAML anchors")
        return super().compose_node(parent, index)

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        try:
            if len(set(keys)) != len(keys):
                raise SkillError("invalid_metadata", "Duplicate YAML keys")
        except TypeError as exc:
            raise SkillError("invalid_metadata", "Metadata keys must be strings") from exc
        return super().construct_mapping(node, deep)


def skill_id(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value) or len(value) > 64:
        raise SkillError(
            "invalid_skill_id", "Use 1-64 lowercase letters, digits and single hyphens"
        )
    return value


def relative_path(value: str) -> str:
    p = PurePosixPath(value)
    if (
        not value
        or len(value.encode()) > 4096
        or p.is_absolute()
        or "\\" in value
        or any(part in ("", ".", "..") for part in value.split("/"))
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise SkillError(
            "unsafe_path", "Expected a normalized relative file path", path=value[:200]
        )
    return value


@contextmanager
def parent_fd(root: Path, relative: str, *, create: bool = False) -> Iterator[tuple[int, str]]:
    parts = relative_path(relative).split("/")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        yield fd, parts[-1]
    except OSError as exc:
        raise SkillError(
            "unsafe_path", "Path changed or contains an unsafe component", path=relative
        ) from exc
    finally:
        os.close(fd)


@contextmanager
def reader(root: Path, relative: str) -> Iterator[BinaryIO]:
    with parent_fd(root, relative) as (fd, name):
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise SkillError("unsafe_file", "Only regular files are supported", path=relative)
            yield stream


def identity(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino]


def stamp(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
    )


def metadata(root: Path, expected_id: str) -> tuple[dict[str, Any], str]:
    with reader(root, "SKILL.md") as stream:
        raw = stream.read(MAX_INSTRUCTIONS + 1)
    if len(raw) > MAX_INSTRUCTIONS:
        raise SkillError("invalid_metadata", "SKILL.md exceeds the 1 MiB instruction limit")
    try:
        text = raw.decode("utf-8")
        lines = text.splitlines(keepends=True)
        if not lines or lines[0].strip() != "---":
            raise ValueError("Missing YAML frontmatter")
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
        front = "".join(lines[1:end])
        if len(front.encode()) > MAX_METADATA:
            raise ValueError("Frontmatter exceeds 16 KiB")
        obj = yaml.load(front, Loader=MetadataLoader)
        if not isinstance(obj, dict) or any(not isinstance(k, str) for k in obj):
            raise ValueError("Metadata must be a mapping with string keys")
        if obj.get("name") != expected_id:
            raise ValueError("Metadata name must match the stable directory ID")
        skill_id(obj["name"])
        if not isinstance(obj.get("description"), str) or not 1 <= len(obj["description"]) <= 1024:
            raise ValueError("description must contain 1-1024 characters")
        for key in ("license", "compatibility", "allowed-tools"):
            if key in obj and not isinstance(obj[key], str):
                raise ValueError(f"{key} must be text")
        if len(obj.get("compatibility", "")) > 500:
            raise ValueError("compatibility exceeds 500 characters")
        if "metadata" in obj and (
            not isinstance(obj["metadata"], dict)
            or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in obj["metadata"].items()
            )
        ):
            raise ValueError("metadata must map strings to strings")
        return obj, text
    except (ValueError, StopIteration, yaml.YAMLError, RecursionError) as exc:
        raise SkillError("invalid_metadata", f"Invalid SKILL.md: {exc}") from exc


def tree(
    root: Path, max_entries: int, directories: list[dict[str, Any]] | None = None
) -> Iterator[str]:
    """Bound directories as well as files (including empty directories)."""
    count = 0
    pending = [""]
    if not stat.S_ISDIR(root.lstat().st_mode):
        raise SkillError("unsafe_file", "Package root must be a real directory")
    while pending:
        directory = pending.pop()
        # open directory through no-follow descriptors before listing its contents
        if directory:
            with parent_fd(root, directory) as (parent, name):
                fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        else:
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with os.scandir(fd) as entries:
                for entry in entries:
                    count += 1
                    if count > max_entries:
                        raise SkillError("package_limit", "Too many package files/directories")
                    rel = relative_path(f"{directory}/{entry.name}" if directory else entry.name)
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        if directories is not None:
                            directories.append({"path": rel, "identity": identity(info)})
                        pending.append(rel)
                    elif stat.S_ISREG(info.st_mode):
                        yield rel
                    else:
                        raise SkillError(
                            "unsafe_file", "Links and special files are forbidden", path=rel
                        )
        finally:
            os.close(fd)


def file_record(
    root: Path, rel: str, *, destination: Path | None = None, remaining: int
) -> dict[str, Any]:
    with reader(root, rel) as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > remaining:
            raise SkillError("package_limit", "Package byte limit exceeded")
        digest = hashlib.sha256()
        total = 0
        output: BinaryIO | None = None
        try:
            if destination is not None:
                with parent_fd(destination, rel, create=True) as (fd, name):
                    out_fd = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o700 if before.st_mode & 0o111 else 0o600,
                        dir_fd=fd,
                    )
                output = os.fdopen(out_fd, "wb")
            while chunk := stream.read(CHUNK):
                total += len(chunk)
                if total > remaining:
                    raise SkillError("package_limit", "Package grew beyond its byte limit")
                digest.update(chunk)
                if output is not None:
                    output.write(chunk)
            if output is not None:
                output.flush()
                os.fsync(output.fileno())
            if stamp(before) != stamp(os.fstat(stream.fileno())):
                raise SkillError("capture_conflict", "File changed during capture; retry", path=rel)
        finally:
            if output is not None:
                output.close()
    # A replaced pathname must not be mistaken for the file we opened.
    with parent_fd(root, rel) as (fd, name):
        if stamp(os.stat(name, dir_fd=fd, follow_symlinks=False)) != stamp(before):
            raise SkillError("capture_conflict", "File replaced during capture; retry", path=rel)
    info = (destination / rel).lstat() if destination is not None else before
    return {
        "path": rel,
        "size": total,
        "sha256": digest.hexdigest(),
        "executable": bool(before.st_mode & 0o111),
        "identity": identity(info),
        "stamp": list(stamp(info)),
    }


def revision(files: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for record in sorted(files, key=lambda f: f["path"]):
        digest.update(
            json.dumps(
                [record["path"], record["sha256"], record["executable"]],
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode()
            + b"\n"
        )
    return digest.hexdigest()


def capture(root: Path, settings: Settings, destination: Path | None = None) -> dict[str, Any]:
    root_identity = identity(root.lstat())
    files: list[dict[str, Any]] = []
    directories: list[dict[str, Any]] = []
    total = 0
    for rel in tree(root, settings.package_files * 2, directories):
        if len(files) >= settings.package_files:
            raise SkillError("package_limit", "Package file count limit exceeded")
        record = file_record(
            root, rel, destination=destination, remaining=settings.package_bytes - total
        )
        files.append(record)
        total += record["size"]
    if destination is not None:
        for directory in sorted(directories, key=lambda d: d["path"]):
            with parent_fd(destination, directory["path"], create=True) as (fd, name):
                try:
                    os.mkdir(name, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                directory["identity"] = identity(os.stat(name, dir_fd=fd, follow_symlinks=False))
    manifest = {
        "revision": revision(files),
        "size": total,
        "files": files,
        "directories": directories,
        "root_identity": identity((destination or root).lstat()),
    }
    if identity(root.lstat()) != root_identity:
        raise SkillError("capture_conflict", "Package root changed during capture")
    if destination is not None:
        verify = capture(root, settings)
        if verify["revision"] != manifest["revision"]:
            raise SkillError("capture_conflict", "Package changed during capture; retry")
        sync_directory(destination)
    return manifest


def same_content(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    if a is None or b is None:
        return a is b
    return all(a[k] == b[k] for k in ("sha256", "size", "executable"))


def current_file(root: Path, rel: str, limit: int) -> dict[str, Any] | None:
    try:
        return file_record(root, rel, remaining=limit)
    except FileNotFoundError:
        return None
    except SkillError as exc:
        # Missing intermediate directories are also absent, not unsafe links.
        if isinstance(exc.__cause__, FileNotFoundError):
            return None
        raise
