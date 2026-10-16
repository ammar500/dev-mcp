"""Bounded validation shared by independent task features."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


class TaskError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def require(condition: bool, message: str, code: str = "invalid_arguments") -> None:
    if not condition:
        raise TaskError(code, message)


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def response_bytes(value: Any) -> int:
    """Conservative wire size, including the duplicated JSON text/structured result."""
    return len(
        json.dumps(
            {
                "content": [{"type": "text", "text": json.dumps(value)}],
                "structuredContent": value,
                "isError": False,
            }
        ).encode()
    )


def record_capacity(value: Any) -> None:
    require(
        response_bytes(value) <= 48 * 1024,
        "Combined record exceeds response capacity; shorten text or use managed context",
        "response_limit",
    )


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def text(value: Any, name: str, limit: int = 8192, *, empty: bool = False) -> str:
    require(isinstance(value, str), f"{name} must be text")
    require(empty or bool(value.strip()), f"{name} must not be empty")
    require(len(json.dumps(value).encode()) <= limit, f"{name} exceeds its {limit}-byte JSON limit")
    return str(value)


def checkout(value: Any) -> str | None:
    if value is None:
        return None
    path = Path(text(value, "checkout", 4096)).expanduser()
    require(path.is_absolute() and path.is_dir(), "Checkout must be an existing absolute directory")
    return str(path.resolve())


def instant(value: Any, timezone: str, *, allow_past: bool = True) -> str:
    raw = text(value, "timestamp", 100)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        require(parsed.tzinfo is not None, "Timestamp needs an explicit UTC offset")
        local = parsed.astimezone(ZoneInfo(timezone))
        # An explicitly offset local time must actually exist in the named timezone.
        require(
            raw.endswith(("Z", "+00:00"))
            or (
                local.replace(tzinfo=None) == parsed.replace(tzinfo=None)
                and local.utcoffset() == parsed.utcoffset()
            ),
            "Timestamp offset/local time does not match the named timezone",
        )
        result = parsed.astimezone(UTC)
        require(
            allow_past or result > datetime.now(UTC),
            "Reminder time is in the past; set allow_past=true if intentional",
        )
        return result.isoformat(timespec="microseconds")
    except (ValueError, KeyError) as exc:
        raise TaskError("invalid_arguments", "Invalid timestamp or IANA timezone") from exc


def due(value: Any, timezone: str) -> str | None:
    if value is None:
        return None
    raw = text(value, "due", 100)
    if len(raw) == 10:
        try:
            return date.fromisoformat(raw).isoformat()
        except ValueError as exc:
            raise TaskError("invalid_arguments", "Invalid due date") from exc
    return instant(raw, timezone)


def page(args: dict[str, Any]) -> tuple[int, int]:
    offset, limit = args.get("offset", 0), args.get("limit", 20)
    require(type(offset) is int and offset >= 0, "offset must be nonnegative")
    require(type(limit) is int and 1 <= limit <= 50, "limit must be 1-50")
    return offset, limit


def changed_fields(patch: Any, allowed: set[str]) -> dict[str, Any]:
    require(isinstance(patch, dict) and bool(patch), "patch must be a nonempty object")
    require(set(patch) <= allowed, "Unknown or immutable patch field")
    return dict(patch)
