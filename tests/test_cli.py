from __future__ import annotations

import pytest
from click.testing import CliRunner

from portwhisperer import cli


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
