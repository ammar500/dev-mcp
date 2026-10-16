from __future__ import annotations

import dataclasses
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from dev_mcp.config import Settings
from dev_mcp.skills import files
from dev_mcp.skills.files import SkillError, capture
from dev_mcp.skills.service import SkillService
from dev_mcp.storage import connect


def call(service: SkillService, name: str, **args: Any) -> dict[str, Any]:
    if name in {"session_open", "acquire", "update_prepare", "update_apply"}:
        args.setdefault("request_id", str(uuid.uuid4()))
    return service.call("skills_" + name, args)


def borrow(service: SkillService, tmp_path: Path) -> dict[str, Any]:
    session = call(service, "session_open", client="test", checkout=str(tmp_path), task="task")
    return call(service, "acquire", session_id=session["session_id"], skill_id="example")


def prepare(
    service: SkillService, acquisition: dict[str, Any], changes: list[dict[str, str]], **args: Any
) -> dict[str, Any]:
    return call(
        service,
        "update_prepare",
        acquisition_id=acquisition["acquisition_id"],
        changes=changes,
        human_instruction="Please update my original example skill.",
        reason="Human-requested improvement",
        **args,
    )


def test_empty_library_and_validation(service: SkillService) -> None:
    assert call(service, "search")["skills"] == []
    with pytest.raises(SkillError, match="limit"):
        call(service, "search", limit=51)
    with pytest.raises(SkillError):
        call(service, "inspect", skill_id="../nope")


def test_complete_package_and_retry(service: SkillService, package: Path, tmp_path: Path) -> None:
    result = call(service, "search", query="development")
    assert result["skills"][0]["skill_id"] == "example"
    inspect = call(service, "inspect", skill_id="example")
    assert inspect["file_count"] == 3
    assert inspect["declared_prerequisites"]["compatibility"] == "Python 3.12"
    session = call(
        service,
        "session_open",
        request_id="session",
        client="test",
        checkout=str(tmp_path),
        task="task",
    )
    assert session == call(
        service,
        "session_open",
        request_id="session",
        client="test",
        checkout=str(tmp_path / "."),
        task="task",
    )
    with pytest.raises(SkillError, match="different"):
        call(
            service,
            "session_open",
            request_id="session",
            client="other",
            checkout=str(tmp_path),
            task="task",
        )
    first = call(
        service, "acquire", request_id="copy", session_id=session["session_id"], skill_id="example"
    )
    assert first == call(
        service, "acquire", request_id="copy", session_id=session["session_id"], skill_id="example"
    )
    second = call(service, "acquire", session_id=session["session_id"], skill_id="example")
    one, two = Path(first["directory"]), Path(second["directory"])
    assert one != two
    assert (one / "scripts/run.py").stat().st_mode & 0o111
    assert (one / "SKILL.md").stat().st_ino != (package / "SKILL.md").stat().st_ino
    assert (one / "assets/data.bin").read_bytes() == (package / "assets/data.bin").read_bytes()
    (package / "SKILL.md").write_text((package / "SKILL.md").read_text() + "New\n")
    assert "New" not in (one / "SKILL.md").read_text()
    assert (
        call(service, "release", session_id=session["session_id"], close_session=True)[
            "session_status"
        ]
        == "closed"
    )
    assert not one.exists() and not two.exists()
    assert call(service, "release", acquisition_id=first["acquisition_id"])["remaining"] == 0


