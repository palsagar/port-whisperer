from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from portwhisperer import scanner
from portwhisperer.scanner import PortInfo


@pytest.fixture
def ports(fake_run, frozen_now) -> dict[int, PortInfo]:
    """Run ``get_listening_ports`` against the fixtures, keyed by port."""
    return {p.port: p for p in scanner.get_listening_ports()}


# ── get_listening_ports ───────────────────────────────────────────────────────


def test_one_entry_per_port_sorted(fake_run, frozen_now):
    # IPv4 and IPv6 lines for the same port collapse into one entry
    result = scanner.get_listening_ports()
    assert [p.port for p in result] == [
        1455,
        3000,
        3001,
        4000,
        5000,
        5432,
        7000,
        7265,
        8000,
        8012,
        9222,
        37777,
        54361,
        62813,
    ]


def test_port_pid_and_process_name(ports):
    assert ports[8012].pid == 23928
    assert ports[5000].pid == ports[7000].pid == 745
    # lsof +c 0 prints the full COMMAND name instead of 9 characters
    assert ports[8012].process_name == "python3.13"
    assert ports[5000].process_name == "ControlCenter"
    assert ports[54361].process_name == "magnitude-acn"
    # lsof escapes the space as \x20; the scanner decodes it
    assert ports[9222].process_name == "Google Chrome"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Google\\x20Chrome", "Google Chrome"),
        ("node", "node"),
        # Control characters stay escaped so they never reach the terminal
        ("evil\\x1b[2Jname", "evil\\x1b[2Jname"),
        ("bell\\x07", "bell\\x07"),
        ("del\\x7f", "del\\x7f"),
    ],
)
def test_decode_lsof_name(raw, expected):
    assert scanner._decode_lsof_name(raw) == expected


def test_lsof_asks_for_full_names(fake_run):
    scanner.get_listening_ports()
    (listen_call,) = fake_run.commands("-sTCP:LISTEN")
    assert "+c 0" in listen_call


def test_command_comes_from_ps(ports):
    assert ports[3000].command == (
        "node /Users/dev/projects/webapp/node_modules/.bin/next dev"
    )


def test_status(ports):
    assert ports[8012].status == "healthy"
    # ppid 1 and a dev runtime → detached (no parent shell)
    assert ports[37777].status == "detached"
    # ppid 1 but not a dev process → still healthy
    assert ports[54361].status == "healthy"
    assert ports[62813].status == "healthy"
    assert ports[3001].status == "zombie"


def test_memory(ports):
    assert ports[8012].memory == "10.9 MB"
    assert ports[1455].memory == "32.0 MB"
    assert ports[5432].memory == "2.0 GB"
    # Zombie reports rss 0 → no memory string
    assert ports[3001].memory is None


def test_start_time_and_uptime(ports):
    assert ports[8012].start_time == datetime(2026, 10, 12, 11, 37, 33)
    assert ports[8012].uptime == "22m 27s"
    assert ports[37777].uptime == "11h 57m"
    assert ports[1455].uptime == "30s"
    assert ports[5432].uptime == "1d 4h"
    # Day-of-month padded with two spaces ("Aug  8") still parses
    assert ports[54361].start_time == datetime(2026, 8, 8, 17, 35, 9)
    assert ports[54361].uptime == "64d 18h"


def test_docker_override(ports):
    pg = ports[5432]
    assert pg.process_name == "docker"
    assert pg.raw_name == "com.docker.backend"
    assert pg.project_name == "dev-postgres"
    assert pg.framework == "PostgreSQL"
    assert pg.cwd is None


def test_docker_not_queried_without_docker_process(fake_run, frozen_now):
    listing = fake_run.outputs["lsof_listen.txt"].splitlines()
    fake_run.outputs["lsof_listen.txt"] = "\n".join(
        line for line in listing if not line.startswith("com.docker")
    )
    scanner.get_listening_ports()
    assert fake_run.commands("docker") == []


def test_cwd_and_project(ports, project_root: Path):
    assert ports[8012].cwd == str(project_root / "simulator")
    assert ports[8012].project_name == "simulator"
    # cwd is webapp/src; the project root is the nearest package.json above it
    assert ports[3000].cwd == str(project_root / "webapp")
    assert ports[3000].project_name == "webapp"
    assert ports[62813].cwd is None


