# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

portwhisperer is a Python CLI tool (`ports`) that shows what's running on TCP ports — framework detection, process info, and interactive management. Uses click for CLI and rich for terminal output. macOS only (shells out to `lsof`, `ps`, and `docker ps`).

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

- `src/portwhisperer/cli.py` — CLI entrypoint and command router. Defines the `main` Click group and subcommands (`ps`, `kill`, `clean`, `watch`). Handles interactive kill prompts (skipped when stdin is not a TTY) and Docker process collapsing for `ps`.
- `src/portwhisperer/scanner.py` — All data gathering. Shells out to `lsof`, `ps`, and `docker ps`. Exposes `PortInfo`, `ProcessInfo` and `KillResult` dataclasses. Public functions: `get_listening_ports`, `get_all_processes`, `get_port_details`, `find_clean_candidates`, `free_port`, `watch_ports`, `is_dev_process`, `is_docker_process`.
- `src/portwhisperer/display.py` — All terminal output. Uses rich tables, panels, and trees. Pure rendering — takes data, prints it. Public functions: `display_port_table`, `display_process_table`, `display_port_detail`, `display_clean_candidates`, `display_kill_targets`, `display_kill_results`, `display_watch_event`, `display_watch_header`.

Data flow: cli calls scanner functions → gets `PortInfo` / `ProcessInfo` dataclasses → passes to display functions.

## Key Design Decisions

- **Batch shell calls**: `get_listening_ports` uses three shell calls total — one `lsof` for ports, one `ps` for all PIDs at once, one `lsof -d cwd` for all cwds at once. Single-port detail mode adds git branch and process tree lookups.
- **Dev process filtering**: `is_dev_process()` in scanner.py maintains a blocklist of system/desktop apps and an allowlist of dev runtimes. Default view filters to dev processes only; `--all` skips the filter.
- **Framework detection cascade**: `_detect_framework_from_command` → `_detect_framework` (inspects `package.json` deps and config files in cwd) → `_detect_framework_from_image` (for Docker) → `_detect_framework_from_name` (process name fallback).
- **Custom Click group**: `_PortGroup` rewrites a leading integer argument (e.g. `ports 3000`) to `--whoisonport 3000` before Click parses it, so bare port numbers coexist with named subcommands.
- **Docker collapsing**: The `ps` subcommand merges all `com.docker.*` / `Docker*` processes into a single summary row with summed CPU and memory, to avoid noise.
- **Safe killing**: every kill goes through `free_port`. It reports success only when `lsof` shows nothing listening on the port. Docker ports use `docker stop <container>` and never signal Docker's own processes. Zombies get no signal.
- **Detached vs daemon**: status `detached` means parent PID 1 (a fact, not a warning). `clean` skips detached processes started with `--daemon` or managed by launchd (`launchctl list`).
