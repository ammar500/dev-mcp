"""Small, explicit XDG configuration; no client repository is used for runtime state."""

from __future__ import annotations

import json
import os
import secrets
import stat
from dataclasses import asdict, dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    library: Path
    state: Path
    config: Path
    port: int = 8765
    package_bytes: int = 256 * 1024 * 1024
    package_files: int = 4096
    storage_bytes: int = 2 * 1024 * 1024 * 1024
    file_workers: int = 2
    waiting_operations: int = 16

    @classmethod
    def load(cls) -> Settings:
        config = (
            Path(
                os.environ.get(
                    "DEV_MCP_CONFIG",
                    str(
                        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
                        / "dev-mcp"
                    ),
                )
            )
            .expanduser()
            .absolute()
        )
        data = (
            json.loads((config / "settings.json").read_text())
            if (config / "settings.json").is_file()
            else {}
        )
        unknown = set(data) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"Unknown configuration fields: {sorted(unknown)}")
        data["config"] = config
        data["state"] = (
            Path(
                os.environ.get(
                    "DEV_MCP_STATE",
                    data.get(
                        "state",
                        str(
                            Path(
                                os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))
                            )
                            / "dev-mcp"
                        ),
                    ),
                )
            )
            .expanduser()
            .absolute()
        )
        data["library"] = (
            Path(
                os.environ.get(
                    "DEV_MCP_LIBRARY",
                    data.get("library", str(Path(__file__).resolve().parents[2] / "skills")),
                )
            )
            .expanduser()
            .absolute()
        )
        return cls(**data)

    def validate(self) -> None:
        for name in ("package_bytes", "package_files", "storage_bytes", "file_workers"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.waiting_operations) is not int or self.waiting_operations < 0:
            raise ValueError("waiting_operations must be a nonnegative integer")
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        for path in (self.state, self.config):
            if (
                path.resolve() == self.library.resolve()
                or self.library.resolve() in path.resolve().parents
            ):
                raise ValueError(
                    "Runtime state and configuration must be outside the skill library"
                )
            # Reject runtime locations inside any checkout, including Git worktrees.
            if any((p / ".git").exists() for p in (path, *path.parents)):
                raise ValueError("Runtime state and configuration must be outside Git checkouts")

    def initialize(self) -> None:
        self.validate()
        private_directory(self.config)
        private_directory(self.state)
        for name in ("acquisitions", "proposals", "transactions", "manifests", "locks"):
            private_directory(self.state / name)

    def save(self) -> None:
        self.initialize()
        write_private(
            self.config / "settings.json",
            json.dumps(
                {k: str(v) if isinstance(v, Path) else v for k, v in asdict(self).items()}, indent=2
            ).encode(),
        )


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError(f"Expected a directory owned by this user: {path}")
    if path.resolve() != path.absolute():
        raise ValueError(f"Managed directories must not contain symbolic links: {path}")
    path.chmod(0o700)


def write_private(path: Path, data: bytes) -> None:
    """Atomic, durable file replacement with private permissions."""
    temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        sync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def bearer_token(settings: Settings) -> str:
    settings.initialize()
    path = settings.config / "token"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError(
                    "Bearer token must be a private regular file owned by this user"
                ) from None
            token = stream.read(256).strip()
        if len(token) < 32:
            raise ValueError("Invalid bearer token file") from None
        return token
    token = secrets.token_urlsafe(48)
    with os.fdopen(fd, "w") as stream:
        stream.write(token + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path.parent)
    return token
