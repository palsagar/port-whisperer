from __future__ import annotations

import signal
import subprocess
from collections.abc import Callable

import pytest

from portwhisperer import scanner
from portwhisperer.scanner import PortInfo

# Full container ID of dev-postgres in tests/fixtures/docker_ps.txt
PG_ID = "3f9a1c0e5b7d2a4c6e8f0a1b3c5d7e9f1a2b4c6d8e0f1a3b5c7d9e1f2a4b6c8d"

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
    (call,) = fake_run.commands("lsof -w -t")
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


def _backend(**overrides: object) -> PortInfo:
    fields: dict = dict(
        port=5432,
        pid=1500,
        raw_name="com.docker.backend",
        container="dev-postgres",
        container_id=PG_ID,
    )
    fields.update(overrides)
    return _node(**fields)


def _docker_run(fake_run, returncode: int, stderr: str):
    """Fail docker stop/kill but serve every other command from the fixtures."""

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        if args[:2] in (["docker", "stop"], ["docker", "kill"]):
            fake_run.calls.append(args)
            return subprocess.CompletedProcess(args, returncode, "", stderr)
        return fake_run(args, **kwargs)

    return run


def test_docker_port_stops_container(monkeypatch, fake_run, signals):
    stopped = lambda: fake_run.commands("docker stop")  # noqa: E731
    _listeners(monkeypatch, lambda: set() if stopped() else {1500})
    result = scanner.free_port(_backend(process_name="docker"))
    # Stop by ID: a name can be reused by a different container
    assert fake_run.commands("docker stop") == [f"docker stop {PG_ID}"]
    # The Docker backend must never receive a signal
    assert signals == []
    assert result.ok


def test_docker_force_kills_container(monkeypatch, fake_run, signals):
    killed = lambda: fake_run.commands("docker kill")  # noqa: E731
    _listeners(monkeypatch, lambda: set() if killed() else {1500})
    assert scanner.free_port(_backend(), force=True).ok
    assert fake_run.commands("docker kill") == [f"docker kill {PG_ID}"]
    assert fake_run.commands("docker stop") == []
    assert signals == []


def test_docker_stop_failure(monkeypatch, fake_run, signals):
    monkeypatch.setattr(
        subprocess, "run", _docker_run(fake_run, 1, "Error: no such container")
    )
    _listeners(monkeypatch, lambda: {1500})
    result = scanner.free_port(_backend())
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
    assert ports[5432].container_id == PG_ID
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


def test_docker_stderr_control_characters_are_removed(monkeypatch, fake_run, signals):
    monkeypatch.setattr(subprocess, "run", _docker_run(fake_run, 1, "bad\x1b[2J\x07"))
    _listeners(monkeypatch, lambda: {1500})
    detail = scanner.free_port(_backend()).detail
    assert "bad[2J" in detail
    assert "\x1b" not in detail
    assert "\x07" not in detail


# ── challenge-review fixes ────────────────────────────────────────────────────


def _ps(*rows: tuple[str, str, str]) -> str:
    """Build docker ps output from (id, ports, name) rows."""
    return "".join(f"{cid}\t{ports}\t{name}\timage:1\n" for cid, ports, name in rows)


def test_docker_map_ignores_udp(fake_run):
    fake_run.outputs["docker_ps.txt"] = _ps(
        ("aaa", "0.0.0.0:5432->5432/tcp", "dev-postgres"),
        ("bbb", "0.0.0.0:5432->5432/udp", "metrics"),
    )
    assert scanner._batch_docker_info()[5432]["id"] == "aaa"


def test_docker_map_drops_ambiguous_ports(fake_run):
    # Two containers publish TCP :5432 on different addresses; neither is safe
    fake_run.outputs["docker_ps.txt"] = _ps(
        ("aaa", "127.0.0.1:5432->5432/tcp", "pg-a"),
        ("bbb", "127.0.0.2:5432->5432/tcp", "pg-b"),
    )
    assert 5432 not in scanner._batch_docker_info()


def test_docker_map_drops_ambiguous_ports_across_families(fake_run):
    fake_run.outputs["docker_ps.txt"] = _ps(
        ("aaa", "0.0.0.0:5432->5432/tcp", "pg-a"),
        ("bbb", "[::]:5432->5432/tcp", "pg-b"),
    )
    assert 5432 not in scanner._batch_docker_info()


@pytest.mark.parametrize("binding", ["[::]:5432", ":::5432", "[::1]:5432"])
def test_docker_map_reads_ipv6_only(fake_run, binding):
    fake_run.outputs["docker_ps.txt"] = _ps(("aaa", f"{binding}->5432/tcp", "pg"))
    assert scanner._batch_docker_info()[5432]["id"] == "aaa"


def test_docker_same_container_on_both_families_is_not_ambiguous(fake_run):
    assert scanner._batch_docker_info()[5432]["id"] == PG_ID


def test_docker_lists_full_ids(fake_run):
    scanner._batch_docker_info()
    (call,) = fake_run.commands("docker ps")
    assert "--no-trunc" in call


def test_docker_changed_container_is_refused(monkeypatch, fake_run, signals):
    # Between the prompt and the kill, another container took the port
    fake_run.outputs["docker_ps.txt"] = _ps(("ccc", "0.0.0.0:5432->5432/tcp", "other"))
    _listeners(monkeypatch, lambda: {1500})
    result = scanner.free_port(_backend())
    assert fake_run.commands("docker stop") == []
    assert not result.ok
    assert "other" in result.detail


def test_docker_recreated_container_with_same_name_is_refused(
    monkeypatch, fake_run, signals
):
    fake_run.outputs["docker_ps.txt"] = _ps(
        ("ddd", "0.0.0.0:5432->5432/tcp", "dev-postgres")
    )
    _listeners(monkeypatch, lambda: {1500})
    assert not scanner.free_port(_backend()).ok
    assert fake_run.commands("docker stop") == []


def test_docker_port_already_free_is_not_stopped(monkeypatch, fake_run, signals):
    _listeners(monkeypatch, lambda: set())
    assert scanner.free_port(_backend()).ok
    assert fake_run.commands("docker stop") == []


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "expected"),
    [
        (0, "41000\n", "", {41000}),
        # lsof exits 1 with no output when nothing matches
        (1, "", "", set()),
        # ...and also exits 1 on a real error, but then it explains on stderr
        (1, "", "lsof: unknown service", None),
        (2, "", "", None),
    ],
)
def test_listener_pids_exit_codes(monkeypatch, returncode, stdout, stderr, expected):
    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    monkeypatch.setattr(subprocess, "run", run)
    assert scanner._port_listener_pids(3000) == expected


def test_no_sigkill_when_the_port_cannot_be_checked(monkeypatch, signals):
    # The first check sees the process; after SIGTERM lsof keeps failing.
    # Without proof that it still holds the port, never escalate.
    _listeners(monkeypatch, lambda: None if signals else {41000})
    result = scanner.free_port(_node())
    assert signals == [(41000, signal.SIGTERM)]
    assert not result.ok
