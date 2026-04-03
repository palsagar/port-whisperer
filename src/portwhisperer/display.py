from __future__ import annotations

from datetime import datetime
from typing import cast

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from portwhisperer.scanner import PortInfo, ProcessInfo

console = Console()

# Maps framework names to Rich style strings, mirroring chalk colors in display.js
FRAMEWORK_COLORS: dict[str, str] = {
    "Next.js": "white",
    "Vite": "yellow",
    "React": "cyan",
    "Vue": "green",
    "Angular": "red",
    "Svelte": "bold orange_red1",
    "SvelteKit": "bold orange_red1",
    "Express": "dim",
    "Fastify": "white",
    "NestJS": "red",
    "Nuxt": "green",
    "Remix": "blue",
    "Astro": "magenta",
    "Django": "green",
    "Flask": "white",
    "FastAPI": "cyan",
    "Rails": "red",
    "Gatsby": "magenta",
    "Go": "cyan",
    "Rust": "sandy_brown",
    "Ruby": "red",
    "Python": "yellow",
    "Node.js": "green",
    "Java": "red",
    "Hono": "dark_orange",
    "Koa": "white",
    "Webpack": "blue",
    "esbuild": "yellow",
    "Parcel": "dark_goldenrod",
    "Docker": "blue",
    "PostgreSQL": "blue",
    "Redis": "red",
    "MySQL": "blue",
    "MongoDB": "green",
    "nginx": "green",
    "LocalStack": "white",
    "RabbitMQ": "dark_orange",
    "Kafka": "white",
    "Elasticsearch": "yellow",
    "MinIO": "red",
}


def _truncate(text: str, max_len: int) -> str:
    """Truncate a string to max_len characters, appending ellipsis if needed.

    Parameters
    ----------
    text : str
        Input string to truncate.
    max_len : int
        Maximum allowed character length.

    Returns
    -------
    str
        Original string if short enough, otherwise truncated with "…".

    Examples
    --------
    .. code-block:: python

        _truncate("hello world", 8)  # "hello w…"
    """
    if not text:
        return ""
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def render_header() -> None:
    """Print the Port Whisperer banner as a Rich Panel.

    Examples
    --------
    .. code-block:: python

        render_header()
    """
    content = Text()
    content.append("🔊 Port Whisperer", style="bold white")
    content.append("\n")
    content.append("listening to your ports...", style="dim")
    console.print()
    console.print(Panel(content, border_style="cyan", expand=False))
    console.print()


def format_framework(framework: str | None) -> str:
    """Return a Rich-markup-styled framework name, or a dim dash if None.

    Parameters
    ----------
    framework : str | None
        Framework name to style.

    Returns
    -------
    str
        Rich markup string with color applied.

    Examples
    --------
    .. code-block:: python

        format_framework("Next.js")  # "[white]Next.js[/]"
        format_framework(None)       # "[dim]—[/]"
    """
    if not framework:
        return "[dim]—[/]"
    style = FRAMEWORK_COLORS.get(framework, "white")
    return f"[{style}]{framework}[/]"


def format_status(status: str) -> str:
    """Return a colored dot + label for the given status string.

    Parameters
    ----------
    status : str
        One of "healthy", "orphaned", "zombie", or any other value.

    Returns
    -------
    str
        Rich markup string with icon and label.

    Examples
    --------
    .. code-block:: python

        format_status("healthy")  # "[green]●[/] [green]healthy[/]"
    """
    _map = {
        "healthy": ("[green]●[/]", "[green]healthy[/]"),
        "orphaned": ("[yellow]●[/]", "[yellow]orphaned[/]"),
        "zombie": ("[red]●[/]", "[red]zombie[/]"),
    }
    icon, label = _map.get(status, ("[dim]●[/]", f"[dim]{status}[/]"))
    return f"{icon} {label}"


def display_port_table(ports: list[PortInfo], filtered: bool = False) -> None:
    """Render all listening ports in a Rich table and print a summary footer.

    Parameters
    ----------
    ports : list[PortInfo]
        Ports to display.
    filtered : bool
        When True, append a ``--all`` hint to the footer.

    Examples
    --------
    .. code-block:: python

        from portwhisperer.scanner import get_listening_ports
        display_port_table(get_listening_ports())
    """
    render_header()

    if not ports:
        console.print("[dim]  No active listening ports found.[/]\n")
        console.print(
            "[dim]  Start a dev server and run [/][cyan]ports[/][dim] again.[/]\n"
        )
        return

    table = Table(box=box.SIMPLE_HEAVY, show_footer=False, pad_edge=False)
    table.add_column("[bold cyan]PORT[/]")
    table.add_column("[bold cyan]PROCESS[/]")
    table.add_column("[bold cyan]PID[/]")
    table.add_column("[bold cyan]PROJECT[/]")
    table.add_column("[bold cyan]FRAMEWORK[/]")
    table.add_column("[bold cyan]UPTIME[/]")
    table.add_column("[bold cyan]STATUS[/]")

    for p in ports:
        project = (
            f"[blue]{_truncate(p.project_name, 20)}[/]"
            if p.project_name
            else "[dim]—[/]"
        )
        uptime = f"[yellow]{p.uptime}[/]" if p.uptime else "[dim]—[/]"
        process = p.process_name or p.raw_name or "—"

        table.add_row(
            f"[bold white]:{p.port}[/]",
            f"[white]{process}[/]",
            f"[dim]{p.pid}[/]",
            project,
            format_framework(p.framework),
            uptime,
            format_status(p.status),
        )

    console.print(table)
    console.print()

    # Build footer line with optional --all hint
    count = len(ports)
    noun = "port" if count == 1 else "ports"
    footer = Text()
    footer.append(f"  {count} {noun} active  ·  ", style="dim")
    footer.append("Run ", style="dim")
    footer.append("ports <number>", style="cyan")
    footer.append(" for details", style="dim")
    if filtered:
        footer.append("  ·  ", style="dim")
        footer.append("--all", style="cyan")
        footer.append(" to show everything", style="dim")
    console.print(footer)
    console.print()


