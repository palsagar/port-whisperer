from __future__ import annotations

import signal
import subprocess
from collections.abc import Callable

import pytest

from portwhisperer import scanner
from portwhisperer.scanner import PortInfo

Signals = list[tuple[int, int]]


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace the scanner's clock so polling loops finish instantly."""
    now = [0.0]

    def fake_sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr(scanner.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(scanner.time, "sleep", fake_sleep)
    return now


@pytest.fixture
def signals(monkeypatch: pytest.MonkeyPatch, clock: list[float]) -> Signals:
    """Record every signal the scanner sends instead of sending it."""
    sent: Signals = []
    monkeypatch.setattr(scanner.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    return sent


def _listeners(
    monkeypatch: pytest.MonkeyPatch, fn: Callable[[], set[int] | None]
) -> list[int]:
    """Serve ``_port_listener_pids`` from ``fn`` and count the calls."""
    calls: list[int] = []

    def fake(port: int) -> set[int] | None:
        calls.append(port)
        return fn()

    monkeypatch.setattr(scanner, "_port_listener_pids", fake)
    return calls


def _node(**overrides: object) -> PortInfo:
    fields: dict = dict(
        port=3000, pid=41000, process_name="node", raw_name="node", command="node x"
    )
    fields.update(overrides)
    return PortInfo(**fields)


# ── _port_listener_pids ───────────────────────────────────────────────────────


def test_listener_pids_parses_lsof(fake_run):
    assert scanner._port_listener_pids(3000) == {41000}
    (call,) = fake_run.commands("lsof -t")
    assert "-iTCP:3000" in call
    assert "-sTCP:LISTEN" in call


def test_listener_pids_empty_when_port_is_free(fake_run):
    fake_run.outputs["lsof_pids.txt"] = ""
    assert scanner._port_listener_pids(3000) == set()


# ── free_port: normal processes ───────────────────────────────────────────────


def test_sigterm_frees_port(monkeypatch, signals):
    _listeners(monkeypatch, lambda: set() if signals else {41000})
    result = scanner.free_port(_node())
    assert signals == [(41000, signal.SIGTERM)]
    assert result.ok
    assert result.port == 3000


def test_sigterm_ignored_escalates_to_sigkill(monkeypatch, signals, clock):
    sigkilled = lambda: (41000, signal.SIGKILL) in signals  # noqa: E731
    _listeners(monkeypatch, lambda: set() if sigkilled() else {41000})
    result = scanner.free_port(_node())
    assert signals == [(41000, signal.SIGTERM), (41000, signal.SIGKILL)]
    # SIGKILL only after the SIGTERM grace period ran out
    assert clock[0] >= scanner._TERM_TIMEOUT
    assert result.ok


def test_force_sends_sigkill_only(monkeypatch, signals):
    _listeners(monkeypatch, lambda: set() if signals else {41000})
    result = scanner.free_port(_node(), force=True)
    assert signals == [(41000, signal.SIGKILL)]
    assert result.ok


def test_unkillable_process_fails(monkeypatch, signals):
    _listeners(monkeypatch, lambda: {41000})
    result = scanner.free_port(_node())
    assert not result.ok
    assert "41000" in result.detail


def test_process_already_gone(monkeypatch, clock):
    def gone(pid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(scanner.os, "kill", gone)
    _listeners(monkeypatch, lambda: set())
    assert scanner.free_port(_node()).ok


def test_permission_denied(monkeypatch, clock):
    def denied(pid: int, sig: int) -> None:
        raise PermissionError

    monkeypatch.setattr(scanner.os, "kill", denied)
    _listeners(monkeypatch, lambda: {41000})
    result = scanner.free_port(_node())
    assert not result.ok
    assert "sudo" in result.detail


def test_port_taken_again_by_new_pid(monkeypatch, signals):
    # A watcher such as nodemon restarts the server under a new PID
    _listeners(monkeypatch, lambda: {50000} if signals else {41000})
    result = scanner.free_port(_node())
    assert signals == [(41000, signal.SIGTERM)]
    assert not result.ok
    assert "50000" in result.detail


def test_lsof_failure_is_not_success(monkeypatch, signals):
    _listeners(monkeypatch, lambda: None)
    assert not scanner.free_port(_node()).ok


# ── free_port: zombies and Docker ─────────────────────────────────────────────


def test_zombie_gets_no_signal(monkeypatch, signals):
    calls = _listeners(monkeypatch, lambda: {41300})
    zombie = _node(port=3001, pid=41300, ppid=40990, status="zombie")
    result = scanner.free_port(zombie)
    assert signals == []
    assert calls == []
    assert not result.ok
    assert "zombie" in result.detail
    assert "40990" in result.detail


def test_docker_port_stops_container(monkeypatch, fake_run, signals):
    _listeners(monkeypatch, lambda: set() if fake_run.commands("docker") else {1500})
    info = _node(
        port=5432,
        pid=1500,
        process_name="docker",
        raw_name="com.docker.backend",
        container="dev-postgres",
    )
    result = scanner.free_port(info)
    assert fake_run.commands("docker") == ["docker stop dev-postgres"]
    # The Docker backend must never receive a signal
    assert signals == []
    assert result.ok


def test_docker_force_kills_container(monkeypatch, fake_run, signals):
    _listeners(monkeypatch, lambda: set())
    info = _node(port=5432, pid=1500, raw_name="com.docker.backend", container="pg")
    scanner.free_port(info, force=True)
    assert fake_run.commands("docker") == ["docker kill pg"]
    assert signals == []


def test_docker_stop_failure(monkeypatch, fake_run, signals):
    def failing(args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args, 1, "", "Error: no such container")

    monkeypatch.setattr(subprocess, "run", failing)
    _listeners(monkeypatch, lambda: {1500})
    info = _node(port=5432, pid=1500, raw_name="com.docker.backend", container="pg")
    result = scanner.free_port(info)
    assert not result.ok
    assert "no such container" in result.detail


def test_docker_without_container_is_refused(monkeypatch, signals):
    calls = _listeners(monkeypatch, lambda: {1500})
    info = _node(port=5432, pid=1500, raw_name="com.docker.backend")
    result = scanner.free_port(info)
    assert signals == []
    assert calls == []
    assert not result.ok
    assert "Docker" in result.detail


def test_listening_ports_fill_ppid_and_container(fake_run, frozen_now):
    ports = {p.port: p for p in scanner.get_listening_ports()}
    assert ports[3001].ppid == 40990
    assert ports[5432].container == "dev-postgres"
    assert ports[3000].container is None


# ── review fixes ──────────────────────────────────────────────────────────────


def test_container_only_for_docker_processes(fake_run, frozen_now):
    # A native postgres row on :5432 must not inherit the container's name,
    # or ports kill 5432 would stop an unrelated container
    header, docker_row, rest = fake_run.outputs["lsof_listen.txt"].split("\n", 2)
    native = "postgres  2000  dev  7u  IPv4 0x1  0t0  TCP 127.0.0.1:5432 (LISTEN)"
    # Docker still listens on another port, so docker ps runs
    other = docker_row.replace("*:5432", "*:6379")
    fake_run.outputs["lsof_listen.txt"] = "\n".join(
        [header, native, docker_row, other, rest]
    )
    ports = {p.port: p for p in scanner.get_listening_ports()}
    assert ports[5432].pid == 2000
    assert ports[5432].container is None


def test_pid_no_longer_listening_gets_no_signal(monkeypatch, signals):
    # The server exited during the prompt; its PID may now belong to someone else
    _listeners(monkeypatch, lambda: {50000})
    result = scanner.free_port(_node())
    assert signals == []
    assert not result.ok


def test_other_listener_from_before_is_not_called_a_restart(monkeypatch, signals):
    # Prefork servers: several PIDs already shared the socket before the kill
    _listeners(monkeypatch, lambda: {50000} if signals else {41000, 50000})
    result = scanner.free_port(_node())
    assert signals == [(41000, signal.SIGTERM)]
    assert not result.ok
    assert "restarted" not in result.detail
    assert "50000" in result.detail


def test_docker_stderr_control_characters_are_removed(monkeypatch, signals):
    def failing(args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args, 1, "", "bad\x1b[2Jthing\x07")

    monkeypatch.setattr(subprocess, "run", failing)
    _listeners(monkeypatch, lambda: {1500})
    info = _node(port=5432, pid=1500, raw_name="com.docker.backend", container="pg")
    detail = scanner.free_port(info).detail
    assert "\x1b" not in detail
    assert "\x07" not in detail