def test_framework(ports):
    assert ports[8012].framework == "FastAPI"
    assert ports[3000].framework == "Next.js"
    assert ports[3001].framework == "Node.js"
    assert ports[37777].framework is None
    # "next" inside the directory name "nextcloud-sync" no longer counts
    assert ports[4000].framework == "Node.js"
    # Versioned binary name falls back to Python
    assert ports[8000].framework == "Python"


def test_default_view_dev_filter(ports):
    dev = [
        p.port
        for p in ports.values()
        if scanner.is_dev_process(p.process_name, p.command)
    ]
    assert sorted(dev) == [3000, 3001, 4000, 5432, 8000, 8012, 37777]


def test_listing_skips_git_and_tree(fake_run, frozen_now):
    result = scanner.get_listening_ports()
    assert all(p.git_branch is None and p.process_tree == [] for p in result)
    assert fake_run.commands("git") == []
    assert fake_run.commands("pid=,ppid=,comm=") == []


def test_no_listeners(fake_run):
    fake_run.outputs["lsof_listen.txt"] = ""
    assert scanner.get_listening_ports() == []


# ── get_port_details ──────────────────────────────────────────────────────────


def test_port_details(fake_run, frozen_now):
    info = scanner.get_port_details(8012)
    assert info is not None
    assert info.git_branch == "main"
    # Walks up through uv and zsh, stopping where the parent is missing
    assert [p["pid"] for p in info.process_tree] == [23928, 23892, 23800]
    # Basenames, not full binary paths
    assert [p["name"] for p in info.process_tree] == ["python3", "uv", "-zsh"]


def test_port_details_looks_up_only_the_target(fake_run, frozen_now):
    # One git call and one process-table scan, however many ports are listening
    scanner.get_port_details(8012)
    assert len(fake_run.commands("git")) == 1
    assert len(fake_run.commands("pid=,ppid=,comm=")) == 1


def test_port_details_docker_skips_git(fake_run, frozen_now):
    info = scanner.get_port_details(5432)
    assert info is not None
    assert info.git_branch is None
    assert fake_run.commands("git") == []


def test_port_details_missing_port(fake_run, frozen_now):
    assert scanner.get_port_details(9999) is None


# ── get_all_processes ─────────────────────────────────────────────────────────


@pytest.fixture
def processes(fake_run, frozen_now) -> dict[int, scanner.ProcessInfo]:
    return {p.pid: p for p in scanner.get_all_processes()}


def test_all_processes_parsing(processes):
    # PID 1 is skipped and the duplicate 41000 row is kept once
    assert sorted(processes) == [126, 1500, 23928, 41000]
    node = processes[41000]
    assert node.process_name == "node"
    assert node.cpu == 12.5
    assert node.memory == "256.0 MB"
    assert node.description == "next dev"
    assert processes[23928].uptime == "22m 27s"


def test_all_processes_name_from_command(processes):
    assert processes[23928].process_name == "python3"
    assert processes[1500].process_name == "com.docker.backend"
    # The command is split on spaces, so an app path with spaces is cut short
    assert processes[126].process_name == "Brave"


def test_all_processes_framework_and_cwd(processes, project_root: Path):
    assert processes[23928].framework == "FastAPI"
    assert processes[23928].cwd == str(project_root / "simulator")
    assert processes[41000].project_name == "webapp"


def test_all_processes_skip_docker_cwd(fake_run, frozen_now):
    scanner.get_all_processes()
    (cwd_call,) = fake_run.commands("-d cwd")
    assert cwd_call.endswith("-p 126,23928,41000")


def test_all_processes_skip_own_pid(fake_run, frozen_now, monkeypatch):
    monkeypatch.setattr(scanner.os, "getpid", lambda: 126)
    assert 126 not in {p.pid for p in scanner.get_all_processes()}


