from __future__ import annotations

import os
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from dev_mcp.access import EDITOR, Access
from dev_mcp.config import Settings
from dev_mcp.tasks import store
from dev_mcp.tasks.common import TaskError
from dev_mcp.tasks.maintenance import backup, reconcile, restore
from dev_mcp.tasks.service import OPERATIONS, TaskService


@pytest.fixture
def access(settings: Settings) -> Access:
    settings.initialize()
    a = Access(settings)
    a.configure_workspace("personal", "Europe/Berlin", True, ["fake"])
    a.create_profile("personal-editor", "personal", sorted(EDITOR | {"delegate", "skills"}))
    a.create_profile("work-editor", "work", sorted(EDITOR | {"delegate", "skills"}))
    a.create_profile("reader", "personal", ["tasks.read", "pauses.read", "reminders.read"])
    a.create_profile("worker", "personal", ["worker"])
    return a


@pytest.fixture
def tasks(access: Access) -> TaskService:
    return TaskService(access, access.profile("personal-editor"))


def call(s: TaskService, op: str, **args: Any) -> dict[str, Any]:
    if op in OPERATIONS and OPERATIONS[op][1]:
        args.setdefault("request_id", str(uuid.uuid4()))
    return s.call(op, args)


def task(s: TaskService, **args: Any) -> dict[str, Any]:
    return call(s, "tasks_create", title="A task", **args)


def test_crud_retries_versions_and_boundaries(tasks: TaskService, access: Access) -> None:
    t = call(tasks, "tasks_create", request_id="retry", title="Personal secret")
    assert t == call(tasks, "tasks_create", request_id="retry", title="Personal secret")
    with pytest.raises(TaskError, match="different arguments"):
        call(tasks, "tasks_create", request_id="retry", title="Changed")
    other = TaskService(access, access.profile("work-editor"))
    assert call(other, "tasks_list")["items"] == []
    for view in ("task", "history", "attempts", "delegations"):
        with pytest.raises(TaskError, match="not found"):
            call(other, "tasks_get", task_id=t["id"], view=view)
    reader = TaskService(access, access.profile("reader"))
    with pytest.raises(TaskError, match="not permitted"):
        task(reader)
    edited = call(
        tasks,
        "tasks_update",
        task_id=t["id"],
        expected_version=1,
        patch={"instructions": "Do this", "priority": 3},
    )
    assert edited["version"] == 2
    with pytest.raises(TaskError, match="Version conflict"):
        call(tasks, "tasks_update", task_id=t["id"], expected_version=1, patch={"title": "lost"})
    assert call(tasks, "tasks_list", query="do this")["items"][0]["id"] == t["id"]
    completed = call(
        tasks, "tasks_transition", task_id=t["id"], expected_version=2, action="complete"
    )
    assert completed["status"] == "done"
    call(tasks, "tasks_archive", task_id=t["id"], expected_version=3, archived=True)
    assert call(tasks, "tasks_list")["items"] == []
    call(tasks, "tasks_archive", task_id=t["id"], expected_version=4, archived=False)
    call(tasks, "tasks_transition", task_id=t["id"], expected_version=5, action="reopen")
    assert call(tasks, "tasks_get", task_id=t["id"])["status"] == "open"
    assert call(tasks, "tasks_get", task_id=t["id"], view="history")["items"]
    fresh = TaskService(Access(access.settings), access.profile("personal-editor"))
    assert call(fresh, "tasks_create", request_id="retry", title="Personal secret") == t


def test_reminders_queries_dst_and_task_lifecycle(tasks: TaskService) -> None:
    t = task(tasks)
    with pytest.raises(TaskError, match="past"):
        call(tasks, "reminders_create", task_id=t["id"], remind_at="2000-01-01T10:00:00+01:00")
    for timestamp in ("2040-01-01T10:00:00", "2040-03-25T02:30:00+01:00"):
        with pytest.raises(TaskError):
            call(tasks, "reminders_create", task_id=t["id"], remind_at=timestamp)
    r = call(
        tasks,
        "reminders_create",
        task_id=t["id"],
        remind_at="2000-01-01T10:00:00Z",
        allow_past=True,
    )
    future = call(tasks, "reminders_create", task_id=t["id"], remind_at="2099-01-01T10:00:00+01:00")
    assert [x["id"] for x in call(tasks, "reminders_list", window="overdue")["items"]] == [r["id"]]
    assert [x["id"] for x in call(tasks, "reminders_list")["items"]] == [future["id"]]
    call(
        tasks,
        "reminders_update",
        reminder_id=r["id"],
        expected_version=1,
        patch={"status": "acknowledged"},
    )
    assert call(tasks, "tasks_get", task_id=t["id"])["status"] == "open"
    call(tasks, "tasks_transition", task_id=t["id"], expected_version=1, action="complete")
    assert call(tasks, "reminders_list", window="all")["items"] == []
    call(tasks, "tasks_transition", task_id=t["id"], expected_version=2, action="reopen")
    assert call(tasks, "reminders_list", window="all")["items"] == []
    assert len(call(tasks, "reminders_list", window="all", status="all")["items"]) == 2


