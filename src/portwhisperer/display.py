from __future__ import annotations

import dataclasses
import json
from datetime import datetime

from rich import box
from rich.console import Console
from rich.markup import escape
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from portwhisperer.scanner import KillResult, PortInfo, ProcessInfo, is_loopback

console = Console()

# The CLI turns the banner off when stdout is not a terminal or with --quiet
_show_banner = True

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


def set_banner(enabled: bool) -> None:
    """Turn the banner that starts each view on or off.

    Parameters
    ----------
    enabled : bool
        False hides the banner, e.g. when output goes to a pipe.

    Examples
    --------
    .. code-block:: python

        set_banner(sys.stdout.isatty())
    """
    global _show_banner
    _show_banner = enabled


def render_header() -> None:
    """Print the Port Whisperer banner as a Rich Panel, unless it is turned off.

    Examples
    --------
    .. code-block:: python

        render_header()
    """
    if not _show_banner:
        return
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
        One of "healthy", "detached", "zombie", or any other value.

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
        "detached": ("[blue]●[/]", "[blue]detached[/]"),
        "zombie": ("[red]●[/]", "[red]zombie[/]"),
    }
    icon, label = _map.get(status, ("[dim]●[/]", f"[dim]{status}[/]"))
    return f"{icon} {label}"


def format_address(address: str | None) -> str:
    """Return a short label for who can reach a listen address.

    Parameters
    ----------
    address : str | None
        Host part of the lsof NAME, e.g. ``"*"`` or ``"127.0.0.1"``.

    Returns
    -------
    str
        ``local`` for loopback (this machine only), ``LAN`` for all
        interfaces, or the address itself for one network interface.

    Examples
    --------
    .. code-block:: python

        format_address("127.0.0.1")  # "[dim]local[/]"
        format_address("*")          # "[yellow]LAN[/]"
    """
    if not address:
        return "[dim]—[/]"
    if is_loopback(address):
        return "[dim]local[/]"
    if address in ("*", "0.0.0.0", "[::]"):
        return "[yellow]LAN[/]"
    return f"[yellow]{escape(address)}[/]"


