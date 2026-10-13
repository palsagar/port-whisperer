from __future__ import annotations

import pytest
from rich.console import Console

from portwhisperer import display
from portwhisperer.display import format_address, format_status
from portwhisperer.scanner import PortInfo, ProcessInfo


def test_detached_is_neutral_not_a_warning():
    # Detached only means "no parent shell", so it must not look like a warning
    assert format_status("detached") == "[blue]●[/] [blue]detached[/]"


@pytest.fixture
def screen(monkeypatch: pytest.MonkeyPatch) -> Console:
    """Render display output into an 80-column console and record it."""
    console = Console(width=80, record=True, color_system=None)
    monkeypatch.setattr(display, "console", console)
    monkeypatch.setattr(display, "_show_banner", True)
    return console


def _long_port(port: int) -> PortInfo:
    return PortInfo(
        port=port,
        pid=98426,
        process_name="python3.13",
        raw_name="python3.13",
        command="python3 -m uvicorn app:app",
        project_name="an-extremely-long-project-name-for-testing",
        framework="Elasticsearch",
        uptime="202d 13h",
        status="detached",
        address="127.0.0.1",
        tool="webpack-dev-server",
    )


def _table_rows(text: str, first: str) -> list[str]:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith(first))
    return [line for line in lines[start + 2 :] if line.strip()][:-1]  # no footer


def test_port_table_rows_fit_on_one_line(screen):
    display.display_port_table([_long_port(54361), _long_port(54362)])
    rows = _table_rows(screen.export_text(), "PORT")
    assert len(rows) == 2
    assert all(row.lstrip().startswith(":5436") for row in rows)
    assert all(len(row) <= 80 for row in rows)
    assert "…" in rows[0]


def test_process_table_rows_fit_on_one_line(screen):
    proc = ProcessInfo(
        pid=58937,
        process_name="node",
        command="node daemon.mjs --very --long --arguments here",
        description="daemon.mjs --very --long --arguments here and more",
        cpu=12.5,
        memory="101.5 MB",
        project_name="an-extremely-long-project-name-for-testing",
        framework="Node.js",
        uptime="71d 6h",
        tool="webpack-dev-server",
    )
    display.display_process_table([proc, proc])
    rows = _table_rows(screen.export_text(), "PID")
    assert len(rows) == 2
    assert all(row.lstrip().startswith("58937") for row in rows)


def test_port_table_shows_address_and_tool(screen):
    lan = _long_port(3000)
    lan.address = "*"
    lan.tool = "vite"
    display.display_port_table([lan, _long_port(8012)])
    text = screen.export_text()
    assert "ADDRESS" in text
    assert "LAN" in text and "local" in text
    assert "vite" in text


@pytest.mark.parametrize(
    ("address", "label"),
    [
        ("127.0.0.1", "local"),
        ("[::1]", "local"),
        ("*", "LAN"),
        ("0.0.0.0", "LAN"),
        ("192.168.1.5", "192.168.1.5"),
        (None, "—"),
    ],
)
def test_format_address(address, label):
    assert label in format_address(address)


def test_banner_can_be_turned_off(screen):
    display.set_banner(False)
    display.display_port_table([])
    assert "Port Whisperer" not in screen.export_text()


def _detail() -> PortInfo:
    return PortInfo(
        port=8012,
        pid=23928,
        process_name="python3.13",
        raw_name="python3.13",
        command="/Users/dev/simulator/.venv/bin/python3 "
        "/Users/dev/simulator/.venv/bin/uvicorn simulator.server:app --port 8012",
        address="127.0.0.1",
        tool="uvicorn",
        other_ports=[8013, 8014],
        process_tree=[
            {"pid": 23928, "ppid": 23892, "name": "python3"},
            {"pid": 23892, "ppid": 23800, "name": "uv"},
            {"pid": 23800, "ppid": 1, "name": "-zsh"},
        ],
    )


def test_detail_shows_command_address_and_other_ports(screen):
    display.display_port_detail(_detail())
    text = screen.export_text()
    assert "python3.13 running uvicorn" in text
    assert "127.0.0.1:8012 (this machine only)" in text
    assert ":8013, :8014" in text
    # The full command is folded, not cut: every part of it appears
    flat = "".join(text.split())
    assert "simulator.server:app--port8012" in flat
    assert "…" not in text


def test_detail_process_tree_is_clean(screen):
    display.display_port_detail(_detail())
    lines = screen.export_text().splitlines()
    start = next(i for i, line in enumerate(lines) if "Process Tree" in line) + 2
    tree = [line.rstrip() for line in lines[start : start + 3]]
    # Oldest ancestor first, one connector per level, no empty root line
    assert tree == [
        "  -zsh (23800)",
        "  └── uv (23892)",
        "      └── python3 (23928)",
    ]


def test_detail_shows_container(screen):
    info = PortInfo(
        port=5432,
        pid=1500,
        process_name="docker",
        raw_name="com.docker.backend",
        command="",
        container="dev-postgres",
        container_id="3f9a1c0e5b7d2a4c6e8f",
        image="postgres:15",
    )
    display.display_port_detail(info)
    text = screen.export_text()
    assert "dev-postgres" in text
    assert "postgres:15" in text
    assert "3f9a1c0e5b7d" in text


@pytest.mark.parametrize(
    ("event", "label"),
    [("new", "▲ NEW"), ("restarted", "↻ RESTARTED"), ("closed", "▼ CLOSED")],
)
def test_watch_event_line(screen, event, label):
    info = _detail()
    info.project_name = "simulator"
    display.display_watch_event(event, info)
    text = screen.export_text()
    assert label in text
    assert ":8012 local ← uvicorn (23928) [simulator]" in text


def test_watch_snapshot(screen):
    display.display_watch_snapshot([_detail()])
    text = screen.export_text()
    assert "Listening now (1 port)" in text
    assert ":8012" in text
    assert "Watching for changes" in text
