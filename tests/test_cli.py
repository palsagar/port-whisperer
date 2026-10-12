from __future__ import annotations

import click
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


# ── ports <port> ──────────────────────────────────────────────────────────────


@pytest.fixture
def detail(monkeypatch: pytest.MonkeyPatch, killer) -> list[tuple[int, bool]]:
    """Report node on :3000 for the detail view; reuse the kill recorder."""
    monkeypatch.setattr(cli, "get_port_details", lambda port: _info(port, 41000))
    return killer


def test_detail_prompt_uses_free_port(detail):
    result = CliRunner().invoke(cli.main, ["3000"], input="y\n")
    assert result.exit_code == 0, result.output
    assert detail == [(3000, False)]
    assert ":3000 is free" in result.output


def test_detail_without_tty_does_not_prompt(monkeypatch, detail):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    result = CliRunner().invoke(cli.main, ["3000"])
    assert result.exit_code == 0, result.output
    assert "[y/N]" not in result.output
    assert detail == []


def test_detail_hint_points_to_ports_kill(monkeypatch, detail):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    result = CliRunner().invoke(cli.main, ["3000"])
    assert "ports kill 3000" in result.output
    assert "ports clean" not in result.output


# ── ports clean ───────────────────────────────────────────────────────────────


@pytest.fixture
def clean(monkeypatch: pytest.MonkeyPatch, killer) -> list[tuple[int, bool]]:
    """Offer two detached candidates; reuse the kill recorder."""
    monkeypatch.setattr(
        cli,
        "find_clean_candidates",
        lambda: [
            _info(3000, 41000, status="detached"),
            _info(4000, 41100, status="detached"),
        ],
    )
    return killer


def test_clean_kills_only_the_picked_process(clean):
    result = CliRunner().invoke(cli.main, ["clean"], input="2\n")
    assert result.exit_code == 0, result.output
    assert clean == [(4000, False)]
    assert "Kill which?" in result.output
    # Each process appears once in the list; the picked one once more in results
    assert result.output.count("PID 41000") == 1
    assert result.output.count("PID 41100") == 2


def test_clean_defaults_to_none(clean):
    result = CliRunner().invoke(cli.main, ["clean"], input="\n")
    assert result.exit_code == 0, result.output
    assert clean == []


def test_clean_all(clean):
    result = CliRunner().invoke(cli.main, ["clean"], input="all\n")
    assert result.exit_code == 0, result.output
    assert clean == [(3000, False), (4000, False)]


def test_clean_reasks_on_bad_input(clean):
    result = CliRunner().invoke(cli.main, ["clean"], input="7\n1\n")
    assert result.exit_code == 0, result.output
    assert "7" in result.output
    assert clean == [(3000, False)]


def test_clean_without_tty_kills_nothing(monkeypatch, clean):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    result = CliRunner().invoke(cli.main, ["clean"])
    assert result.exit_code == 0, result.output
    assert "Kill which?" not in result.output
    assert clean == []


def test_clean_with_no_candidates(monkeypatch, killer):
    monkeypatch.setattr(cli, "find_clean_candidates", lambda: [])
    result = CliRunner().invoke(cli.main, ["clean"])
    assert result.exit_code == 0, result.output
    assert "Nothing to clean" in result.output
    assert killer == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", []),
        ("none", []),
        ("all", [0, 1, 2]),
        ("1,3", [0, 2]),
        ("3 1 3", [2, 0]),
        (" 2 ", [1]),
    ],
)
def test_parse_selection(raw, expected):
    assert cli._parse_selection(raw, 3) == expected


@pytest.mark.parametrize("raw", ["0", "4", "x", "1,,y", "-1"])
def test_parse_selection_rejects(raw):
    with pytest.raises(click.BadParameter):
        cli._parse_selection(raw, 3)
