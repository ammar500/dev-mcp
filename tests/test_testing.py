from __future__ import annotations

import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from dev_mcp.access import EDITOR, Access
from dev_mcp.config import Settings
from dev_mcp.tasks import store
from dev_mcp.tasks.common import TaskError
from dev_mcp.tasks.service import TaskService
from dev_mcp.testing.repository import discover
from tests.test_tasks import call, task


@pytest.fixture
def queue_access(settings: Settings) -> Access:
    settings.initialize()
    a = Access(settings)
    for name, workspace in (("tester", "personal"), ("other", "personal"), ("work", "work")):
        a.create_profile(name, workspace, sorted(EDITOR | {"testing.read", "testing.use"}))
    a.create_profile("reader", "personal", ["testing.read"])
    a.create_profile("worker", "personal", ["worker"])
    return a


@pytest.fixture
def queue(queue_access: Access) -> TaskService:
    return TaskService(queue_access, queue_access.profile("tester"))


def git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def repository(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q")
    (path / "tracked.txt").write_text("unchanged\n")
    git(path, "add", "tracked.txt")
    git(
        path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "initial",
    )
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return repository(tmp_path / "repo")


def join(s: TaskService, repo: Path, **args: Any) -> dict[str, Any]:
    return call(s, "testing_join", source_worktree=str(repo), session_ref="chat", **args)


def check(s: TaskService, ticket: dict[str, Any]) -> dict[str, Any]:
    return call(s, "testing_check", ticket_id=ticket["id"])


def finish(s: TaskService, ticket: dict[str, Any], **args: Any) -> dict[str, Any]:
    return call(s, "testing_done", ticket_id=ticket["id"], **args)


def test_strict_fifo_no_expiry_restart_and_no_poll_receipts(queue: TaskService, repo: Path) -> None:
    tickets = [join(queue, repo) for _ in range(3)]
    assert all(t["status"] == "waiting" and not t["can_test"] for t in tickets)
    db = store.connect(queue.path)
    try:
        with db:
            db.execute("UPDATE testing_tickets SET created='2000-01-01',last_checked='2000-01-01'")
        for _ in range(20):
            assert not check(queue, tickets[2])["can_test"]
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 3
        assert db.execute("SELECT count(*) FROM requests").fetchone()[0] == 3
    finally:
        db.close()
    fresh = TaskService(Access(queue.access.settings), queue.profile)
    assert not check(fresh, tickets[1])["can_test"]
    active = check(fresh, tickets[0])
    assert active["can_test"] and active["poll_after_seconds"] is None
    newer = TaskService(Access(queue.access.settings), queue.profile)
    assert not check(newer, tickets[1])["can_test"]
    with pytest.raises(TaskError, match="cleanup_complete"):
        finish(newer, tickets[0], stack_stopped=True)
    assert not check(newer, tickets[1])["can_test"]
    done = finish(
        newer, tickets[0], stack_stopped=True, cleanup_complete=True, outcome="tests failed"
    )
    assert done["status"] == "done"
    assert not check(newer, tickets[0])["can_test"]
    assert not check(newer, tickets[2])["can_test"]  # Silent next waiter still blocks.
    assert finish(newer, tickets[1])["status"] == "cancelled"
    assert check(newer, tickets[2])["can_test"]
    assert finish(newer, tickets[0])["status"] == "done"


def test_retries_live_checks_and_withdrawal(queue: TaskService, repo: Path) -> None:
    first = join(queue, repo, request_id="enroll")
    assert first == join(queue, repo, request_id="enroll")
    with pytest.raises(TaskError, match="different arguments"):
        join(queue, repo, request_id="enroll", additional_info="changed")
    assert check(queue, first)["can_test"]
    # An enrollment retry cannot replay a permission grant.
    assert not join(queue, repo, request_id="enroll")["can_test"]
    finished = finish(queue, first, request_id="finish", stack_stopped=True, cleanup_complete=True)
    assert finished == finish(
        queue, first, request_id="finish", stack_stopped=True, cleanup_complete=True
    )
    assert not check(queue, first)["can_test"]
    shutil.rmtree(repo)
    assert first == join(queue, repo, request_id="enroll")  # Retry independent of Git availability.


def test_linked_worktrees_and_independent_repos(
    queue: TaskService, repo: Path, tmp_path: Path
) -> None:
    worktree = tmp_path / "linked"
    git(repo, "worktree", "add", "-qb", "test", str(worktree))
    main = join(queue, repo)
    linked = join(queue, worktree)
    independent = join(queue, repository(tmp_path / "other-repo"))
    assert main["repository"] == linked["repository"]
    assert linked["testing_checkout"] == str(repo)
    assert check(queue, independent)["can_test"]
    assert not check(queue, linked)["can_test"]
    finish(queue, main)
    assert check(queue, linked)["can_test"]
    assert (repo / "tracked.txt").read_text() == "unchanged\n"
    assert (worktree / "tracked.txt").read_text() == "unchanged\n"
    assert subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"]) == b""
    assert subprocess.check_output(["git", "-C", str(worktree), "status", "--porcelain"]) == b""


def test_concurrent_check_and_join_across_workspaces(
    queue: TaskService, queue_access: Access, repo: Path
) -> None:
    work = TaskService(queue_access, queue_access.profile("work"))
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(join, s, repo) for s in (queue, work)]
        tickets = [f.result() for f in futures]
        assert tickets[0]["sequence"] != tickets[1]["sequence"]
        futures = [pool.submit(check, s, t) for s, t in zip((queue, work), tickets, strict=True)]
        results = [f.result() for f in futures]
    assert sum(t["can_test"] for t in results) == 1
    for s, own, foreign in ((queue, tickets[0], tickets[1]), (work, tickets[1], tickets[0])):
        assert [t["id"] for t in call(s, "testing_list")["items"]] == [own["id"]]
        with pytest.raises(TaskError, match="not found"):
            check(s, foreign)
        with pytest.raises(TaskError, match="not found"):
            finish(s, foreign)
        with pytest.raises(TaskError, match="not found"):
            call(s, "testing_list", ticket_id=foreign["id"])
        assert foreign["id"] not in json.dumps(check(s, own))


