from __future__ import annotations

import pytest
from click.testing import CliRunner

from portwhisperer import cli
from portwhisperer.scanner import KillResult, PortInfo


@pytest.fixture
def detail_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record the port passed to ``get_port_details`` and report no listener."""
    calls: list[int] = []

    def fake_details(port: int) -> None:
        calls.append(port)

    monkeypatch.setattr(cli, "get_port_details", fake_details)
    return calls


def test_bare_port_becomes_whoisonport(detail_calls):
    result = CliRunner().invoke(cli.main, ["3000"], input="n\n")
    assert result.exit_code == 0, result.output
    assert detail_calls == [3000]
    assert "No process found" in result.output


def test_explicit_whoisonport(detail_calls):
    result = CliRunner().invoke(cli.main, ["--whoisonport", "8012"])
    assert result.exit_code == 0, result.output
    assert detail_calls == [8012]


def test_subcommand_is_not_rewritten(monkeypatch, detail_calls):
    shown: list[list] = []
    monkeypatch.setattr(cli, "get_all_processes", lambda: [])
    monkeypatch.setattr(
        cli, "display_process_table", lambda procs, filtered: shown.append(procs)
    )
    result = CliRunner().invoke(cli.main, ["ps"])
    assert result.exit_code == 0, result.output
    assert shown == [[]]
    assert detail_calls == []


def test_unknown_word_is_an_error(detail_calls):
    result = CliRunner().invoke(cli.main, ["nonsense"])
    assert result.exit_code == 2
    assert detail_calls == []


# ── ports kill ────────────────────────────────────────────────────────────────


def _info(port: int, pid: int, **extra: object) -> PortInfo:
    return PortInfo(
        port=port, pid=pid, process_name="node", raw_name="node", command="", **extra
    )


@pytest.fixture
def killer(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, bool]]:
    """Serve two listening ports and record each ``free_port`` call."""
    calls: list[tuple[int, bool]] = []

    def fake_free_port(info: PortInfo, force: bool = False) -> KillResult:
        calls.append((info.port, force))
        return KillResult(info.port, info.pid, True, f":{info.port} is free")

    monkeypatch.setattr(
        cli, "get_listening_ports", lambda: [_info(3000, 41000), _info(4000, 41100)]
    )
    monkeypatch.setattr(cli, "free_port", fake_free_port)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    return calls


def test_kill_with_yes(killer):
    result = CliRunner().invoke(cli.main, ["kill", "3000", "4000", "-y"])
    assert result.exit_code == 0, result.output
    assert killer == [(3000, False), (4000, False)]
    assert ":3000 is free" in result.output


def test_kill_force(killer):
    result = CliRunner().invoke(cli.main, ["kill", "--force", "-y", "3000"])
    assert result.exit_code == 0, result.output
    assert killer == [(3000, True)]


def test_kill_asks_once(killer):
    result = CliRunner().invoke(cli.main, ["kill", "3000", "4000"], input="y\n")
    assert result.exit_code == 0, result.output
    assert result.output.count("[y/N]") == 1
    assert "41100" in result.output
    assert killer == [(3000, False), (4000, False)]


def test_kill_declined(killer):
    result = CliRunner().invoke(cli.main, ["kill", "3000"], input="n\n")
    assert result.exit_code == 1
    assert killer == []


def test_kill_free_port_fails(killer):
    result = CliRunner().invoke(cli.main, ["kill", "9999", "-y"])
    assert result.exit_code == 1
    assert "nothing listens on :9999" in result.output
    assert killer == []


def test_kill_partial_failure_exits_nonzero(killer):
    result = CliRunner().invoke(cli.main, ["kill", "3000", "9999", "-y"])
    assert result.exit_code == 1
    assert killer == [(3000, False)]


def test_kill_failed_result_exits_nonzero(monkeypatch, killer):
    def failing(info: PortInfo, force: bool = False) -> KillResult:
        return KillResult(info.port, info.pid, False, "still listens")

    monkeypatch.setattr(cli, "free_port", failing)
    result = CliRunner().invoke(cli.main, ["kill", "3000", "-y"])
    assert result.exit_code == 1
    assert "still listens" in result.output


def test_kill_without_tty_needs_yes(monkeypatch, killer):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    result = CliRunner().invoke(cli.main, ["kill", "3000"])
    assert result.exit_code == 1
    assert "--yes" in result.output
    assert killer == []


def test_kill_is_a_subcommand_not_a_port(killer, detail_calls):
    result = CliRunner().invoke(cli.main, ["kill", "3000", "-y"])
    assert result.exit_code == 0, result.output
    assert detail_calls == []
