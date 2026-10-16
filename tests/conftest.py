from __future__ import annotations

from pathlib import Path

import pytest

from dev_mcp.config import Settings
from dev_mcp.skills.service import SkillService


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    library = tmp_path / "library"
    library.mkdir()
    return Settings(library=library, state=tmp_path / "state", config=tmp_path / "config")


@pytest.fixture
def package(settings: Settings) -> Path:
    root = settings.library / "example"
    (root / "assets").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: example\ndescription: Example development skill\n"
        "compatibility: Python 3.12\nmetadata:\n  author: Test\n---\nRead assets/data.bin.\n"
    )
    (root / "assets/data.bin").write_bytes(b"\x00\x01\x02" * 100)
    (root / "scripts/run.py").write_text("print('explicitly run by client')\n")
    (root / "scripts/run.py").chmod(0o755)
    return root


@pytest.fixture
def service(settings: Settings) -> SkillService:
    return SkillService(settings)