def _json_default(value: object) -> object:
    """Serialise the values that :mod:`json` cannot: dataclasses and datetimes."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")


def to_json(data: object, indent: int | None = 2) -> str:
    """Turn dataclasses (or lists of them) into JSON text.

    Parameters
    ----------
    data : object
        A dataclass instance, a list of them, or plain JSON values.
    indent : int | None
        Indentation; None gives one compact line.

    Returns
    -------
    str
        JSON with ``datetime`` values in ISO 8601.

    Examples
    --------
    .. code-block:: python

        to_json(get_listening_ports())
    """
    return json.dumps(data, indent=indent, default=_json_default)


def display_json(data: object) -> None:
    """Print data as JSON on stdout, with no banner and no colour.

    Parameters
    ----------
    data : object
        A dataclass instance, a list of them, or plain JSON values.

    Examples
    --------
    .. code-block:: python

        display_json(get_listening_ports())
    """
    print(to_json(data), flush=True)


def _cell(text: str | None, style: str) -> Text:
    """Build a table cell that never wraps and ends in "…" when cut short.

    Columns made of these cells may shrink, so they give up width first.
    """
    if not text:
        return Text("—", style="dim", no_wrap=True)
    return Text(text, style=style, no_wrap=True, overflow="ellipsis")


def _process_label(name: str, tool: str | None) -> str:
    """Name the tool when an interpreter runs a known one, else the process."""
    return tool or name or "—"


def display_port_table(
    ports: list[PortInfo], filtered: bool = False, project: str | None = None
) -> None:
    """Render all listening ports in a Rich table and print a summary footer.

    Parameters
    ----------
    ports : list[PortInfo]
        Ports to display.
    filtered : bool
        When True, append a ``--all`` hint to the footer.
    project : str | None
        Project root the ports were filtered to (``ports here``).

    Examples
    --------
    .. code-block:: python

        from portwhisperer.scanner import get_listening_ports
        display_port_table(get_listening_ports())
    """
    render_header()

    if project:
        console.print(f"  [dim]Project[/] [blue]{escape(project)}[/]\n")

    if not ports:
        if project:
            console.print("[dim]  No server of this project is listening.[/]\n")
            return
        console.print("[dim]  No active listening ports found.[/]\n")
        console.print(
            "[dim]  Start a dev server and run [/][cyan]ports[/][dim] again.[/]\n"
        )
        return

    table = Table(
        box=box.SIMPLE_HEAVY, show_footer=False, pad_edge=False, collapse_padding=True
    )
    # Fixed columns never wrap; PROJECT and PROCESS hold Text cells that may
    # shrink, so they lose width first at 80 columns
    table.add_column("[bold cyan]PORT[/]", no_wrap=True)
    table.add_column("[bold cyan]ADDRESS[/]", no_wrap=True)
    table.add_column("[bold cyan]PROCESS[/]", max_width=16)
    table.add_column("[bold cyan]PID[/]", no_wrap=True)
    table.add_column("[bold cyan]PROJECT[/]", max_width=20)
    table.add_column("[bold cyan]FRAMEWORK[/]", no_wrap=True, overflow="ellipsis")
    table.add_column("[bold cyan]UPTIME[/]", no_wrap=True)
    table.add_column("[bold cyan]STATUS[/]", no_wrap=True)

    for p in ports:
        uptime = f"[yellow]{p.uptime}[/]" if p.uptime else "[dim]—[/]"
        process = _process_label(p.process_name or p.raw_name, p.tool)

        table.add_row(
            f"[bold white]:{p.port}[/]",
            format_address(p.address),
            _cell(process, "white"),
            f"[dim]{p.pid}[/]",
            _cell(p.project_name, "blue"),
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

    table = Table(
        box=box.SIMPLE_HEAVY, show_footer=False, pad_edge=False, collapse_padding=True
    )
    # Fixed columns never wrap; PROCESS, PROJECT and WHAT hold Text cells that
    # may shrink, so they lose width first at 80 columns
    table.add_column("[bold cyan]PID[/]", no_wrap=True)
    table.add_column("[bold cyan]PROCESS[/]", max_width=15)
    table.add_column("[bold cyan]CPU%[/]", no_wrap=True)
    table.add_column("[bold cyan]MEM[/]", no_wrap=True)
    table.add_column("[bold cyan]PROJECT[/]", max_width=20)
    table.add_column("[bold cyan]FRAMEWORK[/]", no_wrap=True, overflow="ellipsis")
    table.add_column("[bold cyan]UPTIME[/]", no_wrap=True)
    table.add_column("[bold cyan]WHAT[/]", max_width=30)

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
        uptime = f"[yellow]{p.uptime}[/]" if p.uptime else "[dim]—[/]"

        table.add_row(
            f"[dim]{p.pid}[/]",
            _cell(_process_label(p.process_name, p.tool), "bold white"),
            cpu_cell,
            memory,
            _cell(p.project_name, "blue"),
            format_framework(p.framework),
            uptime,
            _cell(p.description or p.process_name, "dim"),
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

    # The interpreter stays visible here; the tables show only the tool
    process = escape(info.raw_name or info.process_name or "—")
    tool = f" [dim]running[/] [white]{escape(info.tool)}[/]" if info.tool else ""
    _row("Process", f"[bold white]{process}[/]{tool}")
    _row("PID", f"[dim]{info.pid}[/]")
    if info.address:
        reach = (
            "this machine only"
            if is_loopback(info.address)
            else "reachable from the network"
        )
        _row("Address", f"{escape(info.address)}:{info.port} [dim]({reach})[/]")
    _row("Status", format_status(info.status))
    _row("Framework", format_framework(info.framework))
    _row("Memory", f"[green]{info.memory}[/]" if info.memory else "[dim]—[/]")
    _row("Uptime", f"[yellow]{info.uptime}[/]" if info.uptime else "[dim]—[/]")

    if info.start_time:
        # Format start_time just like JS's toLocaleString()
        started = info.start_time.strftime("%Y-%m-%d %H:%M:%S")
        _row("Started", f"[dim]{started}[/]")
    if info.other_ports:
        others = ", ".join(f":{port}" for port in info.other_ports)
        _row("Also listens", f"[white]{others}[/]")
    if info.command:
        # Fold long commands instead of cutting them: this is the field that
        # answers "what is this?"
        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=16, style="dim")
        grid.add_column(overflow="fold")
        grid.add_row("Command", Text(info.command, style="white"))
        console.print(Padding(grid, (0, 0, 0, 2)))

    if info.container:
        console.print()
        console.print("  [bold cyan]Container[/]")
        console.print(f"[dim]  {'─' * 22}[/]")
        _row("Name", f"[white]{escape(info.container)}[/]")
        _row("Image", f"[white]{escape(info.image)}[/]" if info.image else "[dim]—[/]")
        if info.container_id:
            _row("ID", f"[dim]{info.container_id[:12]}[/]")

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

        # process_tree runs from the target up toward init; draw it the other
        # way, oldest ancestor at the root and the target as the deepest leaf
        def _label(entry: dict) -> str:
            style = "bold white" if entry["pid"] == info.pid else "white"
            return f"[{style}]{escape(entry['name'])}[/] [dim]({entry['pid']})[/]"

        root, *rest = reversed(info.process_tree)
        tree = node = Tree(_label(root), guide_style="dim")
        for entry in rest:
            node = node.add(_label(entry))

        console.print(Padding(tree, (0, 0, 0, 2), expand=False))

    console.print()
    console.print(f"  [dim]Kill this process: [/][cyan]ports kill {info.port}[/]")
    console.print()


def display_clean_candidates(candidates: list[PortInfo]) -> None:
    """Render the numbered list that ``ports clean`` asks the user to pick from.

    Parameters
    ----------
    candidates : list[PortInfo]
        Detached and zombie dev servers, from
        :func:`~portwhisperer.scanner.find_clean_candidates`.

    Examples
    --------
    .. code-block:: python

        display_clean_candidates(find_clean_candidates())
    """
    render_header()

    if not candidates:
        console.print(
            "[green]  ✓ Nothing to clean: no detached or zombie dev servers.[/]"
        )
        console.print("[dim]  Daemons (--daemon, launchd jobs) are left alone.[/]\n")
        return

    count = len(candidates)
    noun = "server" if count == 1 else "servers"
    console.print(f"[bold]  {count} detached or zombie dev {noun}:[/]\n")
    for i, p in enumerate(candidates, 1):
        console.print(
            f"  [dim]{i}.[/] :[bold white]{p.port}[/] [dim]—[/] "
            f"{_target_label(p)}  {format_status(p.status)}"
        )
    console.print()


def _target_label(p: PortInfo) -> str:
    """Describe what a kill would stop: the container, or the process and PID."""
    if p.container:
        return f"container {escape(p.container)}"
    return f"{escape(p.process_name)} [dim](PID {p.pid})[/]"


def display_kill_targets(targets: list[PortInfo]) -> None:
    """List the ports that ``ports kill`` is about to free.

    Parameters
    ----------
    targets : list[PortInfo]
        Ports to free.

    Examples
    --------
    .. code-block:: python

        display_kill_targets([get_port_details(3000)])
    """
    console.print()
    for p in targets:
        console.print(
            f"  [dim]•[/] :[bold white]{p.port}[/] [dim]—[/] {_target_label(p)}"
        )
    console.print()


def display_kill_results(results: list[KillResult]) -> None:
    """Print one line per port with the outcome of a kill.

    Parameters
    ----------
    results : list[KillResult]
        Outcomes from :func:`~portwhisperer.scanner.free_port`.

    Examples
    --------
    .. code-block:: python

        display_kill_results([free_port(info)])
    """
    console.print()
    for r in results:
        icon = "[green]✓[/]" if r.ok else "[red]✕[/]"
        style = "green" if r.ok else "red"
        pid = f" [dim](PID {r.pid})[/]" if r.pid is not None else ""
        console.print(
            f"  {icon} :[bold white]{r.port}[/]{pid} [dim]—[/] "
            f"[{style}]{escape(r.detail)}[/]"
        )
    console.print()


def display_watch_event(event_type: str, info: PortInfo) -> None:
    """Print a single watch-mode event line with a timestamp prefix.

    Parameters
    ----------
    event_type : str
        ``"new"`` for an opened port, ``"restarted"`` for a new PID on the
        same port, or ``"closed"`` for a port nobody listens on any more.
    info : PortInfo
        The port; for ``"closed"``, the last info seen before it closed.

    Examples
    --------
    .. code-block:: python

        display_watch_event("new", port_info)
        display_watch_event("closed", port_info)
    """
    timestamp = f"[dim]{datetime.now().strftime('%H:%M:%S')}[/]"
    labels = {
        "new": "[green]▲ NEW[/]      ",
        "restarted": "[yellow]↻ RESTARTED[/]",
        "closed": "[red]▼ CLOSED[/]   ",
    }
    console.print(f"  {timestamp} {labels[event_type]} {_watch_line(info)}")


def _watch_line(info: PortInfo) -> str:
    """Describe one port for watch output: port, address, process, project."""
    process = escape(_process_label(info.process_name, info.tool))
    fw = f" {format_framework(info.framework)}" if info.framework else ""
    proj = f" [blue]\\[{escape(info.project_name)}][/]" if info.project_name else ""
    return (
        f":[bold white]{info.port}[/] {format_address(info.address)} "
        f"← [white]{process}[/] [dim]({info.pid})[/]{proj}{fw}"
    )


def display_watch_snapshot(ports: list[PortInfo]) -> None:
    """Print the ports that are open when ``ports watch`` starts.

    Parameters
    ----------
    ports : list[PortInfo]
        Ports listening at the first poll.

    Examples
    --------
    .. code-block:: python

        display_watch_snapshot(get_listening_ports())
    """
    noun = "port" if len(ports) == 1 else "ports"
    console.print(f"[bold]  Listening now ({len(ports)} {noun})[/]")
    for p in ports:
        console.print(f"  {_watch_line(p)}")
    console.print()
    console.print("[bold cyan]  Watching for changes...[/]")
    console.print("[dim]  Press Ctrl+C to stop[/]\n")


def display_watch_header() -> None:
    """Print the banner before watch mode starts.

    Examples
    --------
    .. code-block:: python

        display_watch_header()
    """
    render_header()


def display_watch_json(event_type: str, data: PortInfo | list[PortInfo]) -> None:
    """Print watch events as JSON, one object per line.

    The snapshot prints one ``"snapshot"`` line per open port, then one
    ``{"event": "ready", "time": ...}`` line, so a reader knows the snapshot
    is complete even when no port is open. Port lines hold the port's fields
    plus ``event`` and ``time`` (ISO 8601).

    Parameters
    ----------
    event_type : str
        ``"snapshot"``, ``"new"``, ``"restarted"`` or ``"closed"``.
    data : PortInfo | list[PortInfo]
        A list for ``"snapshot"``, one port otherwise.

    Examples
    --------
    .. code-block:: python

        display_watch_json("new", port_info)
    """
    now = datetime.now().isoformat(timespec="seconds")
    for info in [data] if isinstance(data, PortInfo) else data:
        line = {"event": event_type, "time": now, **dataclasses.asdict(info)}
        print(to_json(line, indent=None), flush=True)
    if event_type == "snapshot":
        print(to_json({"event": "ready", "time": now}, indent=None), flush=True)
