from __future__ import annotations

import sqlite3

import pytest

from dev_mcp.access import EDITOR, LEGACY, Access, Profile
from dev_mcp.config import Settings
from dev_mcp.tasks import store
from dev_mcp.tasks.common import TaskError


def test_testing_permissions_are_opt_in(settings: Settings) -> None:
    access = Access(settings)
    assert not any(p.startswith("testing.") for p in EDITOR | LEGACY.permissions)
    with pytest.raises(ValueError, match=r"requires testing\.read"):
        access._add_profile(Profile("invalid", "personal", frozenset({"testing.use"})))
    with pytest.raises(ValueError, match="only the worker"):
        access._add_profile(Profile("invalid", "personal", frozenset({"worker", "testing.read"})))
    access.create_profile("tester", "personal", ["testing.read", "testing.use"])
    assert Access(settings).profile("tester").permissions == {"testing.read", "testing.use"}


def test_testing_migration_preserves_v1_and_is_atomic() -> None:
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(store.SCHEMA)
        db.execute("INSERT INTO requests VALUES('p','op','id','{}','{}')")
        db.commit()
        store.migrate(db)
        store.migrate(db)
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM requests").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM testing_tickets").fetchone()[0] == 0
        db.execute("PRAGMA user_version=3")
        with pytest.raises(TaskError, match="Unsupported"):
            store.migrate(db)
    finally:
        db.close()
    db = sqlite3.connect(":memory:")
    try:
        db.executescript(store.SCHEMA)
        db.execute("CREATE TABLE testing_tickets(id TEXT)")
        with pytest.raises(sqlite3.OperationalError):
            store.migrate(db)
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM requests").fetchone()[0] == 0
    finally:
        db.close()
