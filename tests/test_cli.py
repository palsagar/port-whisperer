from __future__ import annotations

import json
from datetime import datetime
from importlib.metadata import version

import click
import pytest
from click.testing import CliRunner

from portwhisperer import cli
from portwhisperer.scanner import KillResult, PortInfo, ProcessInfo


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
    # Exit 1: the port is free
    assert result.exit_code == 1, result.output
    assert detail_calls == [3000]
    assert "No process found" in result.output


def test_explicit_whoisonport(detail_calls):
    result = CliRunner().invoke(cli.main, ["--whoisonport", "8012"])
    assert result.exit_code == 1, result.output
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
    monkeypatch.setattr(cli, "_stdout_is_tty", lambda: True)
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


def test_detail_with_redirected_stdout_does_not_prompt(monkeypatch, detail):
    # "ports 3000 > /dev/null" in a terminal: the prompt would be invisible
    monkeypatch.setattr(cli, "_stdout_is_tty", lambda: False)
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


# ── scripting: --json, exit codes, --version, banner (#5) ────────────────────


@pytest.fixture
def listing(monkeypatch: pytest.MonkeyPatch) -> list[PortInfo]:
    """Serve a dev server, a desktop app and a Docker port as the listing."""
    ports = [
        _info(3000, 41000, start_time=datetime(2026, 10, 12, 9, 0), address="*"),
        PortInfo(7000, 745, "ControlCenter", "ControlCenter", ""),
        _info(5432, 1500, container="pg", address="127.0.0.1"),
    ]
    monkeypatch.setattr(cli, "get_listening_ports", lambda: ports)
    return ports


