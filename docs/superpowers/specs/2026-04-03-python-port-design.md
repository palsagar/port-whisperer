# Port Whisperer: JavaScript to Python Port

**Date:** 2026-04-03
**Status:** Draft

## Overview

Full rewrite of port-whisperer from Node.js (ES modules, chalk, cli-table3) to Python 3.12+ (click, rich). The package is renamed to `portwhisperer` for PyPI distribution. All JavaScript source files, `package.json`, and `package-lock.json` are deleted after the port. The tool's functionality is preserved 1:1 with minor CLI improvements.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Package name | `portwhisperer` | Clean, no hyphen |
| CLI entry point | `ports` only | `whoisonport` becomes a `--whoisonport` flag on the main command |
| Arg parsing | click | Decorator-based, clean subcommands, pairs well with rich |
| TUI output | rich | Tables, panels, trees, styled text — replaces chalk + cli-table3 |
| Build backend | hatchling | Lightweight, modern, works well with uv |
| Linting/formatting | ruff | Fast, handles both linting and formatting |
| Type checking | ty | Modern, fast |
| Python version | >=3.12 | User requirement |
| Project layout | `src/` layout | Hatchling default, avoids accidental imports |

## Project Structure

```
portwhisperer/
├── pyproject.toml
├── README.md
├── LICENSE
├── .gitignore
├── src/
│   └── portwhisperer/
│       ├── __init__.py     # __version__ string
│       ├── cli.py          # click group + subcommands
│       ├── scanner.py      # subprocess calls, framework detection, data classes
│       └── display.py      # rich tables, panels, styled output
```

## Dependencies

### Runtime
- `click` — CLI framework
- `rich` — terminal output (tables, panels, trees, styled text)

### Dev (dependency group)
- `ruff` — linting + formatting
- `ty` — type checking

## CLI Design

```
ports                          # dev ports table (filtered, default)
ports --all / -a               # all listening ports
ports 3000                     # detailed view for port 3000
ports --whoisonport 3000       # same as `ports 3000`
ports ps                       # dev process table
ports ps --all / -a            # all processes
ports clean                    # find and kill orphaned/zombie processes
ports watch                    # real-time port monitoring (default 2s interval)
ports watch --interval 5       # custom poll interval in seconds
```

### Default Command Pattern

The click group uses `invoke_without_command=True` with an optional `port` argument. When a port number is provided and no subcommand matches, it shows the detail view. The `--whoisonport` option on the group does the same thing.

### Interactive Prompts

The detail view and `clean` command use `click.confirm()` for kill prompts.

## Scanner Module (`scanner.py`)

### Data Classes

```python
@dataclass
class PortInfo:
    port: int
    pid: int
    process_name: str
    raw_name: str
    command: str
    cwd: str | None
    project_name: str | None
    framework: str | None
    uptime: str | None
    start_time: datetime | None
    status: str  # "healthy", "orphaned", "zombie"
    memory: str | None
    git_branch: str | None
    process_tree: list[dict]

@dataclass
class ProcessInfo:
    pid: int
    process_name: str
    command: str
    description: str
    cpu: float
    memory: str | None
    cwd: str | None
    project_name: str | None
    framework: str | None
    uptime: str | None
```

### Public Functions

- `get_listening_ports(detailed: bool = False) -> list[PortInfo]` — Parses `lsof -iTCP -sTCP:LISTEN -P -n`. Batch-fetches ps info and cwd for all PIDs. Returns sorted by port number. When `detailed=True`, also fetches git branch and process tree.
- `get_all_processes() -> list[ProcessInfo]` — Parses `ps -eo pid=,pcpu=,...`. Batch cwd lookup for non-Docker processes. Used by `ports ps`.
- `get_port_details(port: int) -> PortInfo | None` — Calls `get_listening_ports(detailed=True)`, filters to target port.
- `find_orphaned_processes() -> list[PortInfo]` — Filters listening ports by orphaned/zombie status.
- `kill_process(pid: int, signal: int = signal.SIGTERM) -> bool` — Uses `os.kill()`.
- `watch_ports(callback: Callable[[str, PortInfo | dict], None], interval: float = 2.0)` — Polling loop that calls `callback("new", PortInfo)` or `callback("removed", {"port": int})`. Runs until KeyboardInterrupt.
- `is_dev_process(name: str, command: str) -> bool` — Blocklist/allowlist filtering of system apps vs dev processes.

### Private Functions

