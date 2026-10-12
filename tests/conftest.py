from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from portwhisperer import scanner

FIXTURES = Path(__file__).parent / "fixtures"

# Most specific match first: "lsof -a -d cwd" and "lsof -t" must not hit the
# LISTEN route.
ROUTES: list[tuple[str, str]] = [
    ("lsof -t", "lsof_pids.txt"),
    ("-sTCP:LISTEN", "lsof_listen.txt"),
    ("-d cwd", "lsof_cwd.txt"),
    ("ps -p", "ps_batch.txt"),
    ("pid=,ppid=,comm=", "ps_tree.txt"),
    ("pcpu=", "ps_all.txt"),
    ("docker ps", "docker_ps.txt"),
    ("docker stop", "docker_stop.txt"),
    ("docker kill", "docker_stop.txt"),
    ("launchctl list", "launchctl_list.txt"),
]

# The frozen "now" used for uptime assertions.
NOW = datetime(2026, 10, 12, 12, 0, 0)


class FakeRun:
    """Stand-in for ``subprocess.run`` that serves fixture text by command.

    Matches on the space-joined argument list, records every call so tests
    can count them, and fails any call made with ``shell=True``.

    Examples
    --------
    .. code-block:: python

        fake = FakeRun(tmp_path)
        fake.outputs["docker_ps.txt"] = ""  # pretend no containers run
    """

    def __init__(self, root: Path) -> None:
        self.calls: list[str | list[str]] = []
        self.outputs: dict[str, str] = {
            name: (FIXTURES / name).read_text().replace("{root}", str(root))
            for _, name in ROUTES
        }
        self.outputs["git"] = "main\n"

    def commands(self, needle: str) -> list[str]:
        """Return the recorded commands, joined, that contain ``needle``."""
        return [cmd for cmd in map(_joined, self.calls) if needle in cmd]

    def __call__(
        self, args: str | list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert not kwargs.get("shell"), "scanner must not run commands in a shell"
        self.calls.append(args)
        cmd = _joined(args)
        if cmd.startswith("git"):
            return subprocess.CompletedProcess(args, 0, self.outputs["git"], "")
        for needle, name in ROUTES:
            if needle in cmd:
                return subprocess.CompletedProcess(args, 0, self.outputs[name], "")
        raise AssertionError(f"unexpected subprocess call: {cmd}")


def _joined(args: str | list[str]) -> str:
    return args if isinstance(args, str) else " ".join(args)


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """Build the fake project directories that ``lsof_cwd.txt`` points at."""
    (tmp_path / "simulator").mkdir()
    (tmp_path / "simulator" / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "claude-mem").mkdir()
    (tmp_path / "claude-mem" / "package.json").write_text("{}")
    (tmp_path / "webapp" / "src").mkdir(parents=True)
    (tmp_path / "webapp" / "package.json").write_text(
        json.dumps({"dependencies": {"next": "14.2.0", "react": "18.3.0"}})
    )
    (tmp_path / "nextcloud-sync").mkdir()
    (tmp_path / "nextcloud-sync" / "package.json").write_text(
        json.dumps({"dependencies": {"express": "4.19.0"}})
    )
    (tmp_path / "agent").mkdir()
    return tmp_path


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch, project_root: Path) -> FakeRun:
    """Replace ``subprocess.run`` with a :class:`FakeRun` serving the fixtures."""
    fake = FakeRun(project_root)
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(NOW.year, NOW.month, NOW.day, NOW.hour, NOW.minute, NOW.second)


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """Freeze ``datetime.now()`` inside the scanner so uptimes are stable."""
    monkeypatch.setattr(scanner, "datetime", _FrozenDatetime)
    return NOW
