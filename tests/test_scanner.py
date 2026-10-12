from __future__ import annotations

import json
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
    assert "+c 0" in str(listen_call)


def test_command_comes_from_ps(ports):
    assert ports[3000].command == (
        "node /Users/dev/projects/webapp/node_modules/.bin/next dev"
    )


def test_status(ports):
    assert ports[8012].status == "healthy"
    # ppid 1 and a dev runtime → orphaned
    assert ports[37777].status == "orphaned"
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
    assert info.process_tree[1]["name"] == "/opt/homebrew/bin/uv"


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
    assert "126,23928,41000" in str(cwd_call)


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
        ("ControlCenter", "", False),
        ("code", "node extension-host.js", False),
        # A versioned binary still passes when its command has a dev pattern
        ("python3.13", "python3.13 -m uvicorn app:app", True),
        ("python3.12", "", True),
        ("python3.13", "python3.13 -m http.server", True),
        ("pythonista", "", False),
        # bug: see #2 — blocklist prefixes hide unrelated tools
        ("codex", "node /opt/codex/bin/codex.js", False),
        ("codegen", "node codegen.js", False),
        ("archiver", "node archive.js", False),
    ],
)
def test_is_dev_process(name, command, expected):
    assert scanner.is_dev_process(name, command) is expected
