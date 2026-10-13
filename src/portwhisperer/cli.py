from __future__ import annotations

import os
import re
import sys

import click
from rich.console import Console

from portwhisperer.display import (
    display_clean_candidates,
    display_json,
    display_kill_results,
    display_kill_targets,
    display_port_detail,
    display_port_table,
    display_process_table,
    display_watch_event,
    display_watch_header,
    display_watch_json,
    display_watch_snapshot,
    set_banner,
)
from portwhisperer.scanner import (
    KillResult,
    PortInfo,
    ProcessInfo,
    ScanError,
    find_clean_candidates,
    find_project_root,
    free_port,
    get_all_processes,
    get_listening_ports,
    get_port_details,
    is_dev_process,
    is_docker_process,
    watch_ports,
)

console = Console()


class _PortGroup(click.Group):
    """Custom group that allows `ports 3000` alongside subcommands.

    Rewrites a leading integer argument to ``--whoisonport <n>`` so Click
    doesn't confuse it with a subcommand name.
    """

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        # Find the first argument that is not an option, so "ports --json 3000"
        # works as well as "ports 3000 --json". --whoisonport is the only
        # group option that takes a value.
        i = 0
        while i < len(args) and args[i].startswith("-"):
            i += 2 if args[i] == "--whoisonport" else 1
        if i < len(args) and args[i] not in self.commands:
            try:
                port = int(args[i])
            except ValueError:
                pass
            else:
                # Name the bad port in the user's own words, not --whoisonport's
                if not 1 <= port <= 65535:
                    raise click.UsageError(
                        f"{port} is not a valid port. Use a number from 1 to 65535.",
                        ctx,
                    )
                args = [*args[:i], "--whoisonport", args[i], *args[i + 1 :]]
        return super().parse_args(ctx, args)

    def invoke(self, ctx: click.Context) -> object:
        # A failed lsof scan must not pass for "nothing listens": exit 3
        try:
            return super().invoke(ctx)
        except ScanError as exc:
            click.echo(f"Error: {exc}. Could not check the ports.", err=True)
            ctx.exit(3)


def _stdin_is_tty() -> bool:
    """Return True when a person can answer prompts on stdin.

    Examples
    --------
    .. code-block:: python

        if _stdin_is_tty():
            click.confirm("Kill?")
    """
    return sys.stdin.isatty()


def _stdout_is_tty() -> bool:
    """Return True when output goes to a terminal, not a pipe or a file.

    Examples
    --------
    .. code-block:: python

        set_banner(_stdout_is_tty())
    """
    return sys.stdout.isatty()


def _hide_banner(ctx: click.Context, param: click.Parameter, quiet: bool) -> None:
    """Click callback for a subcommand's ``-q``: turn the banner off."""
    if quiet:
        set_banner(False)


