from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
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
    status: str = "healthy"  # "healthy" | "detached" | "zombie"
    memory: str | None = None
    git_branch: str | None = None
    process_tree: list = field(default_factory=list)
    ppid: int | None = None
    container: str | None = None  # Docker container name for container ports
    container_id: str | None = None  # full ID; kills target this, not the name
    address: str | None = None  # listen address from lsof: "*", "127.0.0.1", "[::1]"
    tool: str | None = None  # tool run by an interpreter, e.g. "uvicorn" for python
    image: str | None = None  # Docker image for container ports
    other_ports: list[int] = field(default_factory=list)  # same PID; detail only


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
    tool: str | None = None  # tool run by an interpreter, e.g. "vite" for node


class ScanError(RuntimeError):
    """Raised when ``lsof`` cannot list the listening ports.

    An empty list would claim that every port is free, so callers that act
    on "free" (``ports <port>``, ``ports next``) must see the failure.
    """


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


def _batch_docker_info(env: dict[str, str] | None = None) -> dict[int, dict]:
    """Single docker ps call to get container info keyed by host port.

    Only TCP bindings count. A port that two containers publish is left out,
    because a kill could not tell which container to stop.

    Parameters
    ----------
    env : dict[str, str] | None
        Environment for the docker call, to pin the endpoint. None inherits
        the current environment.

    Returns
    -------
    dict[int, dict]
        Mapping of host port → {id, name, image}. ``id`` is the full ID.

    Examples
    --------
    .. code-block:: python

        docker = _batch_docker_info()
        print(docker.get(5432))
    """
    result: dict[int, dict] = {}
    ambiguous: set[int] = set()
    try:
        proc = subprocess.run(
            [
                "docker",
                "ps",
                "--no-trunc",
                "--format",
                "{{.ID}}\\t{{.Ports}}\\t{{.Names}}\\t{{.Image}}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            env=env,
        )
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            container_id, ports_str, name = parts[0], parts[1], parts[2]
            image = parts[3] if len(parts) > 3 else ""

            # Parse mappings like "0.0.0.0:5432->5432/tcp, [::]:5432->5432/tcp"
            # (older Docker writes ":::5432"). Skip UDP, which never shows up
            # as a TCP listener.
            pattern = r"(?:\d+\.\d+\.\d+\.\d+|\[[0-9a-fA-F:.]*\]|::):(\d+)->\d+/tcp"
            for m in re.finditer(pattern, ports_str):
                port = int(m.group(1))
                if port in result and result[port]["id"] != container_id:
                    ambiguous.add(port)
                result[port] = {"id": container_id, "name": name, "image": image}
    except (
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
        FileNotFoundError,
    ):
        pass
    for port in ambiguous:
        del result[port]
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
    tokens: list[str] = []
    packages: list[str] = []
    for part in command.lower().split():
        if part.startswith("-"):
            continue
        tokens.append(os.path.basename(part))
        # The package folder under node_modules names the tool being run,
        # e.g. node_modules/@angular/cli/bin/ng.js
        packages.extend(re.findall(r"node_modules/((?:@[^/]+/)?[^/]+)", part))
    # Packages go last so they never split adjacent words such as "ng serve"
    cmd = " ".join(tokens + packages)

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
            # comm is the full binary path on macOS; the basename is enough
            name = os.path.basename(" ".join(parts[2:]))
            processes[p] = {"pid": p, "ppid": pp, "name": name}

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


# Interpreters whose process name hides the tool they run
_INTERPRETERS = {"node", "bun", "deno", "ruby"}

# Tools worth naming in the PROCESS column instead of the interpreter
_KNOWN_TOOLS = {
    "uvicorn",
    "gunicorn",
    "hypercorn",
    "daphne",
    "granian",
    "fastapi",
    "flask",
    "celery",
    "streamlit",
    "jupyter",
    "jupyter-lab",
    "http.server",
    "mkdocs",
    "vite",
    "next",
    "nuxt",
    "astro",
    "remix",
    "webpack",
    "webpack-dev-server",
    "esbuild",
    "parcel",
    "gatsby",
    "tsx",
    "ts-node",
    "nodemon",
    "vitest",
    "jest",
    "storybook",
    "wrangler",
    "serve",
    "http-server",
    "ng",
    "rails",
    "puma",
}


def _detect_tool(command: str, process_name: str) -> str | None:
    """Return the known tool that an interpreter runs, from its command line.

    Only the script (or ``-m`` module) right after the interpreter counts, so
    an argument such as ``--port vite`` never names the tool.

    Parameters
    ----------
    command : str
        Full command string.
    process_name : str
        Process name; only interpreters (node, python, ...) are inspected.

    Returns
    -------
    str | None
        Tool name such as ``"uvicorn"`` or ``"vite"``, or None.

    Examples
    --------
    .. code-block:: python

        _detect_tool("python3 .venv/bin/uvicorn app:app", "python3.13")  # "uvicorn"
        _detect_tool("node node_modules/vite/bin/vite.js", "node")       # "vite"
    """
    name = (process_name or "").lower()
    if name not in _INTERPRETERS and not _PYTHON_RE.fullmatch(name):
        return None
    parts = (command or "").split()[1:]  # skip the interpreter itself
    for i, part in enumerate(parts):
        if part == "-m" and i + 1 < len(parts):
            module = parts[i + 1]
            return module if module in _KNOWN_TOOLS else None
        if part.startswith("-"):
            continue
        # node_modules/<package>/bin/x.js names the package being run
        package = re.search(r"node_modules/(?!\.bin/)((?:@[^/]+/)?[^/]+)/", part)
        script = re.sub(r"\.(c|m)?js$|\.py$|\.ts$", "", os.path.basename(part))
        for candidate in (script, package.group(1) if package else None):
            if candidate in _KNOWN_TOOLS:
                return candidate
        return None
    return None


def _address_rank(address: str) -> int:
    """Rank a listen address by how widely it can be reached.

    Parameters
    ----------
    address : str
        Host part of an lsof NAME, e.g. ``"*"``, ``"127.0.0.1"`` or ``"[::1]"``.

    Returns
    -------
    int
        2 for all interfaces, 1 for one network address, 0 for loopback.

    Examples
    --------
    .. code-block:: python

        _address_rank("*")          # 2
        _address_rank("127.0.0.1")  # 0
    """
    if address in ("*", "0.0.0.0", "[::]"):
        return 2
    if is_loopback(address):
        return 0
    return 1


def is_loopback(address: str | None) -> bool:
    """Return True when a listen address is reachable from this machine only.

    Parameters
    ----------
    address : str | None
        Host part of an lsof NAME, e.g. ``"127.0.0.1"`` or ``"[::1]"``.

    Returns
    -------
    bool
        True for 127.0.0.0/8, ``::1`` and ``localhost``.

    Examples
    --------
    .. code-block:: python

        is_loopback("127.0.0.1")  # True
        is_loopback("*")          # False
    """
    if not address:
        return False
    return address.startswith("127.") or address in ("[::1]", "localhost")


def _config_path() -> Path:
    """Return the user config file path, honouring ``XDG_CONFIG_HOME``.

    Returns
    -------
    Path
        ``$XDG_CONFIG_HOME/portwhisperer/config.toml``, or
        ``~/.config/portwhisperer/config.toml`` when the variable is unset.

    Examples
    --------
    .. code-block:: python

        print(_config_path())
    """
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "portwhisperer" / "config.toml"


@cache
def _user_process_lists() -> tuple[frozenset[str], frozenset[str]]:
    """Read the dev and ignored process names from the user config file.

    The file is optional. When it holds invalid TOML, an unknown key, or a
    value that is not a list of strings, one warning goes to stderr and the
    whole file is ignored. The result is cached for the life of the process.

    Returns
    -------
    tuple[frozenset[str], frozenset[str]]
        Lower-cased ``dev_processes`` and ``ignore_processes``.

    Examples
    --------
    .. code-block:: python

        dev, ignored = _user_process_lists()
    """
    empty: tuple[frozenset[str], frozenset[str]] = (frozenset(), frozenset())
    path = _config_path()
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return empty
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        print(f"portwhisperer: ignoring {path}: {exc}", file=sys.stderr)
        return empty

    keys = ("dev_processes", "ignore_processes")
    unknown = sorted(set(data) - set(keys))
    bad = [
        k
        for k in keys
        if k in data
        and not (isinstance(data[k], list) and all(isinstance(v, str) for v in data[k]))
    ]
    if unknown or bad:
        problem = (
            f"unknown key {unknown[0]!r}"
            if unknown
            else f"{bad[0]!r} must be a list of strings"
        )
        print(f"portwhisperer: ignoring {path}: {problem}", file=sys.stderr)
        return empty
    dev, ignored = (frozenset(v.lower() for v in data.get(k, [])) for k in keys)
    return dev, ignored


# ── public API ────────────────────────────────────────────────────────────────


def is_dev_process(name: str, command: str) -> bool:
    """Return True if the process looks like a dev server rather than a system app.

    Used for detached-process detection and filtering the table view. Names in
    the user config file (``ignore_processes``, then ``dev_processes``) win
    over the built-in lists.

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

    # The user's own lists come first, so they can correct any built-in entry
    user_dev, user_ignored = _user_process_lists()
    if n in user_ignored:
        return False
    if n in user_dev:
        return True

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

    Raises
    ------
    ScanError
        When ``lsof`` is missing, times out, or reports an error.

    Examples
    --------
    .. code-block:: python

        for p in get_listening_ports():
            print(p.port, p.process_name, p.framework)
    """
    try:
        proc = subprocess.run(
            # +c 0 stops lsof from cutting process names to 9 characters;
            # -w hides harmless warnings, so anything on stderr is a real error
            ["lsof", "-w", "+c", "0", "-iTCP", "-sTCP:LISTEN", "-P", "-n"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError as exc:
        raise ScanError("lsof is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise ScanError("lsof did not answer within 10 seconds") from exc
    raw = proc.stdout
    # lsof exits 1 both for "no match" and for errors; only errors print
    if proc.returncode != 0 and not raw.strip() and proc.stderr.strip():
        error = re.sub(r"[\x00-\x1f\x7f]", " ", proc.stderr.strip())
        raise ScanError(f"lsof failed: {error}")

    lines = raw.strip().splitlines()
    if not lines:
        return []

    by_port: dict[int, dict] = {}
    entries: list[dict] = []

    for line in lines[1:]:  # skip header
        parts = line.split()
        if len(parts) < 9:
            continue

        process_name = _decode_lsof_name(parts[0])
        pid = int(parts[1])
        name_field = parts[8]

        m = re.search(r"^(.*):(\d+)$", name_field)
        if not m:
            continue
        address, port = m.group(1), int(m.group(2))

        # Keep the first entry per port, but the widest address of all its
        # rows: reachability from the network is what the address is for
        if port in by_port:
            entry = by_port[port]
            if _address_rank(address) > _address_rank(entry["address"]):
                entry["address"] = address
            continue
        by_port[port] = {
            "port": port,
            "pid": pid,
            "process_name": process_name,
            "address": address,
        }
        entries.append(by_port[port])

    # Deduplicate PIDs for batch calls
    unique_pids = list({e["pid"] for e in entries})

    ps_map = _batch_ps_info(unique_pids)
    cwd_map = _batch_cwd(unique_pids)

    def _command(pid: int) -> str:
        return ps_map[pid]["command"] if pid in ps_map else ""

    # Only call docker if a container engine holds one of the ports
    has_docker = any(
        container_engine(e["process_name"], _command(e["pid"])) for e in entries
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
            address=entry["address"],
        )

        # Status and uptime from batched ps
        if ps:
            info.ppid = ps["ppid"]
            if "Z" in ps["stat"]:
                info.status = "zombie"
            # Parent PID 1 means the process outlived the shell that started it.
            # That is a fact, not a fault: nohup jobs and daemons look the same.
            elif ps["ppid"] == 1 and is_dev_process(process_name, ps["command"]):
                info.status = "detached"

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
        # Only a container engine's listener stands for a container. A native server
        # that shares the port number must not inherit the container's name.
        engine = container_engine(process_name, info.command)
        docker = docker_map.get(port) if engine else None
        if docker:
            info.project_name = docker["name"]
            info.container = docker["name"]
            info.container_id = docker["id"]
            info.image = docker["image"] or None
            info.framework = _detect_framework_from_image(docker["image"])
            info.process_name = "docker"
        else:
            info.tool = _detect_tool(info.command, process_name)

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
        info.tool = _detect_tool(e["command"], e["process_name"])

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
    ports = get_listening_ports()
    for info in ports:
        if info.port == port:
            # Only the requested port pays for the git and process tree lookups
            if info.cwd:
                info.git_branch = _get_git_branch(info.cwd)
            info.process_tree = _get_process_tree(info.pid)
            # Docker's listener holds every container port, so "other ports"
            # would list unrelated containers
            if not info.container:
                info.other_ports = [
                    p.port for p in ports if p.pid == info.pid and p.port != port
                ]
            return info
    return None


def _launchd_pids() -> set[int]:
    """Return the PIDs of running jobs that launchd manages for this user.

    ``brew services`` and other launch agents show up here.

    Returns
    -------
    set[int]
        PIDs from the first column of ``launchctl list``; empty on failure.

    Examples
    --------
    .. code-block:: python

        if 1234 in _launchd_pids():
            print("managed by launchd")
    """
    try:
        proc = subprocess.run(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return set()
    pids: set[int] = set()
    for line in proc.stdout.splitlines()[1:]:  # skip header
        parts = line.split()
        # Jobs that are loaded but not running show "-" instead of a PID
        if parts and parts[0].isdigit():
            pids.add(int(parts[0]))
    return pids


def find_clean_candidates() -> list[PortInfo]:
    """Return the dev servers that ``ports clean`` may offer to kill.

    These are zombies, plus detached processes that are not intentional
    daemons. A detached process counts as a daemon, and is left out, when:

    - its command line contains ``--daemon``, or
    - launchd manages it for this user (its PID is in ``launchctl list``),
      as with ``brew services``. Jobs started with ``sudo brew services``
      belong to root's launchd domain and are not detected.

    pm2 needs no rule: its processes are children of the pm2 daemon, not of
    PID 1, so they are never detached.

    Returns
    -------
    list[PortInfo]
        Candidate ports, sorted by port number.

    Examples
    --------
    .. code-block:: python

        for p in find_clean_candidates():
            print(p.port, p.status)
    """
    ports = get_listening_ports()
    # Only pay for the launchctl call when there is a detached process to check
    launchd = _launchd_pids() if any(p.status == "detached" for p in ports) else set()
    return [
        p
        for p in ports
        if p.status == "zombie"
        or (
            p.status == "detached"
            and "--daemon" not in p.command
            and p.pid not in launchd
        )
    ]


@dataclass
class KillResult:
    port: int
    pid: int | None  # None when nothing listened on the port to begin with
    ok: bool  # True only when nothing listens on the port any more
    detail: str


# Seconds to wait for the port after SIGTERM, then after SIGKILL or docker stop
_TERM_TIMEOUT = 3.0
_KILL_TIMEOUT = 2.0
_POLL_INTERVAL = 0.1


def is_docker_process(name: str) -> bool:
    """Return True when the process name belongs to the Docker family.

    Parameters
    ----------
    name : str
        Process name to check.

    Returns
    -------
    bool
        True for com.docker.*, Docker*, docker, and docker-sandbox.

    Examples
    --------
    .. code-block:: python

        is_docker_process("com.docker.backend")  # True
        is_docker_process("node")                # False
    """
    return (
        name.startswith("com.docke")
        or name.startswith("Docker")
        or name in ("docker", "docker-sandbox")
    )


# Process names that container_engine can classify only with the command line
_NEEDS_COMMAND = {"ssh"}


def container_engine(name: str, command: str = "") -> str | None:
    """Name the container engine that a process belongs to, if any.

    Container engines forward published ports from a VM through one host
    process. Signalling that process drops every port it forwards, so the kill
    path must recognise all of them, not only Docker Desktop.

    Parameters
    ----------
    name : str
        Process name.
    command : str
        Full command string. Needed to tell Lima's ``ssh`` forwarder from an
        ordinary ssh tunnel.

    Returns
    -------
    str | None
        ``"Docker"``, ``"OrbStack"``, ``"Lima"`` (Colima uses Lima),
        ``"Rancher Desktop"`` or ``"Podman"``; None for other processes.

    Examples
    --------
    .. code-block:: python

        container_engine("OrbStack Helper")  # "OrbStack"
        container_engine("ssh", "ssh -F /Users/me/.colima/_lima/colima/ssh.config")
        # "Lima"
    """
    if is_docker_process(name) or name.startswith("vpnkit"):
        return "Docker"
    if name.startswith("OrbStack"):
        return "OrbStack"
    if name.startswith("Rancher Desktop"):
        return "Rancher Desktop"
    if name == "limactl":
        return "Lima"
    if name == "gvproxy":
        return "Podman"
    # Lima (and so Colima) forwards ports over ssh with its own ssh config
    if name == "ssh" and re.search(r"/\.lima/|/_lima/|/\.colima/", command):
        return "Lima"
    return None


def _port_listener_pids(port: int) -> set[int] | None:
    """Return the PIDs that listen on a TCP port.

    Parameters
    ----------
    port : int
        The TCP port to check.

    Returns
    -------
    set[int] | None
        The listening PIDs, an empty set when the port is free, or None when
        ``lsof`` could not run.

    Examples
    --------
    .. code-block:: python

        if not _port_listener_pids(3000):
            print("port 3000 is free")
    """
    try:
        proc = subprocess.run(
            # -w hides harmless warnings, so anything on stderr is a real error
            ["lsof", "-w", "-t", f"-iTCP:{port}", "-sTCP:LISTEN", "-P", "-n"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    # lsof exits 1 both for "no match" and for errors; only errors print
    if proc.returncode == 1 and not proc.stdout.strip() and not proc.stderr.strip():
        return set()
    if proc.returncode != 0:
        return None
    return {int(pid) for pid in proc.stdout.split() if pid.isdigit()}


def _wait_for_release(port: int, pid: int, timeout: float) -> bool:
    """Poll until ``pid`` no longer listens on ``port`` or ``timeout`` passes.

    Parameters
    ----------
    port : int
        The TCP port to watch.
    pid : int
        The process expected to let go of the port.
    timeout : float
        Seconds to wait.

    Returns
    -------
    bool
        True if the process let go of the port in time.

    Examples
    --------
    .. code-block:: python

        _wait_for_release(3000, 41000, timeout=3.0)
    """
    deadline = time.monotonic() + timeout
    while True:
        listeners = _port_listener_pids(port)
        if listeners is not None and pid not in listeners:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_INTERVAL)


def _port_free_result(info: PortInfo, before: set[int] | None = None) -> KillResult:
    """Build the final result from one last look at the port.

    Parameters
    ----------
    info : PortInfo
        The port that was targeted.
    before : set[int] | None
        PIDs that listened on the port before the kill, if known.

    Returns
    -------
    KillResult
        Success only when nothing listens on the port.

    Examples
    --------
    .. code-block:: python

        result = _port_free_result(info)
    """
    listeners = _port_listener_pids(info.port)

    def fail(detail: str) -> KillResult:
        return KillResult(info.port, info.pid, False, detail)

    if listeners is None:
        return fail(f"could not check whether :{info.port} is free (lsof failed)")
    if not listeners:
        return KillResult(info.port, info.pid, True, "port is free")
    if info.pid in listeners:
        return fail(f"PID {info.pid} still listens on :{info.port}")
    others = ", ".join(str(p) for p in sorted(listeners))
    if before and listeners <= before:
        # Prefork servers share one socket across several PIDs
        return fail(
            f"PID {others} also listened on :{info.port} and is still running. "
            f"Run ports kill {info.port} again to stop it"
        )
    # The old process is gone but something restarted the server
    return fail(
        f"PID {info.pid} stopped, but PID {others} took :{info.port} again. "
        "A parent watcher (nodemon, npm run dev) probably restarted it"
    )


def _local_docker_env() -> dict[str, str] | None:
    """Return an environment pinned to the Docker endpoint, if it is local.

    The endpoint comes from ``DOCKER_HOST`` or else the active docker context.
    Only a ``unix://`` socket is local. A remote daemon's containers can
    publish the same port numbers as local listeners, so stopping one of them
    would hit the wrong machine.

    Returns
    -------
    dict[str, str] | None
        A copy of the environment with ``DOCKER_HOST`` set to the local socket
        and ``DOCKER_CONTEXT`` removed, or None when the endpoint is not local
        or cannot be found.

    Examples
    --------
    .. code-block:: python

        env = _local_docker_env()
        if env:
            subprocess.run(["docker", "ps"], env=env)
    """
    host = os.environ.get("DOCKER_HOST")
    if not host:
        try:
            proc = subprocess.run(
                [
                    "docker",
                    "context",
                    "inspect",
                    "--format",
                    "{{.Endpoints.docker.Host}}",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None
        if proc.returncode != 0:
            return None
        host = proc.stdout.strip()
    if not host.startswith("unix://"):
        return None
    env = {k: v for k, v in os.environ.items() if k != "DOCKER_CONTEXT"}
    env["DOCKER_HOST"] = host
    return env


def _socket_holders(path: str) -> dict[int, str] | None:
    """Return the processes that hold a Unix socket file open.

    Parameters
    ----------
    path : str
        Path of the socket file.

    Returns
    -------
    dict[int, str] | None
        Mapping of PID → process name, or None when ``lsof`` could not run.

    Examples
    --------
    .. code-block:: python

        _socket_holders("/Users/me/.docker/run/docker.sock")
        # {1500: "com.docker.backend"}
    """
    try:
        proc = subprocess.run(
            # -F pc prints "p<pid>" and "c<name>" lines; +c 0 keeps full names
            ["lsof", "-w", "+c", "0", "-F", "pc", "--", os.path.realpath(path)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    holders: dict[int, str] = {}
    pid: int | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("c") and pid is not None:
            holders[pid] = _decode_lsof_name(line[1:])
    return holders


# Engines that run at most once per user, so their process name identifies them
_SINGLE_INSTANCE_ENGINES = {"Docker", "OrbStack"}


def _endpoint_serves_listener(info: PortInfo, host: str) -> bool:
    """Return True when the Docker endpoint belongs to the engine on the port.

    Two engines can run at once, e.g. Docker Desktop and Colima, and both can
    publish the same port number. ``docker stop`` must only go to the engine
    whose process actually listens on the macOS port. The link holds when:

    - the listening process holds the endpoint's socket open, or
    - the engine runs once per user (Docker Desktop, OrbStack) and a process
      with the listener's exact name holds the socket. Matching the exact
      name keeps docker CLI clients (``com.docker.cli``) from counting.

    Lima (and so Colima and Rancher Desktop) can run several instances, so it
    needs the PID match.

    Parameters
    ----------
    info : PortInfo
        The container port about to be freed.
    host : str
        The endpoint, a ``unix://`` URL.

    Returns
    -------
    bool
        False when the link cannot be shown.

    Examples
    --------
    .. code-block:: python

        _endpoint_serves_listener(info, "unix:///Users/me/.docker/run/docker.sock")
    """
    holders = _socket_holders(host.removeprefix("unix://"))
    if not holders:
        return False
    if info.pid in holders:
        return True
    engine = container_engine(info.raw_name, info.command)
    return engine in _SINGLE_INSTANCE_ENGINES and info.raw_name in holders.values()


def _stop_container(info: PortInfo, force: bool) -> KillResult:
    """Stop the Docker container behind a port, then check that the port is free.

    Parameters
    ----------
    info : PortInfo
        A port whose ``container`` and ``container_id`` are set.
    force : bool
        Use ``docker kill`` instead of ``docker stop``.

    Returns
    -------
    KillResult
        The outcome for the port.

    Examples
    --------
    .. code-block:: python

        _stop_container(info, force=False)  # runs "docker stop <id>"
    """
    action = "kill" if force else "stop"
    name = info.container or ""

    # Re-check right before stopping: the port may be free already, or a
    # different container may have taken it since the user confirmed
    before = _port_listener_pids(info.port)
    if before is None or info.pid not in before:
        return _port_free_result(info, before)
    env = _local_docker_env()
    if env is None:
        return KillResult(
            info.port,
            info.pid,
            False,
            "Docker is not using a local socket (check DOCKER_HOST and "
            "docker context), so the container behind this port cannot be "
            "confirmed. Nothing was stopped",
        )
    engine = container_engine(info.raw_name, info.command) or "the container engine"
    if not _endpoint_serves_listener(info, env["DOCKER_HOST"]):
        return KillResult(
            info.port,
            info.pid,
            False,
            f":{info.port} is held by {engine}, but the docker CLI talks to "
            f"a different engine ({env['DOCKER_HOST']}). Nothing was stopped; "
            "switch with docker context use",
        )
    # Compare full IDs: a recreated container can reuse the old name
    current = _batch_docker_info(env).get(info.port)
    if current is None or current["id"] != info.container_id:
        now = f"now {current['name']}" if current else "now unknown"
        return KillResult(
            info.port,
            info.pid,
            False,
            f"the container on :{info.port} changed since it was listed "
            f"(was {name}, {now}). Nothing was stopped; run the command again",
        )
    container_id = current["id"]

    try:
        # docker stop waits up to 10 s for the container before it kills it
        proc = subprocess.run(
            ["docker", action, container_id],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
    except FileNotFoundError:
        return KillResult(info.port, info.pid, False, "the docker CLI is not found")
    except subprocess.TimeoutExpired:
        return KillResult(
            info.port, info.pid, False, f"docker {action} {name} timed out"
        )
    if proc.returncode != 0:
        # Drop control characters so docker's output cannot drive the terminal
        error = re.sub(r"[\x00-\x1f\x7f]", "", proc.stderr.strip())
        error = error or f"exit code {proc.returncode}"
        return KillResult(
            info.port, info.pid, False, f"docker {action} {name} failed: {error}"
        )

    _wait_for_release(info.port, info.pid, _KILL_TIMEOUT)
    return _port_free_result(info)


def free_port(info: PortInfo, force: bool = False) -> KillResult:
    """Stop whatever listens on a port and report success only once it is free.

    - A container port runs ``docker stop`` (or ``docker kill`` with
      ``force``), but only after the docker endpoint is shown to belong to
      the engine that listens on the port. The processes of a container
      engine (Docker Desktop, OrbStack, Colima, ...) never receive a signal,
      because that would drop every port they forward.
    - A zombie has already exited, so it gets no signal. Only its parent can
      remove it.
    - Any other process gets SIGTERM, then SIGKILL if it still holds the port
      after ``_TERM_TIMEOUT`` seconds. ``force`` sends SIGKILL at once.

    Parameters
    ----------
    info : PortInfo
        The port to free, from :func:`get_listening_ports`.
    force : bool
        Skip the graceful step.

    Returns
    -------
    KillResult
        ``ok`` is True only if nothing listens on the port afterwards.

    Examples
    --------
    .. code-block:: python

        result = free_port(get_port_details(3000))
        print(result.ok, result.detail)
    """
    if info.status == "zombie":
        parent = f"PID {info.ppid}" if info.ppid else "its parent"
        return KillResult(
            info.port,
            info.pid,
            False,
            f"PID {info.pid} is a zombie: it has already exited and no signal "
            f"can remove it. Only its parent ({parent}) can reap it",
        )
    if info.container:
        return _stop_container(info, force)
    command = info.command
    if not command and info.raw_name in _NEEDS_COMMAND:
        # The batched ps call failed for this PID. Without the command line a
        # Lima ssh forwarder looks like any ssh tunnel, so ask again.
        command = _batch_ps_info([info.pid]).get(info.pid, {}).get("command", "")
        if not command:
            return KillResult(
                info.port,
                info.pid,
                False,
                f"could not read the command line of PID {info.pid} "
                f"({info.raw_name}), so it may be a container engine's port "
                "forwarder. Nothing was signalled",
            )
    engine = container_engine(info.raw_name, command)
    if engine:
        return KillResult(
            info.port,
            info.pid,
            False,
            f"PID {info.pid} is {engine}'s own process; signalling it would drop "
            "every port it forwards. No container was found for this port (is "
            "the docker CLI installed and pointed at this engine?)",
        )

    # Signal only a PID that still holds the port. After a long prompt the
    # server may be gone and its PID reused by an unrelated process.
    before = _port_listener_pids(info.port)
    if before is None or info.pid not in before:
        return _port_free_result(info, before)

    steps = [signal.SIGKILL] if force else [signal.SIGTERM, signal.SIGKILL]
    for i, sig in enumerate(steps):
        if i > 0:
            # Escalate only on fresh proof that the process still holds the
            # port. A failed check is not proof, and SIGKILL could cut short a
            # clean shutdown.
            current = _port_listener_pids(info.port)
            if current is None or info.pid not in current:
                break
        try:
            os.kill(info.pid, sig)
        except ProcessLookupError:
            break  # already gone; the final check decides
        except PermissionError:
            return KillResult(
                info.port,
                info.pid,
                False,
                f"permission denied. Try: sudo kill {info.pid}",
            )
        timeout = _TERM_TIMEOUT if sig == signal.SIGTERM else _KILL_TIMEOUT
        if _wait_for_release(info.port, info.pid, timeout):
            break
    return _port_free_result(info, before)


def watch_ports(
    callback: Callable[[str, PortInfo | list[PortInfo]], None],
    interval: float = 2.0,
    dev_only: bool = True,
) -> None:
    """Poll listening ports and invoke callback on changes.

    Each port is tracked with its PID, so a server that restarts on the same
    port between two polls is reported. Runs until ``KeyboardInterrupt``.

    Parameters
    ----------
    callback : Callable[[str, PortInfo | list[PortInfo]], None]
        Called once with ``("snapshot", list[PortInfo])`` for the ports open
        at the start, then with ``("new", PortInfo)``,
        ``("restarted", PortInfo)`` (same port, new PID) or
        ``("closed", PortInfo)`` (the last info seen for that port). A failed
        scan after the first one is skipped; the first one raises
        :class:`ScanError`.
    interval : float
        Polling interval in seconds (default: 2.0).
    dev_only : bool
        Track only dev processes, as the default ``ports`` view does.

    Examples
    --------
    .. code-block:: python

        def on_change(event, data):
            print(event, data)

        watch_ports(on_change, dev_only=False)
    """

    def poll() -> dict[int, PortInfo]:
        ports = get_listening_ports()
        if dev_only:
            ports = [p for p in ports if is_dev_process(p.process_name, p.command)]
        return {p.port: p for p in ports}

    previous = poll()
    callback("snapshot", list(previous.values()))
    try:
        while True:
            time.sleep(interval)
            try:
                current = poll()
            except ScanError:
                # A failed scan says nothing about the ports; try again on
                # the next tick instead of reporting every port as closed
                continue
            for port, info in current.items():
                if port not in previous:
                    callback("new", info)
                elif previous[port].pid != info.pid:
                    callback("restarted", info)
            for port, info in previous.items():
                if port not in current:
                    callback("closed", info)
            previous = current
    except KeyboardInterrupt:
        pass


def find_project_root(directory: str) -> str:
    """Return the project that ``directory`` belongs to, for ``ports here``.

    The git work tree wins, so a monorepo counts as one project. Outside git,
    the nearest directory with a project marker (``package.json``,
    ``pyproject.toml``, ...) is used.

    Parameters
    ----------
    directory : str
        Starting directory, usually the current one.

    Returns
    -------
    str
        Absolute path with symlinks resolved, to match the paths lsof reports.

    Examples
    --------
    .. code-block:: python

        find_project_root(os.getcwd())  # "/Users/me/projects/webapp"
    """
    directory = os.path.realpath(directory)
    try:
        proc = subprocess.run(
            ["git", "-C", directory, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        top = proc.stdout.strip()
        if proc.returncode == 0 and top:
            return os.path.realpath(top)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return _find_project_root(directory)