def test_recovery_lookup_and_safe_release(
    settings: Settings, service: SkillService, package: Path, tmp_path: Path
) -> None:
    acquisition = borrow(service, tmp_path)
    root = Path(acquisition["directory"])
    (root / "SKILL.md").write_text("edited")
    # Identical bytes at an externally replaced inode must still be protected.
    temp = root / "replacement"
    temp.write_bytes((root / "assets/data.bin").read_bytes())
    os.replace(temp, root / "assets/data.bin")
    (root / "output.txt").write_text("valuable output")
    service = SkillService(settings)
    found = call(service, "sessions", checkout=str(tmp_path), client="test", task="task")
    assert found["sessions"][0]["id"] == acquisition["session_id"]
    result = call(service, "release", session_id=acquisition["session_id"], close_session=True)
    assert result["session_status"] == "open"
    assert result["acquisitions"][0]["unresolved_count"] == 3
    assert not (root / "scripts/run.py").exists()
    assert (root / "output.txt").read_text() == "valuable output"
    for path in (root / "SKILL.md", root / "assets/data.bin", root / "output.txt"):
        path.unlink()
    assert (
        call(service, "release", acquisition_id=acquisition["acquisition_id"], close_session=True)[
            "remaining"
        ]
        == 0
    )


def test_symlink_directory_and_root_replacement_preserved(
    service: SkillService, package: Path, tmp_path: Path
) -> None:
    acquired = borrow(service, tmp_path)
    root = Path(acquired["directory"])
    (root / "assets/data.bin").unlink()
    (root / "assets").rmdir()
    (root / "assets").symlink_to(package / "assets", target_is_directory=True)
    assert call(service, "release", acquisition_id=acquired["acquisition_id"])["remaining"] == 1
    assert (package / "assets/data.bin").exists()
    (root / "assets").unlink()
    root.rmdir()
    root.mkdir()
    (root / "important").write_text("not ours")
    assert call(service, "release", acquisition_id=acquired["acquisition_id"])["remaining"] == 1
    assert (root / "important").exists()


@pytest.mark.parametrize(
    "bad",
    [
        "no frontmatter",
        "---\nname: wrong\ndescription: x\n---\n",
        "---\nname: example\ndescription: [x]\n---\n",
        "---\nname: example\nname: example\ndescription: x\n---\n",
        "---\nname: example\ndescription: &x x\nmetadata: *x\n---\n",
    ],
)
def test_bad_metadata(service: SkillService, package: Path, bad: str) -> None:
    (package / "SKILL.md").write_text(bad)
    assert call(service, "search")["invalid_count"] == 1
    with pytest.raises(SkillError):
        call(service, "inspect", skill_id="example")


def test_unsafe_packages_and_limits(settings: Settings, package: Path) -> None:
    (package / "link").symlink_to("SKILL.md")
    with pytest.raises(SkillError, match="Links"):
        capture(package, settings)
    (package / "link").unlink()
    os.mkfifo(package / "fifo")
    with pytest.raises(SkillError, match="special"):
        capture(package, settings)
    (package / "fifo").unlink()
    with pytest.raises(SkillError, match="byte"):
        capture(package, dataclasses.replace(settings, package_bytes=10))
    with pytest.raises(SkillError, match=r"count|many"):
        capture(package, dataclasses.replace(settings, package_files=2))