def display_process_table(processes: list[ProcessInfo], filtered: bool = False) -> None:
    """Render all dev processes in a Rich table (used by ``ports ps``).

    CPU usage is color-coded: >25% red, >5% yellow, otherwise green.

    Parameters
    ----------
    processes : list[ProcessInfo]
        Processes to display.
    filtered : bool
        When True, append a ``--all`` hint to the footer.

    Examples
    --------
    .. code-block:: python

        from portwhisperer.scanner import get_all_processes
        display_process_table(get_all_processes())
    """
    render_header()

    if not processes:
        console.print("[dim]  No dev processes found.[/]\n")
        console.print(
            "[dim]  Run [/][cyan]ports ps --all[/][dim] to show all processes.[/]\n"
        )
        return

    table = Table(box=box.SIMPLE_HEAVY, show_footer=False, pad_edge=False)
    table.add_column("[bold cyan]PID[/]")
    table.add_column("[bold cyan]PROCESS[/]")
    table.add_column("[bold cyan]CPU%[/]")
    table.add_column("[bold cyan]MEM[/]")
    table.add_column("[bold cyan]PROJECT[/]")
    table.add_column("[bold cyan]FRAMEWORK[/]")
    table.add_column("[bold cyan]UPTIME[/]")
    table.add_column("[bold cyan]WHAT[/]")

    for p in processes:
        cpu_str = f"{p.cpu:.1f}"
        # Color-code CPU: red when hot, yellow when busy, green when idle
        if p.cpu > 25:
            cpu_cell = f"[red]{cpu_str}[/]"
        elif p.cpu > 5:
            cpu_cell = f"[yellow]{cpu_str}[/]"
        else:
            cpu_cell = f"[green]{cpu_str}[/]"

        memory = f"[green]{p.memory}[/]" if p.memory else "[dim]—[/]"
        project = (
            f"[blue]{_truncate(p.project_name, 20)}[/]"
            if p.project_name
            else "[dim]—[/]"
        )
        uptime = f"[yellow]{p.uptime}[/]" if p.uptime else "[dim]—[/]"
        what = f"[dim]{_truncate(p.description or p.process_name, 30)}[/]"

        table.add_row(
            f"[dim]{p.pid}[/]",
            f"[bold white]{_truncate(p.process_name, 15)}[/]",
            cpu_cell,
            memory,
            project,
            format_framework(p.framework),
            uptime,
            what,
        )

    console.print(table)
    console.print()

    count = len(processes)
    noun = "process" if count == 1 else "processes"
    footer = Text()
    footer.append(f"  {count} {noun}", style="dim")
    if filtered:
        footer.append("  ·  ", style="dim")
        footer.append("--all", style="cyan")
        footer.append(" to show everything", style="dim")
    console.print(footer)
    console.print()