def test_pause_raw_no_ai_or_process_and_context(
    tasks: TaskService, access: Access, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = task(tasks)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-read")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-read")
    original_getenv = os.getenv

    def guarded_env(key: str, *args: Any) -> Any:
        assert "API_KEY" not in key
        return original_getenv(key, *args)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Pause operation launched a process")

    monkeypatch.setattr(os, "getenv", guarded_env)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    raw = "  paused here\n\n❄ original\ttext\r\n"
    first = call(
        tasks,
        "pauses_save",
        raw_text=raw,
        task_id=t["id"],
        checkout=str(tmp_path),
        session_ref="chat1",
    )
    assert call(tasks, "pauses_find", pause_id=first["id"])["raw_text"] == raw
    assert call(tasks, "tasks_get", task_id=t["id"])["version"] == 1
    second = call(
        tasks, "pauses_save", raw_text="corrected", supersedes=first["id"], task_id=t["id"]
    )
    assert call(tasks, "pauses_find", task_id=t["id"])["items"][0]["id"] == second["id"]
    assert len(call(tasks, "pauses_find", unresolved_only=False)["items"]) == 2
    with pytest.raises(TaskError, match="immutable"):
        call(
            tasks,
            "pauses_update",
            pause_id=first["id"],
            expected_version=1,
            patch={"raw_text": "rewrite"},
        )
    call(tasks, "pauses_update", pause_id=second["id"], expected_version=1, patch={"resumed": True})
    assert call(tasks, "pauses_find")["items"] == []
    work = TaskService(access, access.profile("work-editor"))
    with pytest.raises(TaskError, match="not found"):
        call(work, "pauses_find", pause_id=first["id"])
    with pytest.raises(TaskError, match="not found"):
        call(work, "pauses_save", raw_text="bad", supersedes=first["id"])
    call(tasks, "tasks_archive", task_id=t["id"], expected_version=1, archived=True)
    assert call(tasks, "pauses_find", pause_id=first["id"])["raw_text"] == raw
    assert (
        call(
            TaskService(access, access.profile("personal-editor")),
            "pauses_find",
            pause_id=first["id"],
        )["raw_text"]
        == raw
    )