# -q on a subcommand runs after the group has set the banner, so it wins
quiet_option = click.option(
    "-q",
    "--quiet",
    is_flag=True,
    expose_value=False,
    callback=_hide_banner,
    help="Do not print the banner.",
)
json_option = click.option(
    "--json", "as_json", is_flag=True, help="Print JSON instead of a table."
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
    docker_procs = [p for p in processes if is_docker_process(p.process_name)]
    non_docker = [p for p in processes if not is_docker_process(p.process_name)]

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


@click.group(cls=_PortGroup, invoke_without_command=True)
@click.version_option(package_name="portwhisperer", prog_name="portwhisperer")
@click.option(
    "--all",
    "-a",
    "show_all",
    is_flag=True,
    help="Show all listening ports, not just dev servers",
)
@click.option(
    "--whoisonport",
    type=click.IntRange(1, 65535),
    metavar="PORT",
    help="Show details for a specific port",
)
@json_option
@click.option("-q", "--quiet", is_flag=True, help="Do not print the banner.")
@click.pass_context
def main(
    ctx: click.Context,
    show_all: bool,
    whoisonport: int | None,
    as_json: bool,
    quiet: bool,
) -> None:
    """Port Whisperer — listen to your ports.

    \b
    ports <port> exits 0 when a process listens on the port, 1 when the port
    is free, 2 when the argument is not a valid port, and 3 when the ports
    could not be checked.
    """
    # The banner is decoration for people; pipes and files get plain data
    set_banner(not quiet and _stdout_is_tty())
    if ctx.invoked_subcommand is not None:
        return

    if whoisonport is not None:
        info = get_port_details(whoisonport)
        if as_json:
            if info:
                display_json(info)
            sys.exit(0 if info else 1)
        display_port_detail(info)
        if info is None:
            sys.exit(1)
        # Without a terminal nobody can answer, so never prompt (or hang). The
        # prompt goes to stdout, so a redirected stdout would hide it.
        if (
            _stdin_is_tty()
            and _stdout_is_tty()
            and click.confirm(f"  Kill process on :{whoisonport}?", default=False)
        ):
            display_kill_results([free_port(info)])
        return

    # Default: show port table, filtered to dev servers unless --all
    ports = get_listening_ports()
    if not show_all:
        ports = [p for p in ports if is_dev_process(p.process_name, p.command)]
    if as_json:
        display_json(ports)
        return
    display_port_table(ports, filtered=not show_all)


@main.command()
@click.option(
    "--all",
    "-a",
    "show_all",
    is_flag=True,
    help="Show all processes",
)
@json_option
@quiet_option
def ps(show_all: bool, as_json: bool) -> None:
    """Show all running dev processes."""
    processes = get_all_processes()

    if not show_all:
        processes = [p for p in processes if is_dev_process(p.process_name, p.command)]
        # Merge all Docker-family processes into one summary row. JSON keeps
        # them apart: the summary row's PID and command are not real.
        if not as_json:
            processes = _collapse_docker(processes)

    # Hottest processes first
    processes.sort(key=lambda p: p.cpu, reverse=True)
    if as_json:
        display_json(processes)
        return
    display_process_table(processes, filtered=not show_all)


def _parse_selection(raw: str, count: int) -> list[int]:
    """Turn the answer to "Kill which?" into zero-based candidate indexes.

    Parameters
    ----------
    raw : str
        The answer: ``all``, ``none`` or empty, or numbers separated by commas
        or spaces.
    count : int
        Number of candidates on offer.

    Returns
    -------
    list[int]
        Picked indexes in the order given, without duplicates.

    Raises
    ------
    click.BadParameter
        When a number is out of range or the answer cannot be read. Click then
        asks again.

    Examples
    --------
    .. code-block:: python

        _parse_selection("1,3", 3)  # [0, 2]
        _parse_selection("all", 2)  # [0, 1]
    """
    answer = raw.strip().lower()
    if answer in ("", "none"):
        return []
    if answer == "all":
        return list(range(count))
    picked: list[int] = []
    for token in answer.replace(",", " ").split():
        if not token.isdigit() or not 1 <= int(token) <= count:
            raise click.BadParameter(
                f"{token!r} is not a number from 1 to {count}, 'all' or 'none'"
            )
        if int(token) - 1 not in picked:
            picked.append(int(token) - 1)
    if not picked:
        raise click.BadParameter("give numbers like 1,3, or 'all' or 'none'")
    return picked


@main.command()
@quiet_option
def clean() -> None:
    """Pick detached or zombie dev servers to kill. Daemons are left alone."""
    candidates = find_clean_candidates()
    display_clean_candidates(candidates)
    if not candidates:
        return

    if not _stdin_is_tty():
        console.print(
            "[dim]  No terminal, so nothing was killed. "
            "Use [/][cyan]ports kill <port> --yes[/][dim].[/]\n"
        )
        return

    picked = click.prompt(
        "  Kill which? (e.g. 1,3 / all / none)",
        default="none",
        show_default=False,
        value_proc=lambda raw: _parse_selection(raw, len(candidates)),
    )
    if not picked:
        console.print("[dim]\n  Nothing killed.\n[/]")
        return
    display_kill_results([free_port(candidates[i]) for i in picked])


@main.command()
@click.argument("port_numbers", metavar="PORT...", nargs=-1, required=True, type=int)
@click.option("--force", is_flag=True, help="Send SIGKILL (or docker kill) right away")
@click.option("--yes", "-y", is_flag=True, help="Do not ask for confirmation")
def kill(port_numbers: tuple[int, ...], force: bool, yes: bool) -> None:
    """Free the given ports and exit 0 only if all of them end up free."""
    by_port = {p.port: p for p in get_listening_ports()}
    requested = list(dict.fromkeys(port_numbers))
    targets = [by_port[n] for n in requested if n in by_port]

    if targets and not yes:
        if not _stdin_is_tty():
            console.print("[red]  stdin is not a terminal. Pass --yes to kill.[/]")
            sys.exit(1)
        display_kill_targets(targets)
        noun = "this" if len(targets) == 1 else f"these {len(targets)}"
        if not click.confirm(f"  Kill {noun}?", default=False):
            console.print("[dim]\n  Aborted.\n[/]")
            sys.exit(1)

    results = {t.port: free_port(t, force=force) for t in targets}
    ordered = [
        results.get(n) or KillResult(n, None, False, f"nothing listens on :{n}")
        for n in requested
    ]
    display_kill_results(ordered)
    if not all(r.ok for r in ordered):
        sys.exit(1)


@main.command()
@click.option(
    "--interval",
    default=2.0,
    type=click.FloatRange(min=0.2),
    show_default=True,
    help="Poll interval in seconds (at least 0.2)",
)
@click.option(
    "--all",
    "-a",
    "show_all",
    is_flag=True,
    help="Watch all listening ports, not just dev servers",
)
@click.option(
    "--json", "as_json", is_flag=True, help="Print one JSON object per event."
)
@quiet_option
def watch(interval: float, show_all: bool, as_json: bool) -> None:
    """Monitor port changes in real-time.

    Prints the ports open now, then one line each time a port opens, closes,
    or is taken over by a new PID (a restart).
    """
    if as_json:
        watch_ports(display_watch_json, interval=interval, dev_only=not show_all)
        return

    def on_event(event: str, data: PortInfo | list[PortInfo]) -> None:
        if isinstance(data, PortInfo):
            display_watch_event(event, data)
        else:
            display_watch_snapshot(data)

    display_watch_header()
    watch_ports(on_event, interval=interval, dev_only=not show_all)


@main.command("open")
@click.argument("port", type=click.IntRange(1, 65535))
def open_port(port: int) -> None:
    """Open http://localhost:PORT in the default browser.

    Exits 1 when nothing listens on the port.
    """
    if port not in {p.port for p in get_listening_ports()}:
        console.print(f"[red]  Nothing listens on :{port}.[/]")
        sys.exit(1)
    click.launch(f"http://localhost:{port}")


@main.command("next")
@click.argument("start", type=click.IntRange(1, 65535), default=3000)
def next_port(start: int) -> None:
    """Print the first free TCP port at or above START (default 3000).

    Only listeners that lsof can see count, which without sudo means your
    own processes.
    """
    busy = {p.port for p in get_listening_ports()}
    free = next((n for n in range(start, 65536) if n not in busy), None)
    if free is None:
        console.print(f"[red]  No free port from {start} to 65535.[/]")
        sys.exit(1)
    click.echo(free)


@main.command()
@quiet_option
def here() -> None:
    """Show the ports served from the current project.

    The project is the git repository you are in, or else the nearest
    directory with a project file such as package.json or pyproject.toml.
    """
    root = find_project_root(os.getcwd())
    ports = [
        p
        for p in get_listening_ports()
        if p.cwd and (p.cwd == root or p.cwd.startswith(root.rstrip("/") + "/"))
    ]
    display_port_table(ports, project=root)