- `_batch_ps_info(pids: list[int]) -> dict[int, dict]` — Single `ps -p <pids>` call.
- `_batch_cwd(pids: list[int]) -> dict[int, str]` — Single `lsof -a -d cwd -p <pids>` call.
- `_batch_docker_info() -> dict[int, dict]` — Single `docker ps --format` call.
- `_detect_framework(project_root: str) -> str | None` — Reads `package.json` deps, checks config files.
- `_detect_framework_from_command(command: str, process_name: str) -> str | None` — Regex on command string.
- `_detect_framework_from_image(image: str) -> str | None` — Docker image name matching.
- `_detect_framework_from_name(process_name: str) -> str | None` — Fallback by process name.
- `_find_project_root(directory: str) -> str` — Walks up looking for `package.json`, `Cargo.toml`, `go.mod`, `pyproject.toml`, `Gemfile`, `pom.xml`, `build.gradle`. Max 15 levels.
- `_format_uptime(seconds: float) -> str` — Human-readable duration from elapsed seconds.
- `_format_memory(rss_kb: int) -> str` — Human-readable memory (KB/MB/GB).
- `_summarize_command(command: str, process_name: str) -> str` — Short description from full command string.

### Subprocess Strategy

All subprocess calls use `subprocess.run()` with:
- `capture_output=True`
- `text=True`
- `timeout` (5s for ps/docker, 10s for lsof)
- Errors caught and returned as empty results (same resilience as JS version)

## Display Module (`display.py`)

### Module-Level Console

Single `rich.console.Console()` instance used by all display functions.

### Functions

- `render_header()` — Rich `Panel` with "Port Whisperer" banner, cyan border.
- `display_port_table(ports: list[PortInfo], filtered: bool)` — Rich `Table` with columns: PORT, PROCESS, PID, PROJECT, FRAMEWORK, UPTIME, STATUS. Footer with count and hints.
- `display_process_table(processes: list[ProcessInfo], filtered: bool)` — Rich `Table` for `ports ps`: PID, PROCESS, CPU%, MEM, PROJECT, FRAMEWORK, UPTIME, WHAT. CPU color by threshold.
- `display_port_detail(info: PortInfo | None)` — Key-value layout with Rich Panel. Process tree rendered with `rich.tree.Tree`.
- `display_clean_results(orphaned, killed, failed)` — Summary with status icons.
- `display_watch_event(event_type: str, info)` — Timestamped single-line output.
- `display_watch_header()` — Banner + instruction message.

### Styling

Framework color mapping preserved from JS version:
- Next.js → white, Vite → yellow, React → cyan, Vue → green, Angular → red, Express → gray, Django → green, Rails → red, Docker → blue, PostgreSQL → blue, Redis → red, etc.

Status icons: `[green]●[/green]` healthy, `[yellow]●[/yellow]` orphaned, `[red]●[/red]` zombie.

CPU thresholds: >25% red, >5% yellow, else green.

## Tooling Configuration (all in `pyproject.toml`)

### ruff
- `target-version = "py312"`
- `line-length = 88`
- `select = ["E", "F", "W", "I", "UP", "B", "SIM"]`

### ty
- Standard type checking, no special config.

### hatchling
- `[build-system]` with `hatchling`
- `[project.scripts]` mapping `ports = "portwhisperer.cli:main"`
- Classifiers for Python 3.12+, MIT license, CLI tool

### Dev dependencies
- `[dependency-groups]` with `dev = ["ruff", "ty"]`

## Files Deleted

- `src/index.js`
- `src/scanner.js`
- `src/display.js`
- `package.json`
- `package-lock.json`
- `node_modules/` (if present)
- `CLAUDE.md` (rewritten for Python project)

## Files Modified

- `README.md` — Overhauled for Python: pip/uv installation, usage walkthrough of every command with example output, platform support note.
- `.gitignore` — Replaced Node patterns with Python patterns (`__pycache__/`, `*.egg-info/`, `.venv/`, etc.)

## README Walkthrough Structure

The README will include:

1. **Project description** — what it does, one-liner
2. **What it looks like** — example output screenshot/block for `ports` command
3. **Install** — `pip install portwhisperer` and `uv tool install portwhisperer`
4. **Usage walkthrough** — each command explained with example invocation and expected output:
   - `ports` (default dev view)
   - `ports --all` (all ports)
   - `ports <number>` (inspect a port) and `ports --whoisonport <number>`
   - `ports ps` (dev processes) and `ports ps --all`
   - `ports clean` (cleanup orphans)
   - `ports watch` and `ports watch --interval 5`
5. **How it works** — the 3 shell calls, framework detection, batch strategy
6. **Platform support** — macOS supported, Linux planned, Windows not planned
7. **License** — MIT

## Platform Support

macOS only (unchanged). Uses `lsof` and `ps` with macOS-specific flags. Linux support is noted as planned but not implemented in this port.