def test_claim_exclusion_checkout_overlap_and_recovery(
    tasks: TaskService, access: Access, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    (repo / "nested").mkdir(parents=True)
    t = task(tasks, checkout=str(repo))
    with ThreadPoolExecutor(2) as pool:

        def attempt() -> dict[str, Any] | str:
            try:
                return call(
                    tasks, "tasks_claim", task_id=t["id"], expected_version=1, session_ref="chat"
                )
            except TaskError:
                return "rejected"

        results = list(pool.map(lambda _: attempt(), range(2)))
    assert results.count("rejected") == 1
    owned = next(x for x in results if isinstance(x, dict))
    work = TaskService(access, access.profile("work-editor"))
    other = task(work, checkout=str(repo / "nested"))
    with pytest.raises(TaskError, match="overlaps"):
        call(work, "tasks_claim", task_id=other["id"], expected_version=1, session_ref="other")
    for op, extra in [
        ("tasks_update", {"patch": {"title": "changed"}}),
        ("tasks_transition", {"action": "complete"}),
        ("tasks_archive", {"archived": True}),
    ]:
        with pytest.raises(TaskError, match="execution owns"):
            call(tasks, op, task_id=t["id"], expected_version=2, **extra)
    with store.connect(tasks.path) as db:
        db.execute("UPDATE attempts SET expires='2000' WHERE id=?", (owned["id"],))
    assert call(tasks, "tasks_get", task_id=t["id"])["execution"]["status"] == "uncertain"
    with pytest.raises(TaskError):
        call(tasks, "tasks_report", attempt_id=owned["id"], expected_version=2, action="complete")
    with pytest.raises(TaskError):
        reconcile(access, "personal", owned["id"], confirmed_stopped=False)
    reconcile(access, "personal", owned["id"], confirmed_stopped=True)
    next_attempt = call(
        work, "tasks_claim", task_id=other["id"], expected_version=1, session_ref="other"
    )
    call(work, "tasks_report", attempt_id=next_attempt["id"], expected_version=1, action="complete")
    assert list(repo.iterdir()) == [repo / "nested"]


def test_delegation_fake_worker_frozen_cancel_and_global_limit(
    tasks: TaskService, access: Access
) -> None:
    worker = TaskService(access, access.profile("worker"))
    t = task(
        tasks,
        instructions="Frozen instructions",
        context=[{"kind": "snapshot", "value": "Evidence"}],
    )
    queued = call(
        tasks, "tasks_delegate", task_id=t["id"], expected_version=1, execution_profile="fake"
    )
    assert not queued["agent_started"]
    with pytest.raises(TaskError, match="queued delegation"):
        call(
            tasks,
            "tasks_update",
            task_id=t["id"],
            expected_version=2,
            patch={"instructions": "new"},
        )
    claimed = call(worker, "worker_claim", execution_profiles=["fake"], session_ref="fake-session")
    assert claimed["snapshot"]["instructions"] == "Frozen instructions"
    second = task(tasks)
    call(
        tasks, "tasks_delegate", task_id=second["id"], expected_version=1, execution_profile="fake"
    )
    with pytest.raises(TaskError, match="slot"):
        call(worker, "worker_claim", execution_profiles=["fake"], session_ref="second")
    current = call(tasks, "tasks_get", task_id=t["id"], view="delegations")["items"][0]
    cancelled = call(
        tasks,
        "delegations_cancel",
        delegation_id=current["id"],
        expected_version=current["version"],
    )
    assert cancelled["status"] == "cancel_requested"
    beat = call(worker, "worker_heartbeat", attempt_id=claimed["id"])
    assert beat["status"] == "cancel_requested"
    with pytest.raises(TaskError, match="Confirm cancellation"):
        call(
            worker,
            "worker_finish",
            attempt_id=claimed["id"],
            expected_version=beat["version"],
            action="complete",
        )
    call(
        worker,
        "worker_finish",
        attempt_id=claimed["id"],
        expected_version=beat["version"],
        action="cancel",
    )
    next_run = call(worker, "worker_claim", execution_profiles=["fake"], session_ref="second")
    call(
        worker,
        "worker_finish",
        attempt_id=next_run["id"],
        expected_version=1,
        action="complete",
        result={"summary": "Done"},
    )
    assert call(tasks, "tasks_get", task_id=second["id"])["status"] == "done"
    work = TaskService(access, access.profile("work-editor"))
    w = task(work)
    with pytest.raises(TaskError, match="disabled"):
        call(work, "tasks_delegate", task_id=w["id"], expected_version=1, execution_profile="fake")
    assert (
        call(worker, "worker_claim", execution_profiles=["fake"], session_ref="idle")["status"]
        == "idle"
    )


def test_future_priority_and_queued_cancel(tasks: TaskService, access: Access) -> None:
    worker = TaskService(access, access.profile("worker"))
    future = task(tasks, priority=9)
    call(
        tasks,
        "tasks_delegate",
        task_id=future["id"],
        expected_version=1,
        execution_profile="fake",
        not_before="2099-01-01T00:00:00Z",
    )
    low, high = task(tasks, priority=0), task(tasks, priority=8)
    low_queue = call(
        tasks, "tasks_delegate", task_id=low["id"], expected_version=1, execution_profile="fake"
    )
    call(tasks, "tasks_delegate", task_id=high["id"], expected_version=1, execution_profile="fake")
    run = call(worker, "worker_claim", execution_profiles=["fake"], session_ref="fake")
    assert run["task_id"] == high["id"]
    call(worker, "worker_finish", attempt_id=run["id"], expected_version=1, action="fail")
    call(tasks, "delegations_cancel", delegation_id=low_queue["id"], expected_version=1)
    assert call(tasks, "tasks_get", task_id=low["id"])["status"] == "open"
    assert (
        call(worker, "worker_claim", execution_profiles=["fake"], session_ref="idle")["status"]
        == "idle"
    )


def test_pagination_and_argument_bounds(tasks: TaskService) -> None:
    for _ in range(6):
        call(tasks, "pauses_save", raw_text="x" * 15000)
    result = call(tasks, "pauses_find", limit=50)
    assert len(result["items"]) == 1 and result["next_offset"] == 1
    assert len(call(tasks, "pauses_find", offset=1)["items"]) == 1
    with pytest.raises(TaskError):
        call(tasks, "pauses_save", raw_text="x" * 65000)
    with pytest.raises(TaskError):
        call(tasks, "tasks_list", limit=51)
    with pytest.raises(TaskError):
        call(
            tasks,
            "tasks_update",
            task_id=task(tasks)["id"],
            expected_version=1,
            patch={"workspace": "work"},
        )


def test_workspace_backup_restore_preserves_history_and_uncertainty(
    tasks: TaskService, access: Access, tmp_path: Path
) -> None:
    t = task(tasks)
    run = call(tasks, "tasks_claim", task_id=t["id"], expected_version=1, session_ref="chat")
    pause = call(tasks, "pauses_save", raw_text="original")
    root = access.root("personal")
    (root / "context" / "example.txt").write_text("context")
    dest = tmp_path / "backup"
    backup(access, "personal", dest)
    with pytest.raises(TaskError, match="same workspace"):
        restore(access, "work", dest, confirmed_stopped=True)
    with pytest.raises(TaskError, match="absent"):
        restore(access, "personal", dest, confirmed_stopped=True)
    root.rename(root.with_name("personal-preserved"))
    restore(access, "personal", dest, confirmed_stopped=True)
    restored = TaskService(access, access.profile("personal-editor"))
    assert call(restored, "pauses_find", pause_id=pause["id"])["raw_text"] == "original"
    assert call(restored, "tasks_get", task_id=t["id"])["execution"]["status"] == "uncertain"
    assert (root / "context" / "example.txt").read_text() == "context"
    reconcile(access, "personal", run["id"], confirmed_stopped=True)


def test_managed_context_isolation_capture_and_restore(
    tasks: TaskService, access: Access, tmp_path: Path
) -> None:
    from dev_mcp.tasks.context import import_file

    source = tmp_path / "input.txt"
    source.write_text("frozen context " * 1000)
    imported = import_file(access, "personal", source)
    t = task(tasks, context=[{"kind": "managed", "value": imported["id"]}])
    source.write_text("later edit")
    preview = call(tasks, "tasks_get", task_id=t["id"], view="context")
    assert preview["text"].startswith("frozen context")
    assert preview["next_offset"] == 2048
    call(tasks, "tasks_delegate", task_id=t["id"], expected_version=1, execution_profile="fake")
    worker = TaskService(access, access.profile("worker"))
    attempt = call(
        worker, "worker_claim", execution_profiles=["fake"], session_ref="context-reader"
    )
    heartbeat = call(worker, "worker_heartbeat", attempt_id=attempt["id"], context_index=0)
    assert heartbeat["context_file"]["text"] == preview["text"]
    call(
        worker,
        "worker_finish",
        attempt_id=attempt["id"],
        expected_version=heartbeat["version"],
        action="release",
    )
    other = TaskService(access, access.profile("work-editor"))
    with pytest.raises(TaskError, match="not found"):
        task(other, context=[{"kind": "managed", "value": imported["id"]}])
    with pytest.raises(TaskError):
        task(tasks, context=[{"kind": "managed", "value": "../../outside"}])
    link = tmp_path / "link"
    link.symlink_to(source)
    with pytest.raises(OSError):
        import_file(access, "personal", link)
    destination = tmp_path / "snapshot"
    backup(access, "personal", destination)
    root = access.root("personal")
    root.rename(root.with_name("personal-old"))
    restore(access, "personal", destination, confirmed_stopped=True)
    assert call(tasks, "tasks_get", task_id=t["id"], view="context")["text"] == preview["text"]
    Path(preview["path"]).write_text("external edit")
    with pytest.raises(TaskError, match="externally changed"):
        call(tasks, "tasks_get", task_id=t["id"], view="context")


def test_delegation_competes_with_external_claim(tasks: TaskService) -> None:
    t = task(tasks)

    def contender(op: str) -> str:
        arguments = (
            {"session_ref": "existing"} if op == "tasks_claim" else {"execution_profile": "fake"}
        )
        try:
            call(tasks, op, task_id=t["id"], expected_version=1, **arguments)
            return "won"
        except TaskError:
            return "lost"

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(contender, ["tasks_claim", "tasks_delegate"]))
    assert sorted(outcomes) == ["lost", "won"]
    db = store.connect(tasks.path)
    try:
        total = db.execute("SELECT count(*) FROM attempts").fetchone()[0]
        total += db.execute("SELECT count(*) FROM delegations").fetchone()[0]
        assert total == 1
    finally:
        db.close()