# ── framework detection ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("command", "name", "expected"),
    [
        ("node /app/node_modules/.bin/next start", "node", "Next.js"),
        ("next-server (v14.2.0)", "node", "Next.js"),
        ("node /app/node_modules/vite/bin/vite.js", "node", "Vite"),
        ("nuxt dev", "node", "Nuxt"),
        ("ng serve --port 4200", "node", "Angular"),
        ("node /app/node_modules/.bin/ng serve --port 4200", "node", "Angular"),
        ("webpack serve", "node", "Webpack"),
        ("remix dev", "node", "Remix"),
        ("astro dev", "node", "Astro"),
        ("gatsby develop", "node", "Gatsby"),
        ("flask run", "python3", "Flask"),
        ("python manage.py runserver", "python", "Django"),
        ("uvicorn main:app --reload", "python3", "FastAPI"),
        ("python3 -m uvicorn main:app", "python3", "FastAPI"),
        ("ruby bin/rails server", "ruby", "Rails"),
        ("cargo run", "cargo", "Rust"),
        ("node server.js", "node", "Node.js"),
        ("", "python3", "Python"),
        ("", "java", "Java"),
        ("", "unknown", None),
        ("", "python3.13", "Python"),
        ("", "pythonista", None),
        ("django-admin runserver", "python3", "Django"),
        ("node /app/node_modules/.bin/webpack-dev-server", "node", "Webpack"),
        ("/usr/local/bin/cargo run --release", "cargo", "Rust"),
        # Framework words inside other words or directory names do not match
        ("node /Users/me/nextcloud-sync/server.js", "node", "Node.js"),
        ("node /srv/invite-service/index.js", "node", "Node.js"),
        ("python /data/disastrous/app.py", "python", "Python"),
        ("node /srv/escargot/index.js", "node", "Node.js"),
        ("python /srv/guardrails/app.py", "python", "Python"),
        ("node nextcloud-sync", "node", "Node.js"),
        # The package folder under node_modules is a framework signal too
        ("node /app/node_modules/@angular/cli/bin/ng.js serve", "node", "Angular"),
        ("node /app/node_modules/@remix-run/dev/dist/cli.js dev", "node", "Remix"),
        ("node /a/node_modules/gatsby/dist/commands/develop.js", "node", "Gatsby"),
    ],
)
def test_detect_framework_from_command(command, name, expected):
    assert scanner._detect_framework_from_command(command, name) == expected


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"package.json": {"dependencies": {"next": "14"}}}, "Next.js"),
        ({"package.json": {"dependencies": {"nuxt": "3"}}}, "Nuxt"),
        ({"package.json": {"devDependencies": {"@sveltejs/kit": "2"}}}, "SvelteKit"),
        ({"package.json": {"dependencies": {"svelte": "4"}}}, "Svelte"),
        ({"package.json": {"devDependencies": {"vite": "5", "react": "18"}}}, "Vite"),
        ({"package.json": {"dependencies": {"react": "18"}}}, "React"),
        ({"package.json": {"dependencies": {"express": "4"}}}, "Express"),
        ({"package.json": {"dependencies": {"@nestjs/core": "10"}}}, "NestJS"),
        ({"package.json": {}, "vite.config.ts": ""}, "Vite"),
        ({"next.config.mjs": ""}, "Next.js"),
        ({"angular.json": ""}, "Angular"),
        ({"Cargo.toml": ""}, "Rust"),
        ({"go.mod": ""}, "Go"),
        ({"manage.py": ""}, "Django"),
        ({"Gemfile": ""}, "Ruby"),
        ({"pyproject.toml": ""}, None),
        ({}, None),
        # Unreadable package.json falls through to config files
        ({"package.json": "{not json", "go.mod": ""}, "Go"),
    ],
)
def test_detect_framework(tmp_path: Path, files: dict, expected):
    for name, content in files.items():
        text = content if isinstance(content, str) else json.dumps(content)
        (tmp_path / name).write_text(text)
    assert scanner._detect_framework(str(tmp_path)) == expected


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("postgres:15", "PostgreSQL"),
        ("redis:7-alpine", "Redis"),
        ("mariadb:11", "MySQL"),
        ("mongo:7", "MongoDB"),
        ("nginx:latest", "nginx"),
        ("localstack/localstack", "LocalStack"),
        ("rabbitmq:3-management", "RabbitMQ"),
        ("confluentinc/cp-kafka", "Kafka"),
        ("opensearchproject/opensearch:2", "Elasticsearch"),
        ("minio/minio", "MinIO"),
        ("myorg/api:dev", "Docker"),
        ("", "Docker"),
    ],
)
def test_detect_framework_from_image(image, expected):
    assert scanner._detect_framework_from_image(image) == expected


