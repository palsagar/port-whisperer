# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

portwhisperer is a Python CLI tool (`ports`) that shows what's running on TCP ports — framework detection, process info, and interactive management. Uses click for CLI and rich for terminal output. macOS only (shells out to `lsof`, `ps`, and `docker ps`). An optional user config at `~/.config/portwhisperer/config.toml` extends the dev/ignore process lists.

## Commands

```bash
uv sync                  # install deps
source .venv/bin/activate
ports                    # run the CLI
ruff check src/          # lint
ruff format src/         # format
ty check src/            # type check
uv run pytest            # run tests
```

No build step. Tests live in `tests/`. They monkeypatch `subprocess.run` to serve saved
`lsof`/`ps`/`docker` output from `tests/fixtures/`, so they run on any OS.

## Architecture

Three source files:

- `src/portwhisperer/cli.py` — CLI entrypoint and command router. Defines the `main` Click group and subcommands (`ps`, `kill`, `clean`, `watch`, `open`, `next`, `here`). Handles `--json`, `-q`, `--version`, exit codes for `ports <port>` (0 busy / 1 free / 2 invalid), interactive kill prompts (skipped when stdin is not a TTY) and Docker process collapsing for `ps`. Turns the banner off when stdout is not a TTY (`_stdout_is_tty`, separate from `_stdin_is_tty`).
- `src/portwhisperer/scanner.py` — All data gathering. Shells out to `lsof`, `ps`, and `docker ps`. Exposes `PortInfo`, `ProcessInfo` and `KillResult` dataclasses. Public functions: `get_listening_ports`, `get_all_processes`, `get_port_details`, `find_clean_candidates`, `free_port`, `watch_ports`, `find_project_root`, `is_dev_process`, `is_docker_process`, `container_engine`, `is_loopback`. Also reads the user config (`_user_process_lists`, cached).
- `src/portwhisperer/display.py` — All terminal output. Uses rich tables, panels, and trees. Pure rendering — takes data, prints it. Public functions: `display_port_table`, `display_process_table`, `display_port_detail`, `display_clean_candidates`, `display_kill_targets`, `display_kill_results`, `display_watch_event`, `display_watch_snapshot`, `display_watch_header`, `display_watch_json`, `display_json`, `set_banner`. `render_header()` is the one place the banner is gated.

Data flow: cli calls scanner functions → gets `PortInfo` / `ProcessInfo` dataclasses → passes to display functions.

## Key Design Decisions

- **Batch shell calls**: `get_listening_ports` uses three shell calls total — one `lsof` for ports, one `ps` for all PIDs at once, one `lsof -d cwd` for all cwds at once. Single-port detail mode adds git branch and process tree lookups.
- **Dev process filtering**: `is_dev_process()` in scanner.py maintains a blocklist of system/desktop apps and an allowlist of dev runtimes. The user config's `ignore_processes`, then `dev_processes`, win over both. Default views (`ports`, `ps`, `watch`) filter to dev processes only; `--all` skips the filter.
- **Listen address and tool names**: `PortInfo.address` keeps the widest address among a port's lsof rows. `tool` names what an interpreter runs (`uvicorn`, `vite`); tables show it, `process_name` stays the interpreter for filtering.
- **One-line tables**: fixed columns are `no_wrap`; PROCESS/PROJECT/WHAT hold `Text(no_wrap=True, overflow="ellipsis")` cells in wrappable columns, so Rich shrinks them first.
- **Watch**: `watch_ports` tracks `(port, pid)`, emits `snapshot` once, then `new` / `restarted` / `closed`.
- **Framework detection cascade**: `_detect_framework_from_command` → `_detect_framework` (inspects `package.json` deps and config files in cwd) → `_detect_framework_from_image` (for Docker) → `_detect_framework_from_name` (process name fallback).
- **Custom Click group**: `_PortGroup` rewrites a leading integer argument (e.g. `ports 3000`) to `--whoisonport 3000` before Click parses it, so bare port numbers coexist with named subcommands.
- **Docker collapsing**: The `ps` subcommand merges all `com.docker.*` / `Docker*` processes into a single summary row with summed CPU and memory, to avoid noise.
- **Safe killing**: every kill goes through `free_port`. It reports success only when `lsof` shows nothing listening on the port. Container ports use `docker stop <container>`; `container_engine()` recognises Docker Desktop, OrbStack, Lima/Colima, Rancher Desktop and Podman forwarders, which never get a signal. Before `docker stop`, `_endpoint_serves_listener` checks that the listening PID holds the endpoint's socket (or, for single-instance engines, a process with the same name does). Zombies get no signal.
- **Detached vs daemon**: status `detached` means parent PID 1 (a fact, not a warning). `clean` skips detached processes started with `--daemon` or managed by launchd (`launchctl list`).
