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