def test_json_listing(listing):
    result = CliRunner().invoke(cli.main, ["--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    # The dev filter applies, as in the table
    assert [p["port"] for p in data] == [3000, 5432]
    assert data[0]["start_time"] == "2026-10-12T09:00:00"
    assert data[0]["address"] == "*"


def test_json_listing_all(listing):
    result = CliRunner().invoke(cli.main, ["--json", "--all"])
    assert [p["port"] for p in json.loads(result.output)] == [3000, 7000, 5432]


def test_json_listing_has_no_banner_even_on_a_tty(monkeypatch, listing):
    monkeypatch.setattr(cli, "_stdout_is_tty", lambda: True)
    result = CliRunner().invoke(cli.main, ["--json"])
    assert result.output.lstrip().startswith("[")


def test_json_detail_is_one_object(monkeypatch, detail):
    result = CliRunner().invoke(cli.main, ["3000", "--json"], input="y\n")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["port"] == 3000
    # JSON mode never prompts, even on a terminal
    assert detail == []


def test_json_detail_free_port_exits_1_silently(detail_calls):
    result = CliRunner().invoke(cli.main, ["3000", "--json"])
    assert result.exit_code == 1
    assert result.output == ""


@pytest.mark.parametrize("args", [["70000"], ["0"], ["--whoisonport", "70000"]])
def test_invalid_port_exits_2(detail_calls, args):
    result = CliRunner().invoke(cli.main, args)
    assert result.exit_code == 2
    assert detail_calls == []


def test_invalid_bare_port_message_names_the_port(detail_calls):
    result = CliRunner().invoke(cli.main, ["70000"])
    assert "70000 is not a valid port" in result.output


def test_version():
    result = CliRunner().invoke(cli.main, ["--version"])
    assert result.exit_code == 0
    assert version("portwhisperer") in result.output


@pytest.mark.parametrize(
    ("args", "tty", "banner"),
    [
        ([], True, True),
        ([], False, False),
        (["-q"], True, False),
        (["--quiet"], True, False),
        (["ps"], True, True),
        (["ps", "-q"], True, False),
        (["ps"], False, False),
    ],
)
def test_banner_only_on_a_tty(monkeypatch, listing, args, tty, banner):
    monkeypatch.setattr(cli, "get_all_processes", lambda: [])
    monkeypatch.setattr(cli, "_stdout_is_tty", lambda: tty)
    result = CliRunner().invoke(cli.main, args)
    assert result.exit_code == 0, result.output
    assert ("Port Whisperer" in result.output) is banner


def test_ps_json_keeps_docker_processes_apart(monkeypatch):
    procs = [
        ProcessInfo(10, "com.docker.backend", "", "", 1.0),
        ProcessInfo(11, "com.docker.vpnkit", "", "", 2.0),
        ProcessInfo(12, "node", "node x.js", "x.js", 3.0),
    ]
    monkeypatch.setattr(cli, "get_all_processes", lambda: list(procs))
    result = CliRunner().invoke(cli.main, ["ps", "--json"])
    assert result.exit_code == 0, result.output
    assert [p["pid"] for p in json.loads(result.output)] == [12, 11, 10]


# ── ports watch (#7) ──────────────────────────────────────────────────────────


@pytest.fixture
def watch_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Record how ``watch_ports`` is called instead of polling."""
    calls: list[dict] = []

    def fake_watch(callback, interval: float = 2.0, dev_only: bool = True) -> None:
        calls.append({"callback": callback, "interval": interval, "dev": dev_only})

    monkeypatch.setattr(cli, "watch_ports", fake_watch)
    return calls


@pytest.mark.parametrize("interval", ["0", "-1", "0.1"])
def test_watch_rejects_tiny_interval(watch_calls, interval):
    result = CliRunner().invoke(cli.main, ["watch", "--interval", interval])
    assert result.exit_code == 2
    assert "0.2" in result.output
    assert watch_calls == []


def test_watch_defaults_to_dev_filter(watch_calls):
    assert CliRunner().invoke(cli.main, ["watch"]).exit_code == 0
    assert watch_calls[0]["dev"] is True
    assert watch_calls[0]["interval"] == 2.0


def test_watch_all(watch_calls):
    CliRunner().invoke(cli.main, ["watch", "-a", "--interval", "0.5"])
    assert watch_calls[0]["dev"] is False
    assert watch_calls[0]["interval"] == 0.5


def test_watch_json_prints_one_object_per_line(watch_calls, capsys):
    result = CliRunner().invoke(cli.main, ["watch", "--json"])
    assert result.output == ""  # no banner, no heading
    callback = watch_calls[0]["callback"]
    callback("snapshot", [_info(3000, 1), _info(4000, 2)])
    callback("restarted", _info(3000, 9))
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(e["event"], e.get("port"), e.get("pid")) for e in events] == [
        ("snapshot", 3000, 1),
        ("snapshot", 4000, 2),
        ("ready", None, None),
        ("restarted", 3000, 9),
    ]
    assert "time" in events[0] and "time" in events[2]


def test_watch_json_empty_snapshot_still_says_ready(watch_calls, capsys):
    CliRunner().invoke(cli.main, ["watch", "--json"])
    watch_calls[0]["callback"]("snapshot", [])
    (line,) = capsys.readouterr().out.splitlines()
    assert json.loads(line)["event"] == "ready"


def test_watch_routes_snapshot_and_events(monkeypatch, watch_calls):
    seen: list[tuple] = []
    monkeypatch.setattr(cli, "display_watch_snapshot", lambda d: seen.append(("s", d)))
    monkeypatch.setattr(
        cli, "display_watch_event", lambda e, d: seen.append((e, d.port))
    )
    CliRunner().invoke(cli.main, ["watch"])
    callback = watch_calls[0]["callback"]
    callback("snapshot", [])
    callback("restarted", _info(3000, 9))
    assert seen == [("s", []), ("restarted", 3000)]


# ── ports open / next / here (#8) ─────────────────────────────────────────────


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch, killer) -> list[str]:
    urls: list[str] = []
    monkeypatch.setattr(cli.click, "launch", lambda url: urls.append(url))
    return urls


def test_open_launches_browser(launched):
    result = CliRunner().invoke(cli.main, ["open", "3000"])
    assert result.exit_code == 0, result.output
    assert launched == ["http://localhost:3000"]


def test_open_free_port_exits_1(launched):
    result = CliRunner().invoke(cli.main, ["open", "3001"])
    assert result.exit_code == 1
    assert "Nothing listens on :3001" in result.output
    assert launched == []


def test_next_skips_busy_ports(monkeypatch):
    monkeypatch.setattr(
        cli, "get_listening_ports", lambda: [_info(3000, 1), _info(3002, 2)]
    )
    assert CliRunner().invoke(cli.main, ["next", "3000"]).output == "3001\n"
    assert CliRunner().invoke(cli.main, ["next"]).output == "3001\n"
    assert CliRunner().invoke(cli.main, ["next", "3002"]).output == "3003\n"


def test_next_when_nothing_is_free(monkeypatch):
    monkeypatch.setattr(cli, "get_listening_ports", lambda: [_info(65535, 1)])
    result = CliRunner().invoke(cli.main, ["next", "65535"])
    assert result.exit_code == 1


def test_here_shows_only_this_project(monkeypatch):
    ports = [
        _info(3000, 1, cwd="/repo"),
        _info(3001, 2, cwd="/repo/packages/web"),
        # Same prefix, different project
        _info(3002, 3, cwd="/repo-other"),
        _info(3003, 4),
    ]
    monkeypatch.setattr(cli, "get_listening_ports", lambda: ports)
    monkeypatch.setattr(cli, "find_project_root", lambda cwd: "/repo")
    shown: list = []
    monkeypatch.setattr(
        cli,
        "display_port_table",
        lambda ports, project: shown.append(([p.port for p in ports], project)),
    )
    result = CliRunner().invoke(cli.main, ["here"])
    assert result.exit_code == 0, result.output
    assert shown == [([3000, 3001], "/repo")]


@pytest.mark.parametrize("name", ["open", "next", "here", "watch"])
def test_new_command_names_are_not_ports(monkeypatch, detail_calls, name):
    monkeypatch.setattr(cli, "get_listening_ports", lambda: [])
    monkeypatch.setattr(cli, "watch_ports", lambda *a, **k: None)
    monkeypatch.setattr(cli, "find_project_root", lambda cwd: "/nowhere")
    CliRunner().invoke(cli.main, [name, "--help"])
    assert detail_calls == []


def test_bare_port_still_routes_next_to_new_commands(detail_calls):
    CliRunner().invoke(cli.main, ["3000"])
    assert detail_calls == [3000]


# ── scan failures ─────────────────────────────────────────────────────────────


def _failing_scan() -> list[PortInfo]:
    raise cli.ScanError("lsof failed: boom")


@pytest.mark.parametrize(
    "args",
    [["3000", "--json"], ["3000"], ["next"], ["open", "3000"], ["here"], []],
)
def test_failed_scan_exits_3_and_never_claims_free(monkeypatch, args):
    monkeypatch.setattr(cli, "get_listening_ports", _failing_scan)
    monkeypatch.setattr(cli, "get_port_details", lambda port: _failing_scan())
    monkeypatch.setattr(cli, "find_project_root", lambda cwd: "/repo")
    result = CliRunner().invoke(cli.main, args)
    assert result.exit_code == 3
    assert "Could not check the ports" in result.output
    assert "3000\n" not in result.output


@pytest.mark.parametrize(
    "args",
    [["--json", "3000"], ["-q", "3000"], ["-q", "--json", "3000"], ["-a", "3000"]],
)
def test_flags_before_the_port(detail_calls, args):
    CliRunner().invoke(cli.main, args)
    assert detail_calls == [3000]


def test_flag_before_invalid_port(detail_calls):
    result = CliRunner().invoke(cli.main, ["--json", "70000"])
    assert result.exit_code == 2
    assert "70000 is not a valid port" in result.output


def test_flag_before_subcommand_is_not_rewritten(watch_calls, detail_calls):
    result = CliRunner().invoke(cli.main, ["-q", "watch"])
    assert result.exit_code == 0, result.output
    assert detail_calls == []
    assert len(watch_calls) == 1