def test_capture_detects_mutation(
    settings: Settings, package: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = files.file_record
    copied = False

    def changing(root: Path, rel: str, **kwargs: Any) -> dict[str, Any]:
        nonlocal copied
        result = original(root, rel, **kwargs)
        if kwargs.get("destination") and not copied:
            copied = True
            (root / rel).write_bytes(b"changed")
        return result

    monkeypatch.setattr(files, "file_record", changing)
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(SkillError, match="changed"):
        capture(package, settings, target)


def test_selected_frozen_binary_update_and_cleanup(
    service: SkillService, package: Path, tmp_path: Path
) -> None:
    acquired = borrow(service, tmp_path)
    root = Path(acquired["directory"])
    (root / "assets/data.bin").write_bytes(b"\x00new")
    (root / "notes.txt").write_text("publish me")
    (root / "generated.txt").write_text("keep local")
    (root / "scripts/run.py").unlink()
    proposed = prepare(
        service,
        acquired,
        [
            {"path": "assets/data.bin", "action": "replace"},
            {"path": "notes.txt", "action": "add"},
            {"path": "scripts/run.py", "action": "delete"},
        ],
    )
    assert "Binary" in proposed["preview"]
    (root / "notes.txt").write_text("later unpublished edit")
    result = call(
        service, "update_apply", request_id="publish", proposal_id=proposed["proposal_id"]
    )
    assert result == call(
        service, "update_apply", request_id="publish", proposal_id=proposed["proposal_id"]
    )
    assert (package / "notes.txt").read_text() == "publish me"
    assert (package / "assets/data.bin").read_bytes() == b"\x00new"
    assert not (package / "scripts/run.py").exists()
    assert not (package / "generated.txt").exists()
    released = call(service, "release", acquisition_id=acquired["acquisition_id"])
    assert released["acquisitions"][0]["unresolved_count"] == 2
    assert not (root / "assets/data.bin").exists()
    assert (root / "notes.txt").read_text() == "later unpublished edit"
    with connect(service.settings) as db:
        audit = db.execute(
            "SELECT details FROM audit WHERE id=?", (result["audit_receipt"],)
        ).fetchone()[0]
    assert "agent_assertion" in audit and "Human-requested" in audit


@pytest.mark.parametrize(
    "changes",
    [
        [{"path": "../bad", "action": "add"}],
        [{"path": "SKILL.md", "action": "delete"}],
        [{"path": "/tmp/bad", "action": "replace"}],
    ],
)
def test_reject_unsafe_updates(
    service: SkillService, package: Path, tmp_path: Path, changes: list[dict[str, str]]
) -> None:
    acquired = borrow(service, tmp_path)
    with pytest.raises(SkillError):
        prepare(service, acquired, changes)


def test_reject_no_authorization_and_renaming(
    service: SkillService, package: Path, tmp_path: Path
) -> None:
    acquired = borrow(service, tmp_path)
    changes = [{"path": "SKILL.md", "action": "replace"}]
    with pytest.raises(SkillError, match="human_instruction"):
        call(
            service,
            "update_prepare",
            acquisition_id=acquired["acquisition_id"],
            changes=changes,
            human_instruction=" ",
            reason="test",
        )
    Path(acquired["skill_path"]).write_text("---\nname: renamed\ndescription: x\n---\n")
    with pytest.raises(SkillError, match="stable"):
        prepare(service, acquired, changes)


def test_stale_and_competing_proposals(
    service: SkillService, package: Path, tmp_path: Path
) -> None:
    first, second = borrow(service, tmp_path), borrow(service, tmp_path)
    Path(first["skill_path"]).write_text(Path(first["skill_path"]).read_text() + "one\n")
    Path(second["skill_path"]).write_text(Path(second["skill_path"]).read_text() + "two\n")
    changes = [{"path": "SKILL.md", "action": "replace"}]
    p1, p2 = prepare(service, first, changes), prepare(service, second, changes)

    def apply(p: dict[str, Any]) -> str:
        try:
            return call(service, "update_apply", proposal_id=p["proposal_id"])["status"]
        except SkillError as exc:
            return exc.code

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(apply, [p1, p2])) == ["applied", "revision_conflict"]
    with pytest.raises(SkillError, match="changed since"):
        prepare(service, second if "one" in (package / "SKILL.md").read_text() else first, changes)


class PowerLoss(BaseException):
    pass


