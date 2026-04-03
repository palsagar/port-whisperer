from __future__ import annotations

import re

import click
from rich.console import Console

from portwhisperer.display import (
    display_clean_results,
    display_port_detail,
    display_port_table,
    display_process_table,
    display_watch_event,
    display_watch_header,
)
from portwhisperer.scanner import (
    ProcessInfo,
    find_orphaned_processes,
    get_all_processes,
    get_listening_ports,
    get_port_details,
    is_dev_process,
    kill_process,
    watch_ports,
)

console = Console()


def _is_docker_process(name: str) -> bool:
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

        _is_docker_process("com.docker.backend")  # True
        _is_docker_process("node")                # False
    """
    return (
        name.startswith("com.docke")
        or name.startswith("Docker")
        or name in ("docker", "docker-sandbox")
    )


def _parse_memory_to_kb(memory: str | None) -> float:
    """Parse a formatted memory string back to kilobytes.

    Supports "X.Y GB", "X.Y MB", and "X KB" formats produced by
    :func:`~portwhisperer.scanner._format_memory`.

    Parameters
    ----------
    memory : str | None
        Memory string like "735.5 MB" or "1.2 GB".

    Returns
    -------
    float
        Equivalent value in kilobytes, or 0.0 if unparseable.

    Examples
    --------
    .. code-block:: python

        _parse_memory_to_kb("1.2 GB")   # 1258291.2
        _parse_memory_to_kb("512.0 MB") # 524288.0
        _parse_memory_to_kb(None)       # 0.0
    """
    if not memory:
        return 0.0
    m = re.match(r"([\d.]+)\s*(GB|MB|KB)", memory)
    if not m:
        return 0.0
    val = float(m.group(1))
    unit = m.group(2)
    if unit == "GB":
        return val * 1_048_576
    if unit == "MB":
        return val * 1024
    return val  # KB


def _format_memory_from_kb(total_kb: float) -> str:
    """Format a KB value to a human-readable memory string.

    Parameters
    ----------
    total_kb : float
        Memory in kilobytes.

    Returns
    -------
    str
        Formatted string like "1.2 GB", "512.0 MB", or "128 KB".

    Examples
    --------
    .. code-block:: python

        _format_memory_from_kb(1_048_576)  # "1.0 GB"
        _format_memory_from_kb(2048)       # "2.0 MB"
    """
    if total_kb > 1_048_576:
        return f"{total_kb / 1_048_576:.1f} GB"
    if total_kb > 1024:
        return f"{total_kb / 1024:.1f} MB"
    return f"{round(total_kb)} KB"


def _collapse_docker(processes: list[ProcessInfo]) -> list[ProcessInfo]:
    """Collapse all Docker processes into a single summary ProcessInfo row.

    Separates Docker processes from the rest, sums their CPU and memory,
    and appends one summary entry showing the total count.

    Parameters
    ----------
    processes : list[ProcessInfo]
        Full list of dev processes (already filtered).

    Returns
    -------
    list[ProcessInfo]
        Non-Docker processes plus one Docker summary row (if any Docker
        processes were found).

    Examples
    --------
    .. code-block:: python

        collapsed = _collapse_docker(dev_processes)
    """
    docker_procs = [p for p in processes if _is_docker_process(p.process_name)]
    non_docker = [p for p in processes if not _is_docker_process(p.process_name)]

    if not docker_procs:
        return non_docker

    total_cpu = sum(p.cpu for p in docker_procs)
    total_kb = sum(_parse_memory_to_kb(p.memory) for p in docker_procs)

    summary = ProcessInfo(
        pid=docker_procs[0].pid,
        process_name="Docker",
        command="",
        description=f"{len(docker_procs)} processes",
        cpu=total_cpu,
        memory=_format_memory_from_kb(total_kb),
        framework="Docker",
        uptime=docker_procs[0].uptime,
    )
    return [*non_docker, summary]


@click.group(invoke_without_command=True)
@click.argument("port", required=False, type=int)
@click.option(
    "--all",
    "-a",
    "show_all",
    is_flag=True,
    help="Show all listening ports, not just dev servers",
)
@click.option("--whoisonport", type=int, help="Show details for a specific port")
@click.pass_context
def main(
    ctx: click.Context, port: int | None, show_all: bool, whoisonport: int | None
) -> None:
    """Port Whisperer — listen to your ports."""
    # If a subcommand was invoked, let it handle things
    if ctx.invoked_subcommand is not None:
        return

    # --whoisonport flag is an alias for passing a port number directly
    target_port = whoisonport or port

    if target_port is not None:
        info = get_port_details(target_port)
        display_port_detail(info)
        if info and click.confirm(f"  Kill process on :{target_port}?", default=False):
            success = kill_process(info.pid)
            if success:
                console.print(f"\n  [green]✓ Killed PID {info.pid}[/]\n")
            else:
                console.print(f"\n  [red]✕ Failed. Try: sudo kill -9 {info.pid}[/]\n")
        return

    # Default: show port table, filtered to dev servers unless --all
    ports = get_listening_ports()
    if not show_all:
        ports = [p for p in ports if is_dev_process(p.process_name, p.command)]
    display_port_table(ports, filtered=not show_all)


@main.command()
@click.option(
    "--all",
    "-a",
    "show_all",
    is_flag=True,
    help="Show all processes",
)
def ps(show_all: bool) -> None:
    """Show all running dev processes."""
    processes = get_all_processes()

    if not show_all:
        processes = [p for p in processes if is_dev_process(p.process_name, p.command)]
        # Merge all Docker-family processes into one summary row
        processes = _collapse_docker(processes)

    # Hottest processes first
    processes.sort(key=lambda p: p.cpu, reverse=True)
    display_process_table(processes, filtered=not show_all)


@main.command()
def clean() -> None:
    """Kill orphaned and zombie dev server processes."""
    orphaned = find_orphaned_processes()

    if not orphaned:
        display_clean_results(orphaned, killed=[], failed=[])
        return

    # List what will be killed before asking for confirmation
    console.print()
    count = len(orphaned)
    noun = "process" if count == 1 else "processes"
    console.print(f"[bold yellow]  Found {count} orphaned/zombie {noun}:[/]\n")
    for p in orphaned:
        console.print(
            f"  [dim]•[/] :[bold white]{p.port}[/] [dim]—[/] "
            f"{p.process_name} [dim](PID {p.pid})[/]"
        )
    console.print()

    killed: list[int] = []
    failed: list[int] = []

    if click.confirm("  Kill all?", default=False):
        for p in orphaned:
            if kill_process(p.pid):
                killed.append(p.pid)
            else:
                failed.append(p.pid)
        display_clean_results(orphaned, killed=killed, failed=failed)
    else:
        console.print("[dim]\n  Aborted.\n[/]")


@main.command()
@click.option(
    "--interval",
    default=2.0,
    type=float,
    help="Poll interval in seconds",
)
def watch(interval: float) -> None:
    """Monitor port changes in real-time."""
    display_watch_header()
    watch_ports(display_watch_event, interval=interval)