def test_permissions_owner_and_revocation(
    queue: TaskService, queue_access: Access, repo: Path
) -> None:
    ticket = join(queue, repo)
    other = TaskService(queue_access, queue_access.profile("other"))
    reader = TaskService(queue_access, queue_access.profile("reader"))
    assert call(reader, "testing_list", ticket_id=ticket["id"])["id"] == ticket["id"]
    with pytest.raises(TaskError, match="another profile"):
        check(other, ticket)
    with pytest.raises(TaskError, match="another profile"):
        finish(other, ticket)
    for name in ("reader", "worker"):
        service = TaskService(queue_access, queue_access.profile(name))
        with pytest.raises(TaskError, match="not permitted"):
            check(service, ticket)
    del queue_access.profiles["tester"]
    queue_access.save()
    with pytest.raises(TaskError, match="revoked"):
        check(queue, ticket)


def test_checkout_reservations_and_linked_attempt(
    queue: TaskService, queue_access: Access, repo: Path
) -> None:
    t = task(queue, checkout=str(repo))
    attempt = call(queue, "tasks_claim", task_id=t["id"], expected_version=1, session_ref="chat")
    unlinked = join(queue, repo)
    assert not check(queue, unlinked)["can_test"]
    finish(queue, unlinked)
    with pytest.raises(TaskError, match="linked task"):
        join(queue, repo, attempt_id=attempt["id"])
    other = TaskService(queue_access, queue_access.profile("other"))
    with pytest.raises(TaskError, match="another profile"):
        join(other, repo, task_id=t["id"], attempt_id=attempt["id"])
    ticket = join(queue, repo, task_id=t["id"], attempt_id=attempt["id"])
    assert check(queue, ticket)["can_test"]
    call(queue, "tasks_report", attempt_id=attempt["id"], expected_version=1, action="complete")
    foreign = TaskService(queue_access, queue_access.profile("work"))
    new_task = task(foreign, checkout=str(repo))
    with pytest.raises(TaskError, match="Checkout overlaps"):
        call(
            foreign,
            "tasks_claim",
            task_id=new_task["id"],
            expected_version=1,
            session_ref="foreign",
        )
    finish(queue, ticket, stack_stopped=True, cleanup_complete=True)
    assert call(
        foreign, "tasks_claim", task_id=new_task["id"], expected_version=1, session_ref="foreign"
    )