# ── is_dev_process ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "command", "expected"),
    [
        ("node", "", True),
        ("python3", "", True),
        ("bun", "", True),
        ("com.docker.backend", "", True),
        ("docker", "", True),
        ("myapp", "node server.js", True),
        ("myapp", "next-server (v14.2.0)", True),
        ("myapp", "", False),
        ("zsh", "", False),
        # Blocklist wins over a matching command line
        ("Slack", "node /Applications/Slack.app/app.js", False),
        ("ControlCenter", "node x.js", False),
        ("code", "node extension-host.js", False),
        # Entry followed by a space or a dot still counts as the same app
        ("Code Helper (Plugin)", "node extension-host.js", False),
        ("Google Chrome", "node x.js", False),
        ("zoom.us", "node x.js", False),
        # A versioned binary still passes when its command has a dev pattern
        ("python3.13", "python3.13 -m uvicorn app:app", True),
        ("python3.12", "", True),
        ("python3.13", "python3.13 -m http.server", True),
        ("pythonista", "", False),
        # Blocklist entries do not hide tools that only share a prefix
        ("codex", "node /opt/codex/bin/codex.js", True),
        ("codex-code-mode-host", "node host.js", True),
        ("codegen", "node codegen.js", True),
        ("archiver", "node archive.js", True),
    ],
)
def test_is_dev_process(name, command, expected):
    assert scanner.is_dev_process(name, command) is expected


# ── subprocess safety ─────────────────────────────────────────────────────────


def test_every_call_is_an_argument_list(fake_run, frozen_now):
    scanner.get_port_details(8012)
    scanner.get_all_processes()
    assert fake_run.calls
    assert all(isinstance(call, list) for call in fake_run.calls)


def test_git_directory_is_one_argument(fake_run):
    # A hostile directory name must reach git untouched, never a shell
    directory = '/tmp/a"b/$(touch pwned)'
    assert scanner._get_git_branch(directory) == "main"
    assert fake_run.calls == [
        ["git", "-C", directory, "rev-parse", "--abbrev-ref", "HEAD"]
    ]


def test_missing_binaries(monkeypatch):
    def missing(args, **kwargs):
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(subprocess, "run", missing)
    # Every subprocess call degrades to an empty result
    assert scanner._batch_ps_info([1]) == {}
    assert scanner._batch_cwd([1]) == {}
    assert scanner._batch_docker_info() == {}
    assert scanner._get_process_tree(1) == []
    assert scanner._get_git_branch("/tmp") is None
    assert scanner.get_listening_ports() == []
    assert scanner.get_all_processes() == []


# ── find_clean_candidates ─────────────────────────────────────────────────────


def _drop_daemon_flag(fake_run) -> None:
    """Make the bun worker an ordinary detached process."""
    ps = fake_run.outputs["ps_batch.txt"]
    fake_run.outputs["ps_batch.txt"] = ps.replace(" --daemon", "")


def test_clean_skips_daemon_flag(fake_run, frozen_now):
    # The bun worker is detached but runs with --daemon; the zombie stays
    assert [p.port for p in scanner.find_clean_candidates()] == [3001]


def test_clean_offers_detached_non_daemon(fake_run, frozen_now):
    _drop_daemon_flag(fake_run)
    assert [p.port for p in scanner.find_clean_candidates()] == [3001, 37777]


def test_clean_skips_launchd_jobs(fake_run, frozen_now):
    _drop_daemon_flag(fake_run)
    fake_run.outputs["launchctl_list.txt"] += "27660\t0\thomebrew.mxcl.worker\n"
    assert [p.port for p in scanner.find_clean_candidates()] == [3001]


def test_launchd_pids(fake_run):
    assert scanner._launchd_pids() == {98426, 679}


# ── listen address, tool names, detail extras (#6) ───────────────────────────