def test_reminder_dst_fold_reschedule_archive_and_permissions(
    tasks: TaskService, access: Access
) -> None:
    from dev_mcp.tasks.common import instant

    assert instant("2026-10-25T02:30:00+02:00", "Europe/Berlin") != instant(
        "2026-10-25T02:30:00+01:00", "Europe/Berlin"
    )
    assert instant("2026-10-25T02:30:00Z", "Europe/Berlin") == instant(
        "2026-10-25T02:30:00+00:00", "Europe/Berlin"
    )
    t = task(tasks)
    r = call(tasks, "reminders_create", task_id=t["id"], remind_at="2099-01-01T12:00:00Z")
    r = call(
        tasks,
        "reminders_update",
        reminder_id=r["id"],
        expected_version=1,
        patch={"remind_at": "2099-03-01T12:00:00+01:00"},
    )
    assert r["remind_at"].startswith("2099-03-01T11:")
    other = TaskService(access, access.profile("work-editor"))
    with pytest.raises(TaskError):
        call(
            other,
            "reminders_update",
            reminder_id=r["id"],
            expected_version=2,
            patch={"status": "acknowledged"},
        )
    call(tasks, "tasks_archive", task_id=t["id"], expected_version=1)
    assert call(tasks, "reminders_list")["items"] == []
    call(tasks, "tasks_archive", task_id=t["id"], expected_version=2, archived=False)
    assert call(tasks, "reminders_list")["items"] == []
    assert call(tasks, "reminders_list", status="cancelled")["items"][0]["id"] == r["id"]


