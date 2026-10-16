"""Owner-managed, fixed workspace/profile bindings. No credentials enter tool results."""

from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import re
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dev_mcp.config import Settings, bearer_token, private_directory, write_private

PERMISSIONS = frozenset(
    {
        "tasks.read",
        "tasks.write",
        "reminders.read",
        "reminders.write",
        "pauses.read",
        "pauses.write",
        "delegate",
        "worker",
        "skills",
    }
)
EDITOR = frozenset(
    {
        "tasks.read",
        "tasks.write",
        "reminders.read",
        "reminders.write",
        "pauses.read",
        "pauses.write",
    }
)


def identifier(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", value):
        raise ValueError(
            "IDs must start with a lowercase letter and use 1-64 letters/digits/hyphens"
        )
    return value


@dataclass(frozen=True)
class Workspace:
    id: str
    timezone: str = "Europe/Berlin"
    delegation_enabled: bool = False
    execution_profiles: tuple[str, ...] = ()


@dataclass(frozen=True)
class Profile:
    id: str
    workspace: str
    permissions: frozenset[str]


LEGACY = Profile("legacy", "personal", frozenset({"skills"}))


@contextmanager
def owner_lock(settings: Settings) -> Iterator[None]:
    private_directory(settings.config)
    fd = os.open(settings.config / "access.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


class Access:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.workspaces = {name: Workspace(name) for name in ("personal", "work")}
        self.profiles: dict[str, Profile] = {}
        path = settings.config / "access.json"
        if path.exists():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("Access configuration must be a private regular file")
            if path.stat().st_size > 128 * 1024:
                raise ValueError("Access configuration exceeds 128 KiB")
            data = json.loads(path.read_text())
            if data.get("version") != 1:
                raise ValueError("Unsupported access configuration version")
            for item in data["workspaces"]:
                ws = Workspace(**{**item, "execution_profiles": tuple(item["execution_profiles"])})
                identifier(ws.id)
                ZoneInfo(ws.timezone)
                if type(ws.delegation_enabled) is not bool:
                    raise ValueError("delegation_enabled must be boolean")
                for name in ws.execution_profiles:
                    identifier(name)
                self.workspaces[ws.id] = ws
            for item in data["profiles"]:
                self._add_profile(
                    Profile(**{**item, "permissions": frozenset(item["permissions"])})
                )
        if len(self.workspaces) > 32 or len(self.profiles) > 64:
            raise ValueError("At most 32 workspaces and 64 profiles are supported")

    def _add_profile(self, profile: Profile) -> None:
        identifier(profile.id)
        if profile.id == "legacy" or profile.workspace not in self.workspaces:
            raise ValueError("Reserved profile ID or unknown workspace")
        if not profile.permissions <= PERMISSIONS:
            raise ValueError("Unknown profile permission")
        for name in ("tasks", "reminders", "pauses"):
            if f"{name}.write" in profile.permissions and f"{name}.read" not in profile.permissions:
                raise ValueError("Write permissions also require the corresponding read permission")
        if "delegate" in profile.permissions and "tasks.write" not in profile.permissions:
            raise ValueError("Delegation requires tasks.write")
        if "worker" in profile.permissions and profile.permissions != frozenset({"worker"}):
            raise ValueError("Worker profiles must contain only the worker permission")
        self.profiles[profile.id] = profile

    def profile(self, name: str) -> Profile:
        if name == "legacy":
            return LEGACY
        if name not in self.profiles:
            raise ValueError("Unknown profile; configure it through the owner CLI")
        return self.profiles[name]

    def save(self) -> None:
        private_directory(self.settings.config)
        data = {
            "version": 1,
            "workspaces": [dataclasses.asdict(w) for w in self.workspaces.values()],
            "profiles": [
                {"id": p.id, "workspace": p.workspace, "permissions": sorted(p.permissions)}
                for p in self.profiles.values()
            ],
        }
        write_private(self.settings.config / "access.json", json.dumps(data, indent=2).encode())

    def configure_workspace(
        self, name: str, timezone: str, delegation: bool, execution_profiles: list[str]
    ) -> None:
        identifier(name)
        ZoneInfo(timezone)
        for value in execution_profiles:
            identifier(value)
        if name not in self.workspaces and len(self.workspaces) >= 32:
            raise ValueError("Workspace capacity reached")
        self.workspaces[name] = Workspace(name, timezone, delegation, tuple(execution_profiles))
        self.save()

    def create_profile(self, name: str, workspace: str, permissions: list[str]) -> Path:
        if name in self.profiles or len(self.profiles) >= 64:
            raise ValueError("Profile already exists or profile capacity reached")
        self._add_profile(Profile(name, workspace, frozenset(permissions)))
        path = self.token_path(name)
        private_directory(path.parent)
        write_private(path, (secrets.token_urlsafe(48) + "\n").encode())
        self.save()
        return path

    def token_path(self, name: str) -> Path:
        return self.settings.config / "credentials" / (identifier(name) + ".token")

    def token(self, name: str) -> str:
        if name == "legacy":
            return bearer_token(self.settings)
        self.profile(name)
        path = self.token_path(name)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("Profile token must be private and owned by this user")
            token = stream.read(256).strip()
        if len(token) < 32:
            raise ValueError("Invalid profile token")
        return token

    def refresh(self, binding: Profile) -> Access:
        """Revocation/permission changes invalidate existing bindings, including stdio."""
        current = Access(self.settings)
        if current.profile(binding.id) != binding:
            raise ValueError("Connection profile changed; reconnect with an authorized profile")
        return current

    def root(self, workspace: str) -> Path:
        if workspace not in self.workspaces:
            raise ValueError("Unknown workspace")
        return self.settings.state / "workspaces" / identifier(workspace)

    def skill_settings(self, workspace: str) -> Settings:
        if workspace == "personal":
            return self.settings
        root = self.root(workspace)
        # Each non-personal library starts empty and is never linked to personal originals.
        return dataclasses.replace(
            self.settings, state=root / "skills-state", library=root / "library"
        )

    def public(self) -> dict[str, Any]:
        return {
            "workspaces": [dataclasses.asdict(w) for w in self.workspaces.values()],
            "profiles": [
                {"id": p.id, "workspace": p.workspace, "permissions": sorted(p.permissions)}
                for p in self.profiles.values()
            ],
        }