def display_port_detail(info: PortInfo | None) -> None:
    """Render a detailed single-port view with location and process tree.

    Parameters
    ----------
    info : PortInfo | None
        Port info to display. Prints an error message if None.

    Examples
    --------
    .. code-block:: python

        from portwhisperer.scanner import get_port_details
        display_port_detail(get_port_details(3000))
    """
    render_header()

    if info is None:
        console.print("[red]  No process found on that port.[/]\n")
        return

    # Port heading + divider
    console.print(f"  [bold white]Port :{info.port}[/]")
    console.print(f"[dim]  {'─' * 22}[/]")
    console.print()

    def _row(label: str, value: str) -> None:
        # Pad label to 16 chars so values align vertically
        console.print(f"  [dim]{label:<16}[/] {value}")

    process = info.process_name or info.raw_name or "—"
    _row("Process", f"[bold white]{process}[/]")
    _row("PID", f"[dim]{info.pid}[/]")
    _row("Status", format_status(info.status))
    _row("Framework", format_framework(info.framework))
    _row("Memory", f"[green]{info.memory}[/]" if info.memory else "[dim]—[/]")
    _row("Uptime", f"[yellow]{info.uptime}[/]" if info.uptime else "[dim]—[/]")

    if info.start_time:
        # Format start_time just like JS's toLocaleString()
        started = info.start_time.strftime("%Y-%m-%d %H:%M:%S")
        _row("Started", f"[dim]{started}[/]")

    # Location section
    console.print()
    console.print("  [bold cyan]Location[/]")
    console.print(f"[dim]  {'─' * 22}[/]")
    _row("Directory", f"[blue]{info.cwd}[/]" if info.cwd else "[dim]—[/]")
    _row(
        "Project",
        f"[white]{info.project_name}[/]" if info.project_name else "[dim]—[/]",
    )
    _row(
        "Git Branch",
        f"[magenta]{info.git_branch}[/]" if info.git_branch else "[dim]—[/]",
    )

    # Process tree — show ancestry chain with indented Rich Tree widget
    if info.process_tree:
        console.print()
        console.print("  [bold cyan]Process Tree[/]")
        console.print(f"[dim]  {'─' * 22}[/]")

        tree = Tree("")
        # process_tree is ordered from the target PID up toward init
        # Build nested nodes so each level is a child of the previous
        node = tree
        for i, entry in enumerate(info.process_tree):
            prefix = "→" if i == 0 else "└─"
            pid_style = "bold white" if entry["pid"] == info.pid else "dim"
            label = (
                f"[dim]{prefix}[/] [{pid_style}]{entry['name']}[/] "
                f"[dim]({entry['pid']})[/]"
            )
            node = node.add(label)

        console.print("  ", tree)

    console.print()
    console.print(
        f"  [dim]Kill this process: [/][cyan]ports clean[/]"
        f"[dim] or [/][red]kill {info.pid}[/]"
    )
    console.print()


def display_clean_results(
    orphaned: list[PortInfo],
    killed: list[int],
    failed: list[int],
) -> None:
    """Render the results of a ``ports clean`` run.

    Parameters
    ----------
    orphaned : list[PortInfo]
        All orphaned/zombie ports that were targeted.
    killed : list[int]
        PIDs successfully killed.
    failed : list[int]
        PIDs that could not be killed.

    Examples
    --------
    .. code-block:: python

        display_clean_results(orphaned, killed=[], failed=[])
    """
    render_header()

    if not orphaned:
        console.print(
            "[green]  ✓ No orphaned or zombie processes found. All clean![/]\n"
        )
        return

    count = len(orphaned)
    noun = "process" if count == 1 else "processes"
    console.print(f"[bold yellow]  Found {count} orphaned/zombie {noun}:[/]\n")

    for p in orphaned:
        was_killed = p.pid in killed
        did_fail = p.pid in failed

        # Show outcome icon: checkmark killed, cross failed, question unknown
        if was_killed:
            icon = "[green]✓[/]"
        elif did_fail:
            icon = "[red]✕[/]"
        else:
            icon = "[yellow]?[/]"

        console.print(
            f"  {icon} :[bold white]{p.port}[/] [dim]—[/] "
            f"{p.process_name} [dim](PID {p.pid})[/]"
        )
        if did_fail:
            console.print(f"[red]    Failed to kill. Try: sudo kill -9 {p.pid}[/]")

    console.print()
    if killed:
        k = len(killed)
        console.print(f"[green]  Cleaned {k} process{'es' if k != 1 else ''}.[/]")
    if failed:
        f_count = len(failed)
        noun = "processes" if f_count != 1 else "process"
        console.print(f"[red]  Failed to clean {f_count} {noun}.[/]")
    console.print()


def display_watch_event(event_type: str, info: PortInfo | dict[str, int]) -> None:
    """Print a single watch-mode event line with a timestamp prefix.

    Parameters
    ----------
    event_type : str
        Either ``"new"`` for an opened port or ``"removed"`` for a closed one.
    info : PortInfo | dict[str, int]
        For ``"new"`` events, a :class:`~portwhisperer.scanner.PortInfo`.
        For ``"removed"`` events, a dict with a ``"port"`` key.

    Examples
    --------
    .. code-block:: python

        display_watch_event("new", port_info)
        display_watch_event("removed", {"port": 3000})
    """
    timestamp = f"[dim]{datetime.now().strftime('%H:%M:%S')}[/]"

    if event_type == "new" and isinstance(info, PortInfo):
        fw = f" {format_framework(info.framework)}" if info.framework else ""
        proj = f" [blue][{info.project_name}][/]" if info.project_name else ""
        console.print(
            f"  {timestamp} [green]▲ NEW[/]    "
            f":[bold white]{info.port}[/] ← [white]{info.process_name}[/]{proj}{fw}"
        )
    elif event_type == "removed":
        removed_info = cast(dict[str, int], info)
        console.print(
            f"  {timestamp} [red]▼ CLOSED[/] :[bold white]{removed_info['port']}[/]"
        )


def display_watch_header() -> None:
    """Print the watch-mode startup message after the header banner.

    Examples
    --------
    .. code-block:: python

        display_watch_header()
    """
    render_header()
    console.print("[bold cyan]  Watching for port changes...[/]")
    console.print("[dim]  Press Ctrl+C to stop[/]\n")