def test_missing_worktree_does_not_skip_or_block_release(
    queue: TaskService, repo: Path, tmp_path: Path
) -> None:
    worktree = tmp_path / "linked"
    git(repo, "worktree", "add", "-qb", "test", str(worktree))
    gone = join(queue, worktree)
    next_ticket = join(queue, repo)
    shutil.rmtree(worktree)
    assert not check(queue, gone)["can_test"]
    assert not check(queue, next_ticket)["can_test"]
    assert (
        call(queue, "testing_list", source_worktree=str(worktree))["items"][0]["id"] == gone["id"]
    )
    finish(queue, gone)
    assert check(queue, next_ticket)["can_test"]


def test_list_pagination_limits_and_recovery(queue: TaskService, repo: Path) -> None:
    ids = [join(queue, repo)["id"] for _ in range(23)]
    first = call(queue, "testing_list", repository=str(repo / ".git"), session_ref="chat")
    assert len(first["items"]) == 20 and first["next_offset"] == 20
    assert [t["id"] for t in call(queue, "testing_list", offset=20)["items"]] == ids[20:]
    with pytest.raises(TaskError, match="1-50"):
        call(queue, "testing_list", limit=51)
    with pytest.raises(TaskError):
        join(queue, repo, additional_info="x" * 10000)


def test_repository_validation_and_git_environment(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "wrong"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path))
    assert discover(str(repo))["testing_checkout"] == str(repo)
    alias = tmp_path / "alias"
    alias.symlink_to(repo, target_is_directory=True)
    assert discover(str(alias))["repository"] == str(repo / ".git")
    (repo / "nested").mkdir()
    with pytest.raises(TaskError, match="checkout root"):
        discover(str(repo / "nested"))
    with pytest.raises(TaskError):
        discover(str(tmp_path))


def test_restore_holders_require_owner_recovery(
    queue: TaskService, queue_access: Access, repo: Path, tmp_path: Path
) -> None:
    from dev_mcp.tasks.maintenance import backup, restore
    from dev_mcp.testing.maintenance import recover

    holder, waiter = join(queue, repo), join(queue, repo)
    assert check(queue, holder)["can_test"]
    saved = tmp_path / "backup"
    backup(queue_access, "personal", saved)
    # Simulate preserving current state outside the deployment and restoring a snapshot.
    queue_access.root("personal").rename(tmp_path / "preserved")
    restore(queue_access, "personal", saved, confirmed_stopped=True)
    fresh = TaskService(queue_access, queue_access.profile("tester"))
    restored = check(fresh, holder)
    assert restored["status"] == "recovery_required" and not restored["can_test"]
    assert not check(fresh, waiter)["can_test"]
    with pytest.raises(TaskError, match="owner recovery"):
        finish(fresh, holder, stack_stopped=True, cleanup_complete=True)
    other = TaskService(queue_access, queue_access.profile("work"))
    t = task(other, checkout=str(repo))
    with pytest.raises(TaskError, match="Checkout overlaps"):
        call(other, "tasks_claim", task_id=t["id"], expected_version=1, session_ref="work")
    for flags in ({}, {"stack_stopped": True}, {"cleanup_complete": True}):
        with pytest.raises(TaskError, match="confirm both"):
            recover(queue_access, "personal", holder["id"], reason="inspected", **flags)
    assert (
        recover(
            queue_access,
            "personal",
            holder["id"],
            reason="Manually stopped and cleaned",
            stack_stopped=True,
            cleanup_complete=True,
        )["status"]
        == "reconciled"
    )
    assert check(fresh, waiter)["can_test"]
    db = store.connect(fresh.path)
    try:
        audit = db.execute(
            "SELECT * FROM events WHERE entity_id=? AND kind='testing.owner_reconciled'",
            (holder["id"],),
        ).fetchone()
        assert audit["actor"].startswith("local-owner:")
        assert json.loads(audit["details"])["reason"] == "Manually stopped and cleaned"
    finally:
        db.close()