@pytest.mark.parametrize("external_edit", [False, True])
def test_interrupted_write_recovery(
    settings: Settings,
    service: SkillService,
    package: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    external_edit: bool,
) -> None:
    acquired = borrow(service, tmp_path)
    original = (package / "SKILL.md").read_text()
    Path(acquired["skill_path"]).write_text(original + "proposed\n")
    (Path(acquired["directory"]) / "assets/data.bin").write_bytes(b"\x00changed")
    p = prepare(
        service,
        acquired,
        [
            {"path": "SKILL.md", "action": "replace"},
            {"path": "assets/data.bin", "action": "replace"},
        ],
    )
    real_install = service.install_file
    calls = 0

    def crash(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PowerLoss
        return real_install(*args, **kwargs)

    monkeypatch.setattr(service, "install_file", crash)
    with pytest.raises(PowerLoss):
        call(service, "update_apply", proposal_id=p["proposal_id"])
    assert (package / "SKILL.md").read_text() == original + "proposed\n"
    if external_edit:
        (package / "SKILL.md").write_text("external valuable edit")
    recovered = SkillService(settings)
    result = call(recovered, "sessions")
    if external_edit:
        assert (package / "SKILL.md").read_text() == "external valuable edit"
        assert result["recovery"]
        with pytest.raises(SkillError, match="conflicts"):
            borrow(recovered, tmp_path)
    else:
        assert (package / "SKILL.md").read_text() == original
        assert not result["recovery"]
        assert not list((settings.state / "transactions").iterdir())


def test_proposal_tampering(service: SkillService, package: Path, tmp_path: Path) -> None:
    acquired = borrow(service, tmp_path)
    Path(acquired["skill_path"]).write_text(Path(acquired["skill_path"]).read_text() + "change")
    p = prepare(service, acquired, [{"path": "SKILL.md", "action": "replace"}])
    frozen = service.settings.state / "proposals" / p["proposal_id"] / "package/SKILL.md"
    frozen.write_text("tampered")
    with pytest.raises(SkillError, match="altered"):
        call(service, "update_apply", proposal_id=p["proposal_id"])


def test_storage_capacity(settings: Settings, package: Path, tmp_path: Path) -> None:
    service = SkillService(dataclasses.replace(settings, storage_bytes=1))
    with pytest.raises(SkillError, match="storage is full"):
        borrow(service, tmp_path)


def test_manifest_revision_ignores_mtime_but_includes_executable(
    settings: Settings, package: Path
) -> None:
    before = capture(package, settings)
    (package / "SKILL.md").touch()
    assert capture(package, settings)["revision"] == before["revision"]
    (package / "SKILL.md").chmod(0o755)
    assert capture(package, settings)["revision"] != before["revision"]
    assert json.loads(json.dumps(before))["size"] > 0


def test_published_new_directories_and_unexpected_empty_dir(
    service: SkillService, package: Path, tmp_path: Path
) -> None:
    acquired = borrow(service, tmp_path)
    root = Path(acquired["directory"])
    (root / "new/deep").mkdir(parents=True)
    (root / "new/deep/guide.txt").write_text("publish")
    (root / "unexpected-empty").mkdir()
    p = prepare(service, acquired, [{"path": "new/deep/guide.txt", "action": "add"}])
    call(service, "update_apply", proposal_id=p["proposal_id"])
    result = call(service, "release", acquisition_id=acquired["acquisition_id"])
    assert not (root / "new").exists()
    assert result["acquisitions"][0]["unresolved_paths"] == [str(root / "unexpected-empty")]
    assert not (service.settings.state / "proposals" / p["proposal_id"]).exists()


def test_package_empty_directory_preserved(
    service: SkillService, package: Path, tmp_path: Path
) -> None:
    (package / "empty/nested").mkdir(parents=True)
    acquired = borrow(service, tmp_path)
    assert (Path(acquired["directory"]) / "empty/nested").is_dir()
    assert call(service, "release", acquisition_id=acquired["acquisition_id"])["remaining"] == 0


def test_publish_after_partial_cleanup(
    service: SkillService, package: Path, tmp_path: Path
) -> None:
    acquired = borrow(service, tmp_path)
    root = Path(acquired["directory"])
    (root / "assets/data.bin").write_bytes(b"changed")
    assert call(service, "release", acquisition_id=acquired["acquisition_id"])["remaining"] == 1
    p = prepare(service, acquired, [{"path": "assets/data.bin", "action": "replace"}])
    call(service, "update_apply", proposal_id=p["proposal_id"])
    assert call(service, "release", acquisition_id=acquired["acquisition_id"])["remaining"] == 0
    assert (package / "assets/data.bin").read_bytes() == b"changed"


def test_owner_conflict_resolution(
    settings: Settings,
    service: SkillService,
    package: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dev_mcp.skills.recovery import accept_current_source

    acquired = borrow(service, tmp_path)
    original = Path(acquired["skill_path"]).read_text()
    Path(acquired["skill_path"]).write_text(original + "proposed")
    p = prepare(service, acquired, [{"path": "SKILL.md", "action": "replace"}])
    real_capture = capture
    import dev_mcp.skills.service as module

    captures = 0

    def fail_final_capture(root: Path, settings: Settings, *args: Any) -> dict[str, Any]:
        nonlocal captures
        if root == package:
            captures += 1
            if captures == 2:
                (root / "SKILL.md").write_text(original + "external chosen edit")
                raise PowerLoss
        return real_capture(root, settings, *args)

    monkeypatch.setattr(module, "capture", fail_final_capture)
    with pytest.raises(PowerLoss):
        call(service, "update_apply", proposal_id=p["proposal_id"])
    monkeypatch.setattr(module, "capture", real_capture)
    recovered = SkillService(settings)
    assert call(recovered, "sessions")["recovery"]
    result = accept_current_source(settings, p["proposal_id"])
    assert result["status"] == "recovered"
    assert (package / "SKILL.md").read_text() == original + "external chosen edit"
    assert not call(recovered, "sessions")["recovery"]
    assert not list((settings.state / "transactions").iterdir())


@pytest.mark.integration
def test_recovery_after_real_process_exit(
    settings: Settings, service: SkillService, package: Path, tmp_path: Path
) -> None:
    import subprocess
    import sys

    acquired = borrow(service, tmp_path)
    original = Path(acquired["skill_path"]).read_text()
    Path(acquired["skill_path"]).write_text(original + "proposed")
    (Path(acquired["directory"]) / "assets/data.bin").write_bytes(b"changed")
    p = prepare(
        service,
        acquired,
        [
            {"path": "SKILL.md", "action": "replace"},
            {"path": "assets/data.bin", "action": "replace"},
        ],
    )
    script = """
import json, os, sys
from pathlib import Path
from dev_mcp.config import Settings
from dev_mcp.skills.service import SkillService
args = json.loads(sys.argv[1])
service = SkillService(Settings(**{k: Path(v) for k,v in args["settings"].items()}))
real_install = service.install_file
calls = 0
def crash(*a, **kw):
    global calls
    calls += 1
    if calls == 2:
        os._exit(73)
    return real_install(*a, **kw)
service.install_file = crash
service.call("skills_update_apply", {"request_id": "real-crash", "proposal_id": args["proposal"]})
"""
    args = {
        "settings": {k: str(getattr(settings, k)) for k in ("library", "state", "config")},
        "proposal": p["proposal_id"],
    }
    result = subprocess.run([sys.executable, "-c", script, json.dumps(args)], check=False)
    assert result.returncode == 73
    assert (package / "SKILL.md").read_text() == original + "proposed"
    recovered = SkillService(settings)
    assert (package / "SKILL.md").read_text() == original
    assert not call(recovered, "sessions")["recovery"]


def test_edit_between_cleanup_hash_and_unlink_is_preserved(
    service: SkillService, package: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dev_mcp.skills.service as module

    acquired = borrow(service, tmp_path)
    root = Path(acquired["directory"])
    real_current = module.current_file

    def editing(root: Path, rel: str, limit: int) -> dict[str, Any] | None:
        record = real_current(root, rel, limit)
        if rel == "SKILL.md" and record is not None:
            (root / rel).write_text("concurrent valuable edit")
        return record

    monkeypatch.setattr(module, "current_file", editing)
    result = call(service, "release", acquisition_id=acquired["acquisition_id"])
    assert result["remaining"] == 1
    assert (root / "SKILL.md").read_text() == "concurrent valuable edit"
