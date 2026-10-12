from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class PortInfo:
    port: int
    pid: int
    process_name: str
    raw_name: str
    command: str
    cwd: str | None = None
    project_name: str | None = None
    framework: str | None = None
    uptime: str | None = None
    start_time: datetime | None = None
    status: str = "healthy"  # "healthy" | "orphaned" | "zombie"
    memory: str | None = None
    git_branch: str | None = None
    process_tree: list = field(default_factory=list)


@dataclass
class ProcessInfo:
    pid: int
    process_name: str
    command: str
    description: str
    cpu: float
    memory: str | None = None
    cwd: str | None = None
    project_name: str | None = None
    framework: str | None = None
    uptime: str | None = None


# ── private helpers ───────────────────────────────────────────────────────────

# python, python3, and versioned binaries such as python3.12 or python3.13
_PYTHON_RE = re.compile(r"python(3(\.\d+)?)?")


def _batch_ps_info(pids: list[int]) -> dict[int, dict]:
    """Single ps call to get ppid/stat/rss/lstart/command for all PIDs.

    Parameters
    ----------
    pids : list[int]
        PIDs to query.

    Returns
    -------
    dict[int, dict]
        Mapping of pid → {ppid, stat, rss, lstart, command}.

    Examples
    --------
    .. code-block:: python

        info = _batch_ps_info([1234, 5678])
        print(info[1234]["command"])
    """
    result: dict[int, dict] = {}
    if not pids:
        return result

    pid_list = ",".join(str(p) for p in pids)
    try:
        proc = subprocess.run(
            ["ps", "-p", pid_list, "-o", "pid=,ppid=,stat=,rss=,lstart=,command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in proc.stdout.strip().splitlines():
            if not line.strip():
                continue
            # Format: PID PPID STAT RSS DOW MON DD HH:MM:SS YYYY COMMAND...
            m = re.match(
                r"^(\d+)\s+(\d+)\s+(\S+)\s+(\d+)\s+\w+\s+(\w+\s+\d+\s+[\d:]+\s+\d+)\s+(.*)$",
                line.strip(),
            )
            if not m:
                continue
            pid = int(m.group(1))
            result[pid] = {
                "ppid": int(m.group(2)),
                "stat": m.group(3),
                "rss": int(m.group(4)),
                "lstart": m.group(5),
                "command": m.group(6),
            }
    except (
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
        FileNotFoundError,
    ):
        pass
    return result


def _batch_cwd(pids: list[int]) -> dict[int, str]:
    """Single lsof call to get cwd for all PIDs.

    Parameters
    ----------
    pids : list[int]
        PIDs to query.

    Returns
    -------
    dict[int, str]
        Mapping of pid → cwd path.

    Examples
    --------
    .. code-block:: python

        cwds = _batch_cwd([1234, 5678])
        print(cwds.get(1234))
    """
    result: dict[int, str] = {}
    if not pids:
        return result

    pid_list = ",".join(str(p) for p in pids)
    try:
        proc = subprocess.run(
            ["lsof", "-a", "-d", "cwd", "-p", pid_list],
            capture_output=True,
            text=True,
            timeout=10,
        )
        lines = proc.stdout.strip().splitlines()
        for line in lines[1:]:  # skip header
            parts = line.split()
            if len(parts) < 9:
                continue
            pid = int(parts[1])
            path = " ".join(parts[8:])
            if path and path.startswith("/"):
                result[pid] = path
    except (
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
        FileNotFoundError,
    ):
        pass
    return result


def _batch_docker_info() -> dict[int, dict]:
    """Single docker ps call to get container info keyed by host port.

    Returns
    -------
    dict[int, dict]
        Mapping of host port → {name, image}.

    Examples
    --------
    .. code-block:: python

        docker = _batch_docker_info()
        print(docker.get(5432))
    """
    result: dict[int, dict] = {}
    try:
        proc = subprocess.run(
            ["docker", "ps", "--format", "{{.Ports}}\\t{{.Names}}\\t{{.Image}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in proc.stdout.strip().splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            ports_str, name = parts[0], parts[1]
            image = parts[2] if len(parts) > 2 else ""

            # Parse port mappings like "0.0.0.0:5432->5432/tcp, [::]:5432->5432/tcp"
            seen: set[int] = set()
            for m in re.finditer(r"(?:\d+\.\d+\.\d+\.\d+|::):(\d+)->", ports_str):
                port = int(m.group(1))
                if port not in seen:
                    seen.add(port)
                    result[port] = {"name": name, "image": image}
    except (
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
        FileNotFoundError,
    ):
        pass
    return result


def _detect_framework(project_root: str) -> str | None:
    """Detect framework by reading package.json deps and config files.

    Parameters
    ----------
    project_root : str
        Absolute path to the project root directory.

    Returns
    -------
    str | None
        Framework name, or None if undetected.

    Examples
    --------
    .. code-block:: python

        fw = _detect_framework("/home/user/myapp")
        print(fw)  # "Next.js"
    """
    pkg_path = os.path.join(project_root, "package.json")
    if os.path.exists(pkg_path):
        try:
            pkg = json.loads(Path(pkg_path).read_text())
            all_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}

            if "next" in all_deps:
                return "Next.js"
            if "nuxt" in all_deps or "nuxt3" in all_deps:
                return "Nuxt"
            if "@sveltejs/kit" in all_deps:
                return "SvelteKit"
            if "svelte" in all_deps:
                return "Svelte"
            if "@remix-run/react" in all_deps or "remix" in all_deps:
                return "Remix"
            if "astro" in all_deps:
                return "Astro"
            if "vite" in all_deps:
                return "Vite"
            if "@angular/core" in all_deps:
                return "Angular"
            if "vue" in all_deps:
                return "Vue"
            if "react" in all_deps:
                return "React"
            if "express" in all_deps:
                return "Express"
            if "fastify" in all_deps:
                return "Fastify"
            if "hono" in all_deps:
                return "Hono"
            if "koa" in all_deps:
                return "Koa"
            if "nestjs" in all_deps or "@nestjs/core" in all_deps:
                return "NestJS"
            if "gatsby" in all_deps:
                return "Gatsby"
            if "webpack-dev-server" in all_deps:
                return "Webpack"
            if "esbuild" in all_deps:
                return "esbuild"
            if "parcel" in all_deps:
                return "Parcel"
        except Exception:
            pass

    # Fall back to config file presence
    if os.path.exists(os.path.join(project_root, "vite.config.ts")) or os.path.exists(
        os.path.join(project_root, "vite.config.js")
    ):
        return "Vite"
    if os.path.exists(os.path.join(project_root, "next.config.js")) or os.path.exists(
        os.path.join(project_root, "next.config.mjs")
    ):
        return "Next.js"
    if os.path.exists(os.path.join(project_root, "angular.json")):
        return "Angular"
    if os.path.exists(os.path.join(project_root, "Cargo.toml")):
        return "Rust"
    if os.path.exists(os.path.join(project_root, "go.mod")):
        return "Go"
    if os.path.exists(os.path.join(project_root, "manage.py")):
        return "Django"
    if os.path.exists(os.path.join(project_root, "Gemfile")):
        return "Ruby"

    return None


def _detect_framework_from_command(command: str, process_name: str) -> str | None:
    """Detect framework by inspecting the command string.

    Parameters
    ----------
    command : str
        Full command string of the process.
    process_name : str
        Short process name (basename).

    Returns
    -------
    str | None
        Framework name, or None if undetected.

    Examples
    --------
    .. code-block:: python

        fw = _detect_framework_from_command(
            "node /app/node_modules/.bin/next start", "node"
        )
        print(fw)  # "Next.js"
    """
    if not command:
        return _detect_framework_from_name(process_name)

    # Keep only the basename of each non-flag argument, so directory names in
    # paths (e.g. /srv/nextcloud-sync/server.js) cannot trigger a match
    cmd = " ".join(
        os.path.basename(part)
        for part in command.lower().split()
        if not part.startswith("-")
    )

    # Whole words only — "invite" must not match Vite, "guardrails" not Rails
    patterns = [
        (re.compile(r"\bnext\b"), "Next.js"),
        (re.compile(r"\bvite\b"), "Vite"),
        (re.compile(r"\bnuxt\b"), "Nuxt"),
        (re.compile(r"\bangular\b|\bng serve\b"), "Angular"),
        (re.compile(r"\bwebpack\b"), "Webpack"),
        (re.compile(r"\bremix\b"), "Remix"),
        (re.compile(r"\bastro\b"), "Astro"),
        (re.compile(r"\bgatsby\b"), "Gatsby"),
        (re.compile(r"\bflask\b"), "Flask"),
        (re.compile(r"\bdjango\b|\bmanage\.py\b"), "Django"),
        (re.compile(r"\buvicorn\b"), "FastAPI"),
        (re.compile(r"\brails\b"), "Rails"),
        (re.compile(r"\bcargo\b|\brustc\b"), "Rust"),
    ]
    for pattern, framework in patterns:
        if pattern.search(cmd):
            return framework

    return _detect_framework_from_name(process_name)


def _detect_framework_from_image(image: str) -> str | None:
    """Detect framework/service from a Docker image name.

    Parameters
    ----------
    image : str
        Docker image name.

    Returns
    -------
    str | None
        Service name (always returns a string for Docker images).

    Examples
    --------
    .. code-block:: python

        fw = _detect_framework_from_image("postgres:15")
        print(fw)  # "PostgreSQL"
    """
    if not image:
        return "Docker"
    img = image.lower()
    if "postgres" in img:
        return "PostgreSQL"
    if "redis" in img:
        return "Redis"
    if "mysql" in img or "mariadb" in img:
        return "MySQL"
    if "mongo" in img:
        return "MongoDB"
    if "nginx" in img:
        return "nginx"
    if "localstack" in img:
        return "LocalStack"
    if "rabbitmq" in img:
        return "RabbitMQ"
    if "kafka" in img:
        return "Kafka"
    if "elasticsearch" in img or "opensearch" in img:
        return "Elasticsearch"
    if "minio" in img:
        return "MinIO"
    return "Docker"


def _detect_framework_from_name(process_name: str) -> str | None:
    """Fallback framework detection by process name alone.

    Parameters
    ----------
    process_name : str
        Short process name.

    Returns
    -------
    str | None
        Framework name, or None if undetected.

    Examples
    --------
    .. code-block:: python

        fw = _detect_framework_from_name("python3")
        print(fw)  # "Python"
    """
    name = (process_name or "").lower()
    if name == "node":
        return "Node.js"
    if _PYTHON_RE.fullmatch(name):
        return "Python"
    if name == "ruby":
        return "Ruby"
    if name == "java":
        return "Java"
    if name == "go":
        return "Go"
    return None


def _find_project_root(directory: str) -> str:
    """Walk up directory tree looking for a project root marker file.

    Parameters
    ----------
    directory : str
        Starting directory path.

    Returns
    -------
    str
        Nearest ancestor directory containing a marker file, or ``directory``
        if none found within 15 levels.

    Examples
    --------
    .. code-block:: python

        root = _find_project_root("/home/user/myapp/src/components")
        print(root)  # "/home/user/myapp"
    """
    markers = [
        "package.json",
        "Cargo.toml",
        "go.mod",
        "pyproject.toml",
        "Gemfile",
        "pom.xml",
        "build.gradle",
    ]
    current = directory
    depth = 0
    while current != "/" and depth < 15:
        for marker in markers:
            if os.path.exists(os.path.join(current, marker)):
                return current
        current = os.path.dirname(current)
        depth += 1
    return directory


def _format_uptime(seconds: float) -> str:
    """Format a duration in seconds as a human-readable string.

    Parameters
    ----------
    seconds : float
        Elapsed seconds.

    Returns
    -------
    str
        Human-readable duration like "2d 3h", "5h 12m", "3m 45s", or "30s".

    Examples
    --------
    .. code-block:: python

        print(_format_uptime(3723))  # "1h 2m"
    """
    secs = int(seconds)
    minutes = secs // 60
    hours = minutes // 60
    days = hours // 24

    if days > 0:
        return f"{days}d {hours % 24}h"
    if hours > 0:
        return f"{hours}h {minutes % 60}m"
    if minutes > 0:
        return f"{minutes}m {secs % 60}s"
    return f"{secs}s"


def _format_memory(rss_kb: int) -> str:
    """Format RSS memory in KB as a human-readable string.

    Parameters
    ----------
    rss_kb : int
        Memory in kilobytes.

    Returns
    -------
    str
        Human-readable memory like "1.2 GB", "512.3 MB", or "128 KB".

    Examples
    --------
    .. code-block:: python

        print(_format_memory(2097152))  # "2.0 GB"
    """
    if rss_kb > 1_048_576:
        return f"{rss_kb / 1_048_576:.1f} GB"
    if rss_kb > 1024:
        return f"{rss_kb / 1024:.1f} MB"
    return f"{rss_kb} KB"


def _summarize_command(command: str, process_name: str) -> str:
    """Extract a short, human-readable description from a full command string.

    Skips the binary path and flags; shows up to 3 meaningful tokens.

    Parameters
    ----------
    command : str
        Full command string.
    process_name : str
        Process name used as fallback.

    Returns
    -------
    str
        Short description like "server.js" or "manage.py runserver".

    Examples
    --------
    .. code-block:: python

        desc = _summarize_command(
            "node /users/foo/project/server.js --port 3000", "node"
        )
        print(desc)  # "server.js"
    """
    cmd = command or ""
    parts = cmd.split()
    meaningful: list[str] = []
    for i, part in enumerate(parts):
        if i == 0:  # skip binary path
            continue
        if part.startswith("-"):  # skip flags
            continue
        # Use basename for file paths
        meaningful.append(os.path.basename(part) if "/" in part else part)
        if len(meaningful) >= 3:
            break

    if meaningful:
        return " ".join(meaningful)
    return process_name


def _get_process_tree(pid: int) -> list[dict]:
    """Walk the process tree from pid up to init, returning ancestry chain.

    Parameters
    ----------
    pid : int
        Starting process ID.

    Returns
    -------
    list[dict]
        List of dicts with keys pid, ppid, name — from ``pid`` up toward root.

    Examples
    --------
    .. code-block:: python

        tree = _get_process_tree(1234)
        for proc in tree:
            print(proc["pid"], proc["name"])
    """
    tree: list[dict] = []
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,comm="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        processes: dict[int, dict] = {}
        for line in proc.stdout.strip().splitlines():
            parts = line.strip().split()
            if len(parts) < 3:
                continue
            p = int(parts[0])
            pp = int(parts[1])
            processes[p] = {"pid": p, "ppid": pp, "name": " ".join(parts[2:])}

        current_pid = pid
        depth = 0
        while current_pid > 1 and depth < 8:
            proc_info = processes.get(current_pid)
            if not proc_info:
                break
            tree.append(proc_info)
            current_pid = proc_info["ppid"]
            depth += 1
    except (
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
        FileNotFoundError,
    ):
        pass
    return tree


def _get_git_branch(directory: str) -> str | None:
    """Return the current git branch of ``directory``, or None.

    Parameters
    ----------
    directory : str
        Directory inside a git work tree.

    Returns
    -------
    str | None
        Branch name, or None when the directory is not a git repository.

    Examples
    --------
    .. code-block:: python

        print(_get_git_branch("/home/user/myapp"))  # "main"
    """
    try:
        git_proc = subprocess.run(
            # Argument list, not a shell string: the directory comes from lsof
            # and may contain quotes or $(...)
            ["git", "-C", directory, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return git_proc.stdout.strip() or None
    except (
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
        FileNotFoundError,
    ):
        return None


def _decode_lsof_name(name: str) -> str:
    """Decode the ``\\xNN`` escapes that lsof writes into the COMMAND column.

    lsof escapes spaces and other unsafe bytes so each line splits cleanly on
    whitespace, e.g. ``Google\\x20Chrome``.

    Parameters
    ----------
    name : str
        Raw COMMAND value from lsof.

    Returns
    -------
    str
        The name with printable ASCII escapes decoded. Control characters and
        non-ASCII bytes stay escaped.

    Examples
    --------
    .. code-block:: python

        print(_decode_lsof_name("Google\\\\x20Chrome"))  # "Google Chrome"
    """

    def _unescape(m: re.Match[str]) -> str:
        char = chr(int(m.group(1), 16))
        # A process picks its own name, so never let it emit terminal control codes
        return char if char.isascii() and char.isprintable() else m.group(0)

    return re.sub(r"\\x([0-9a-fA-F]{2})", _unescape, name)


# ── public API ────────────────────────────────────────────────────────────────


def is_dev_process(name: str, command: str) -> bool:
    """Return True if the process looks like a dev server rather than a system app.

    Used for orphan detection and filtering the table view.

    Parameters
    ----------
    name : str
        Process name (basename).
    command : str
        Full command string.

    Returns
    -------
    bool
        True when the process matches known dev runtimes or command patterns.

    Examples
    --------
    .. code-block:: python

        print(is_dev_process("node", "node server.js"))  # True
        print(is_dev_process("slack", ""))               # False
    """
    n = (name or "").lower()
    cmd = (command or "").lower()

    # Known system/desktop apps — not dev processes
    system_apps = [
        "spotify",
        "raycast",
        "tableplus",
        "postman",
        "linear",
        "cursor",
        "controlcenter",
        "rapportd",
        "superhuman",
        "setappagent",
        "slack",
        "discord",
        "firefox",
        "chrome",
        "google",
        "safari",
        "figma",
        "notion",
        "zoom",
        "teams",
        "code",
        "iterm2",
        "warp",
        "arc",
        "loginwindow",
        "windowserver",
        "systemuiserver",
        "kernel_task",
        "launchd",
        "mdworker",
        "mds_stores",
        "cfprefsd",
        "coreaudio",
        "corebrightnessd",
        "airportd",
        "bluetoothd",
        "sharingd",
        "usernoted",
        "notificationcenter",
        "cloudd",
    ]
    for app in system_apps:
        # Exact name, or the name followed by a space or dot ("Code Helper",
        # "zoom.us"), so "code" does not hide "codex" or "codegen"
        if n == app or n.startswith((app + " ", app + ".")):
            return False

    # Dev process names — exact match on basename
    dev_names = {
        "node",
        "ruby",
        "java",
        "go",
        "cargo",
        "deno",
        "bun",
        "php",
        "uvicorn",
        "gunicorn",
        "flask",
        "rails",
        "npm",
        "npx",
        "yarn",
        "pnpm",
        "tsc",
        "tsx",
        "esbuild",
        "rollup",
        "turbo",
        "nx",
        "jest",
        "vitest",
        "mocha",
        "pytest",
        "cypress",
        "playwright",
        "rustc",
        "dotnet",
        "gradle",
        "mvn",
        "mix",
        "elixir",
    }
    if n in dev_names or _PYTHON_RE.fullmatch(n):
        return True

    # Docker processes (prefix match)
    if n.startswith("com.docke") or n in ("docker", "docker-sandbox"):
        return True

    # Command-line keyword matching — whole words or clear prefixes only
    cmd_indicators = [
        re.compile(r"\bnode\b"),
        re.compile(r"\bnext[\s\-]"),
        re.compile(r"\bvite\b"),
        re.compile(r"\bnuxt\b"),
        re.compile(r"\bwebpack\b"),
        re.compile(r"\bremix\b"),
        re.compile(r"\bastro\b"),
        re.compile(r"\bgulp\b"),
        re.compile(r"\bng serve\b"),
        re.compile(r"\bgatsb"),
        re.compile(r"\bflask\b"),
        re.compile(r"\bdjango\b|manage\.py"),
        re.compile(r"\buvicorn\b"),
        re.compile(r"\brails\b"),
        re.compile(r"\bcargo\b"),
    ]
    return any(pattern.search(cmd) for pattern in cmd_indicators)


def get_listening_ports() -> list[PortInfo]:
    """Return all TCP ports in LISTEN state with process and project metadata.

    Uses a single ``lsof`` call and batches all follow-up lookups to avoid
    N×6 individual shell calls. Git branch and process tree are left empty;
    :func:`get_port_details` fills them for one port.

    Returns
    -------
    list[PortInfo]
        Ports sorted by port number ascending.

    Examples
    --------
    .. code-block:: python

        for p in get_listening_ports():
            print(p.port, p.process_name, p.framework)
    """
    try:
        proc = subprocess.run(
            # +c 0 stops lsof from cutting process names to 9 characters
            ["lsof", "+c", "0", "-iTCP", "-sTCP:LISTEN", "-P", "-n"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        raw = proc.stdout
    except (
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
        FileNotFoundError,
    ):
        return []

    lines = raw.strip().splitlines()
    if not lines:
        return []

    port_map: dict[int, bool] = {}
    entries: list[dict] = []

    for line in lines[1:]:  # skip header
        parts = line.split()
        if len(parts) < 9:
            continue

        process_name = _decode_lsof_name(parts[0])
        pid = int(parts[1])
        name_field = parts[8]

        m = re.search(r":(\d+)$", name_field)
        if not m:
            continue
        port = int(m.group(1))

        # Keep only first entry per port
        if port in port_map:
            continue
        port_map[port] = True
        entries.append({"port": port, "pid": pid, "process_name": process_name})

    # Deduplicate PIDs for batch calls
    unique_pids = list({e["pid"] for e in entries})

    ps_map = _batch_ps_info(unique_pids)
    cwd_map = _batch_cwd(unique_pids)

    # Only call docker if we actually have docker processes in the list
    has_docker = any(
        e["process_name"].startswith("com.docke") or e["process_name"] == "docker"
        for e in entries
    )
    docker_map = _batch_docker_info() if has_docker else {}

    results: list[PortInfo] = []
    for entry in entries:
        port = entry["port"]
        pid = entry["pid"]
        process_name = entry["process_name"]

        ps = ps_map.get(pid)
        cwd = cwd_map.get(pid)

        info = PortInfo(
            port=port,
            pid=pid,
            process_name=process_name,
            raw_name=process_name,
            command=ps["command"] if ps else "",
        )

        # Status and uptime from batched ps
        if ps:
            if "Z" in ps["stat"]:
                info.status = "zombie"
            elif ps["ppid"] == 1 and is_dev_process(process_name, ps["command"]):
                info.status = "orphaned"

            if ps["rss"] > 0:
                info.memory = _format_memory(ps["rss"])

            if ps["lstart"]:
                try:
                    info.start_time = datetime.strptime(
                        ps["lstart"], "%b %d %H:%M:%S %Y"
                    )
                    elapsed = (datetime.now() - info.start_time).total_seconds()
                    info.uptime = _format_uptime(elapsed)
                except ValueError:
                    pass

            # Framework from command line — no extra shell call needed
            info.framework = _detect_framework_from_command(ps["command"], process_name)

        # Docker container overrides
        docker = docker_map.get(port)
        if docker:
            info.project_name = docker["name"]
            info.framework = _detect_framework_from_image(docker["image"])
            info.process_name = "docker"

        # cwd + project + framework from batched lsof (skip if docker already set)
        if cwd and not docker:
            project_root = _find_project_root(cwd)
            info.cwd = project_root
            info.project_name = os.path.basename(project_root)
            info.framework = info.framework or _detect_framework(project_root)

        results.append(info)

    results.sort(key=lambda p: p.port)
    return results


def get_all_processes() -> list[ProcessInfo]:
    """Return all running processes with metadata, like ``ps aux`` but structured.

    Used by ``ports ps``.

    Returns
    -------
    list[ProcessInfo]
        All non-trivial processes with framework and project metadata.

    Examples
    --------
    .. code-block:: python

        for proc in get_all_processes():
            print(proc.pid, proc.process_name, proc.cpu)
    """
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,pcpu=,pmem=,rss=,lstart=,command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        raw = proc.stdout.strip()
    except (
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
        FileNotFoundError,
    ):
        return []

    entries: list[dict] = []
    seen: set[int] = set()
    my_pid = os.getpid()

    for line in raw.splitlines():
        if not line.strip():
            continue
        m = re.match(
            r"^(\d+)\s+([\d.]+)\s+([\d.]+)\s+(\d+)\s+\w+\s+(\w+\s+\d+\s+[\d:]+\s+\d+)\s+(.*)$",
            line.strip(),
        )
        if not m:
            continue

        pid = int(m.group(1))
        if pid <= 1 or pid == my_pid or pid in seen:
            continue
        seen.add(pid)

        command = m.group(6)
        process_name = os.path.basename(command.split()[0]) if command.split() else ""

        entries.append(
            {
                "pid": pid,
                "process_name": process_name,
                "cpu": float(m.group(2)),
                "mem_percent": float(m.group(3)),
                "rss": int(m.group(4)),
                "lstart": m.group(5),
                "command": command,
            }
        )

    # Batch cwd lookup — skip Docker processes (their cwd is useless)
    non_docker = [
        e
        for e in entries
        if not e["process_name"].startswith("com.docke")
        and not e["process_name"].startswith("Docker")
        and e["process_name"] not in ("docker", "docker-sandbox")
    ]
    cwd_map = _batch_cwd([e["pid"] for e in non_docker])

    results: list[ProcessInfo] = []
    for e in entries:
        info = ProcessInfo(
            pid=e["pid"],
            process_name=e["process_name"],
            command=e["command"],
            description=_summarize_command(e["command"], e["process_name"]),
            cpu=e["cpu"],
            memory=_format_memory(e["rss"]) if e["rss"] > 0 else None,
        )

        if e["lstart"]:
            try:
                start_time = datetime.strptime(e["lstart"], "%b %d %H:%M:%S %Y")
                elapsed = (datetime.now() - start_time).total_seconds()
                info.uptime = _format_uptime(elapsed)
            except ValueError:
                pass

        info.framework = _detect_framework_from_command(e["command"], e["process_name"])

        cwd = cwd_map.get(e["pid"])
        if cwd:
            project_root = _find_project_root(cwd)
            info.cwd = project_root
            info.project_name = os.path.basename(project_root)
            info.framework = info.framework or _detect_framework(project_root)

        results.append(info)

    return results


def get_port_details(port: int) -> PortInfo | None:
    """Return detailed info for a single port, or None if not listening.

    Parameters
    ----------
    port : int
        The TCP port number to look up.

    Returns
    -------
    PortInfo | None
        Port info with git branch and process tree populated.

    Examples
    --------
    .. code-block:: python

        details = get_port_details(3000)
        if details:
            print(details.framework, details.git_branch)
    """
    for info in get_listening_ports():
        if info.port == port:
            # Only the requested port pays for the git and process tree lookups
            if info.cwd:
                info.git_branch = _get_git_branch(info.cwd)
            info.process_tree = _get_process_tree(info.pid)
            return info
    return None


def find_orphaned_processes() -> list[PortInfo]:
    """Return listening ports whose process status is orphaned or zombie.

    Returns
    -------
    list[PortInfo]
        Ports with status "orphaned" or "zombie".

    Examples
    --------
    .. code-block:: python

        for p in find_orphaned_processes():
            print(p.port, p.status)
    """
    ports = get_listening_ports()
    return [p for p in ports if p.status in ("orphaned", "zombie")]


def kill_process(pid: int, sig: int = signal.SIGTERM) -> bool:
    """Send a signal to a process.

    Parameters
    ----------
    pid : int
        Target process ID.
    sig : int
        Signal to send (default: SIGTERM).

    Returns
    -------
    bool
        True on success, False if the process was not found or permission denied.

    Examples
    --------
    .. code-block:: python

        success = kill_process(1234)
        print(success)  # True
    """
    try:
        os.kill(pid, sig)
        return True
    except OSError:
        return False


def watch_ports(callback: Callable, interval: float = 2.0) -> None:
    """Poll listening ports and invoke callback on changes.

    Runs indefinitely until ``KeyboardInterrupt``.

    Parameters
    ----------
    callback : Callable
        Called with ``("new", PortInfo)`` for new ports and
        ``("removed", {"port": int})`` for removed ports.
    interval : float
        Polling interval in seconds (default: 2.0).

    Examples
    --------
    .. code-block:: python

        def on_change(event, data):
            print(event, data)

        watch_ports(on_change)
    """
    previous_ports: set[int] = set()

    def check() -> None:
        nonlocal previous_ports
        current = get_listening_ports()
        current_set = {p.port for p in current}

        for p in current:
            if p.port not in previous_ports:
                callback("new", p)

        for port in previous_ports:
            if port not in current_set:
                callback("removed", {"port": port})

        previous_ports = current_set

    check()
    try:
        while True:
            time.sleep(interval)
            check()
    except KeyboardInterrupt:
        pass