def test_recovery_withdraws_silent_waiter_and_is_idempotent(
    queue: TaskService, queue_access: Access, repo: Path
) -> None:
    from dev_mcp.testing.maintenance import recover

    head, successor = join(queue, repo), join(queue, repo)
    with pytest.raises(TaskError, match="not be empty"):
        recover(queue_access, "personal", head["id"], reason="")
    result = recover(queue_access, "personal", head["id"], reason="Abandoned session")
    assert result == recover(queue_access, "personal", head["id"], reason="Abandoned session")
    assert check(queue, successor)["can_test"]
    assert not check(queue, head)["can_test"]


def test_task_claim_and_testing_grant_race(
    queue: TaskService, queue_access: Access, repo: Path
) -> None:
    other = TaskService(queue_access, queue_access.profile("work"))
    ticket = join(queue, repo)
    t = task(other, checkout=str(repo))

    def claim() -> bool:
        try:
            call(other, "tasks_claim", task_id=t["id"], expected_version=1, session_ref="other")
            return True
        except TaskError as exc:
            assert exc.code == "checkout_busy"
            return False

    with ThreadPoolExecutor(2) as pool:
        grant = pool.submit(check, queue, ticket)
        claimed = pool.submit(claim)
    assert int(grant.result()["can_test"]) + int(claimed.result()) == 1


def test_done_check_race_never_leaves_two_holders(queue: TaskService, repo: Path) -> None:
    first, second = join(queue, repo), join(queue, repo)
    assert check(queue, first)["can_test"]
    with ThreadPoolExecutor(2) as pool:
        done = pool.submit(finish, queue, first, stack_stopped=True, cleanup_complete=True)
        granted = pool.submit(check, queue, second)
    assert done.result()["status"] == "done"
    assert granted.result()["status"] in {"waiting", "active"}
    assert check(queue, second)["can_test"]
    assert not check(queue, first)["can_test"]


def test_restore_version_one_backup_migrates_without_losing_tasks(
    queue: TaskService, queue_access: Access, tmp_path: Path
) -> None:
    from dev_mcp.tasks.maintenance import backup, restore

    original = task(queue)
    saved = tmp_path / "version-one-backup"
    backup(queue_access, "personal", saved)
    db = store.connect(saved / "tasks.sqlite3")
    try:
        db.executescript("DROP TABLE testing_tickets; PRAGMA user_version=1;")
    finally:
        db.close()
    queue_access.root("personal").rename(tmp_path / "preserved-current")
    restore(queue_access, "personal", saved, confirmed_stopped=True)
    fresh = TaskService(queue_access, queue_access.profile("tester"))
    assert call(fresh, "tasks_get", task_id=original["id"])["title"] == original["title"]
    assert call(fresh, "testing_list")["items"] == []


def test_repository_bare_rejected_and_separate_git_directory(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    git(bare, "init", "--bare", "-q")
    with pytest.raises(TaskError):
        discover(str(bare))
    main = tmp_path / "main"
    main.mkdir()
    common = tmp_path / "metadata"
    git(main, "init", "-q", "--separate-git-dir", str(common))
    assert discover(str(main)) == {
        "repository": str(common),
        "source_worktree": str(main),
        "testing_checkout": str(main),
    }


def test_head_linked_to_finished_attempt_does_not_skip(queue: TaskService, repo: Path) -> None:
    t = task(queue, checkout=str(repo))
    attempt = call(queue, "tasks_claim", task_id=t["id"], expected_version=1, session_ref="chat")
    head = join(queue, repo, task_id=t["id"], attempt_id=attempt["id"])
    next_ticket = join(queue, repo)
    call(queue, "tasks_report", attempt_id=attempt["id"], expected_version=1, action="complete")
    blocked = check(queue, head)
    assert not blocked["can_test"] and blocked["waiting_reason"] == "conflict"
    assert not check(queue, next_ticket)["can_test"]
    finish(queue, head)
    assert check(queue, next_ticket)["can_test"]