def test_listen_address(ports):
    assert ports[3000].address == "*"
    assert ports[8012].address == "127.0.0.1"
    assert ports[5000].address == "*"


def test_widest_address_wins(fake_run, frozen_now):
    # The IPv4 loopback row comes first, but the port is open on all interfaces
    fake_run.outputs["lsof_listen.txt"] += (
        "node  41000  dev  30u  IPv4 0x1  0t0  TCP 127.0.0.1:9000 (LISTEN)\n"
        "node  41000  dev  31u  IPv6 0x2  0t0  TCP *:9000 (LISTEN)\n"
        "node  41000  dev  32u  IPv4 0x3  0t0  TCP 127.0.0.1:9001 (LISTEN)\n"
        "node  41000  dev  33u  IPv6 0x4  0t0  TCP [::1]:9001 (LISTEN)\n"
    )
    ports = {p.port: p for p in scanner.get_listening_ports()}
    assert ports[9000].address == "*"
    assert ports[9001].address == "127.0.0.1"


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("127.0.0.1", True),
        ("127.0.0.53", True),
        ("[::1]", True),
        ("*", False),
        ("192.168.1.5", False),
        (None, False),
    ],
)
def test_is_loopback(address, expected):
    assert scanner.is_loopback(address) is expected


def test_tool_names(ports):
    assert ports[8012].tool == "uvicorn"
    assert ports[3000].tool == "next"
    assert ports[8000].tool == "http.server"
    assert ports[4000].tool is None
    # The interpreter name stays for the detail view and the dev filter
    assert ports[8012].process_name == "python3.13"


@pytest.mark.parametrize(
    ("command", "name", "expected"),
    [
        ("python3 /app/.venv/bin/uvicorn app:app --reload", "python3.13", "uvicorn"),
        ("python -m flask run", "python", "flask"),
        ("python -m mymodule", "python", None),
        ("node /app/node_modules/vite/bin/vite.js", "node", "vite"),
        ("node /app/node_modules/.bin/next dev", "node", "next"),
        ("node --inspect /app/node_modules/.bin/nodemon x.js", "node", "nodemon"),
        ("node server.js --mode vite", "node", None),
        ("node", "node", None),
        # A native binary already shows its own name
        ("/usr/local/bin/vite", "vite", None),
        ("bun /repo/node_modules/.bin/astro dev", "bun", "astro"),
    ],
)
def test_detect_tool(command, name, expected):
    assert scanner._detect_tool(command, name) == expected


def test_all_processes_tool(processes):
    assert processes[23928].tool == "uvicorn"


def test_port_details_other_ports(fake_run, frozen_now):
    info = scanner.get_port_details(5000)
    assert info is not None
    assert info.other_ports == [7000]


def test_port_details_docker_has_image_and_no_other_ports(fake_run, frozen_now):
    info = scanner.get_port_details(5432)
    assert info is not None
    assert info.image == "postgres:15"
    assert info.other_ports == []


# ── user config file (#9) ─────────────────────────────────────────────────────


def test_config_adds_dev_process(no_user_config):
    assert not scanner.is_dev_process("air", "air")
    scanner._user_process_lists.cache_clear()
    no_user_config.parent.mkdir(parents=True)
    no_user_config.write_text('dev_processes = ["Air", "hugo"]\n')
    assert scanner.is_dev_process("air", "air")
    assert scanner.is_dev_process("hugo", "hugo server")


def test_config_ignores_process_over_builtin(no_user_config):
    no_user_config.parent.mkdir(parents=True)
    no_user_config.write_text('ignore_processes = ["node"]\n')
    assert not scanner.is_dev_process("node", "node server.js")


def test_config_ignore_beats_dev(no_user_config):
    no_user_config.parent.mkdir(parents=True)
    no_user_config.write_text(
        'dev_processes = ["ollama"]\nignore_processes = ["ollama"]\n'
    )
    assert not scanner.is_dev_process("ollama", "ollama serve")