def test_context_copy_does_not_hold_task_lock(
    tasks: TaskService, access: Access, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dev_mcp.tasks import context

    source = tmp_path / "context"
    source.write_bytes(b"a" * (1024 * 1024 + 1))
    original_sync = context.sync_directory
    observations = []

    def observe(path: Path) -> None:
        # Run a task mutation from another thread while an import is still in flight.
        with ThreadPoolExecutor(1) as pool:
            observations.append(pool.submit(task, tasks).result(timeout=3))
        original_sync(path)

    monkeypatch.setattr(context, "sync_directory", observe)
    context.import_file(access, "personal", source)
    assert len(observations) == 1


def test_worker_skips_reserved_checkout_and_cannot_report_other_owner(
    tasks: TaskService, access: Access, tmp_path: Path
) -> None:
    worker = TaskService(access, access.profile("worker"))
    external = task(tasks, checkout=str(tmp_path))
    run = call(tasks, "tasks_claim", task_id=external["id"], expected_version=1, session_ref="chat")
    blocked = task(tasks, checkout=str(tmp_path), priority=9)
    available = task(tasks, priority=0)
    for t in (blocked, available):
        call(tasks, "tasks_delegate", task_id=t["id"], expected_version=1, execution_profile="fake")
    claimed = call(worker, "worker_claim", execution_profiles=["fake"], session_ref="test")
    assert claimed["task_id"] == available["id"] and claimed["execution_profile"] == "fake"
    with pytest.raises(TaskError, match="another profile"):
        call(worker, "worker_finish", attempt_id=run["id"], expected_version=1, action="complete")
    with pytest.raises(TaskError, match="another profile"):
        call(tasks, "tasks_report", attempt_id=claimed["id"], expected_version=1, action="release")


def test_due_date_filter_includes_local_day(tasks: TaskService) -> None:
    one = task(tasks, due="2026-10-16")
    two = task(tasks, due="2026-10-16T23:30:00+02:00")
    task(tasks, due="2026-10-17T00:30:00+02:00")
    found = call(tasks, "tasks_list", due_before="2026-10-16")
    assert {row["id"] for row in found["items"]} == {one["id"], two["id"]}