def test_config_can_show_a_blocked_app(no_user_config):
    no_user_config.parent.mkdir(parents=True)
    no_user_config.write_text('dev_processes = ["code"]\n')
    assert scanner.is_dev_process("code", "")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("dev_processes = [", "ignoring"),
        ('dev_processes = "air"', "'dev_processes' must be a list of strings"),
        ("ignore_processes = [1, 2]", "'ignore_processes' must be a list"),
        ('dev_process = ["air"]', "unknown key 'dev_process'"),
    ],
)
def test_bad_config_warns_once_and_uses_builtins(no_user_config, capsys, text, message):
    no_user_config.parent.mkdir(parents=True)
    no_user_config.write_text(text)
    assert scanner.is_dev_process("node", "node x")
    assert not scanner.is_dev_process("air", "air")
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert message in err
    assert str(no_user_config) in err


def test_config_hides_port_from_default_view(fake_run, frozen_now, no_user_config):
    no_user_config.parent.mkdir(parents=True)
    no_user_config.write_text('ignore_processes = ["bun"]\n')
    ports = scanner.get_listening_ports()
    shown = [p.port for p in ports if scanner.is_dev_process(p.process_name, p.command)]
    assert 37777 not in shown


def test_config_respects_xdg_and_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "x"))
    assert scanner._config_path() == tmp_path / "x" / "portwhisperer" / "config.toml"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert scanner._config_path() == (
        tmp_path / ".config" / "portwhisperer" / "config.toml"
    )


# ── watch_ports (#7) ──────────────────────────────────────────────────────────


def _watch(monkeypatch, polls: list[list[PortInfo]], dev_only: bool = True):
    """Run ``watch_ports`` over a fixed series of polls and return the events."""
    series = iter(polls)
    monkeypatch.setattr(scanner, "get_listening_ports", lambda: next(series))
    sleeps = iter(range(len(polls) - 1))

    def fake_sleep(seconds: float) -> None:
        if next(sleeps, None) is None:
            raise KeyboardInterrupt

    monkeypatch.setattr(scanner.time, "sleep", fake_sleep)
    events: list[tuple[str, object]] = []
    scanner.watch_ports(lambda kind, data: events.append((kind, data)), 0.2, dev_only)
    return [
        (kind, [p.port for p in data] if isinstance(data, list) else data.pid)
        for kind, data in events
    ]


def _p(port: int, pid: int, name: str = "node") -> PortInfo:
    return PortInfo(port=port, pid=pid, process_name=name, raw_name=name, command="")


def test_watch_snapshot_then_changes(monkeypatch):
    events = _watch(
        monkeypatch,
        [
            [_p(3000, 1), _p(4000, 2)],
            [_p(3000, 1), _p(4000, 2), _p(5000, 3)],
            [_p(3000, 1), _p(5000, 3)],
        ],
    )
    assert events == [("snapshot", [3000, 4000]), ("new", 3), ("closed", 2)]


def test_watch_detects_restart_on_same_port(monkeypatch):
    events = _watch(monkeypatch, [[_p(3000, 1)], [_p(3000, 9)]])
    assert events == [("snapshot", [3000]), ("restarted", 9)]


def test_watch_filters_to_dev_processes(monkeypatch):
    polls = [[_p(3000, 1), _p(7000, 2, "ControlCenter")], [_p(3000, 1)]]
    assert _watch(monkeypatch, polls) == [("snapshot", [3000])]


def test_watch_all_shows_everything(monkeypatch):
    polls = [[_p(3000, 1), _p(7000, 2, "ControlCenter")], [_p(3000, 1)]]
    assert _watch(monkeypatch, polls, dev_only=False) == [
        ("snapshot", [3000, 7000]),
        ("closed", 2),
    ]


# ── ports here (#8) ───────────────────────────────────────────────────────────


def test_find_project_root_prefers_git(monkeypatch, tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "package.json").write_text("{}")

    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, f"{tmp_path}\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert scanner.find_project_root(str(tmp_path / "pkg")) == str(tmp_path.resolve())


def test_find_project_root_without_git(monkeypatch, tmp_path):
    (tmp_path / "pkg" / "src").mkdir(parents=True)
    (tmp_path / "pkg" / "package.json").write_text("{}")

    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 128, "", "not a git repository")

    monkeypatch.setattr(subprocess, "run", run)
    assert scanner.find_project_root(str(tmp_path / "pkg" / "src")) == str(
        (tmp_path / "pkg").resolve()
    )
