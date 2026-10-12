# portwhisperer

[![PyPI](https://img.shields.io/pypi/v/portwhisperer)](https://pypi.org/project/portwhisperer/)
[![Python](https://img.shields.io/pypi/pyversions/portwhisperer)](https://pypi.org/project/portwhisperer/)
[![License](https://img.shields.io/pypi/l/portwhisperer)](https://github.com/palsagar/port-whisperer/blob/main/LICENSE)

**A beautiful CLI tool to see what's running on your ports.**

Stop guessing which process is on port 3000. `portwhisperer` gives you a color-coded table of every dev server, database, and background process listening on your machine -- with framework detection, Docker container identification, and interactive process management.

## What it looks like

```
$ ports

╭────────────────────────────╮
│ 🔊 Port Whisperer          │
│ listening to your ports... │
╰────────────────────────────╯

 PORT   ADDRESS  PROCESS  PID    PROJECT          FRAMEWORK   UPTIME  STATUS
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 :3000  local    next     42872  frontend         Next.js     1d 9h   ● healthy
 :5173  LAN      vite     95380  preview-app      Vite        2h 40m  ● healthy
 :5432  LAN      docker   58351  backend-postgr…  PostgreSQL  10d 3h  ● healthy
 :6379  LAN      docker   58351  backend-redis-1  Redis       10d 3h  ● healthy
 :8000  local    uvicorn  61204  api              FastAPI     3h 12m  ● healthy

  5 ports active  ·  Run ports <number> for details  ·  --all to show everything
```

- **ADDRESS**: `local` means only this machine can connect (127.0.0.1 or ::1). `LAN` means the server listens on all interfaces, so other machines on your network can reach it. A single network address is shown as is.
- **PROCESS** names the tool when an interpreter runs a known one: `uvicorn` instead of `python3.13`, `vite` instead of `node`.
- **STATUS** colors: green = healthy, blue = detached (its parent shell is gone, which is often on purpose), red = zombie.

Every row stays on one line. In a narrow terminal, long names end in `…`.

## Install

```bash
uv tool install portwhisperer    # global install (recommended)
```

```bash
uv pip install portwhisperer     # into current virtual env
```

```bash
pip install portwhisperer        # classic pip
```

```bash
pipx install portwhisperer       # isolated global install
```

Requires Python 3.12+.

## Usage

### Show dev server ports (default)

```bash
ports
```

Shows dev servers, Docker containers, and databases. System apps (Spotify, Raycast, etc.) are filtered out by default.

### Show all listening ports

```bash
ports --all
```

Includes system services, desktop apps, and everything else listening on your machine.

### Inspect a specific port

```bash
ports 3000
# or
ports --whoisonport 3000
```

Detailed view: the listen address, the full command line, other ports held by the same process, the repository path and git branch, the container name and image for Docker ports, and the process tree. An interactive prompt then offers to kill the process. The prompt appears only when stdin and stdout are both terminals, so scripts never hang on it.

```
$ ports 3000

╭────────────────────────────╮
│ 🔊 Port Whisperer          │
│ listening to your ports... │
╰────────────────────────────╯

  Port :3000
  ──────────────────────

  Process          node running next
  PID              42872
  Address          127.0.0.1:3000 (this machine only)
  Status           ● healthy
  Framework        Next.js
  Memory           156.2 MB
  Uptime           1d 9h
  Started          2026-04-02 03:14:00
  Also listens     :9229
  Command          node /Users/dev/frontend/node_modules/.bin/next dev

  Location
  ──────────────────────
  Directory        /Users/dev/frontend
  Project          frontend
  Git Branch       main

  Process Tree
  ──────────────────────
  -zsh (42501)
  └── npm (42860)
      └── node (42872)

  Kill this process: ports kill 3000

  Kill process on :3000? [y/N]
```

### Show all dev processes

```bash
ports ps
```

A beautiful `ps aux` for developers. Shows all running dev processes (not just port-bound ones) with CPU%, memory, framework detection, and a description column. Docker processes are collapsed into a single summary row.

```
$ ports ps

╭────────────────────────────╮
│ 🔊 Port Whisperer          │
│ listening to your ports... │
╰────────────────────────────╯

 PID    PROCESS  CPU%  MEM       PROJECT      FRAMEWORK  UPTIME   WHAT
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 592    Docker   1.3   735.5 MB  —            Docker     13d 12h  14 processes
 36664  python3  0.2   17.6 MB   agents       Python     6d 10h   browser_use …
 26408  jest     0.1   9.2 MB    frontend     Node.js    10d 13h  jest --watch
 25752  node     0.0   17.3 MB   api-gateway  Node.js    10d 13h  server.js
 66921  uvicorn  0.0   41.1 MB   api          FastAPI    2h 25m   uvicorn api.…

  5 processes  ·  --all to show everything
```

```bash
ports ps --all    # show all processes, not just dev
```

### Free a port

```bash
ports kill 3000           # asks once, then frees the port
ports kill 3000 8080 -y   # several ports, no confirmation
ports kill 3000 --force   # SIGKILL right away
```

Sends SIGTERM, waits up to 3 seconds for the port to free up, then sends SIGKILL. It reports success only when nothing listens on the port any more, and exits non-zero otherwise.

- **Docker ports** run `docker stop <container>` (`docker kill` with `--force`). Docker Desktop and your other containers keep running.
- **Other container engines** (OrbStack, Colima, Rancher Desktop, Podman) forward ports through one host process. `ports kill` never signals that process, because that would drop every port it forwards. It stops the container instead, but only after it confirms that the docker CLI talks to the engine that holds the port. If you run two engines and the active `docker context` points at the other one, `ports kill` refuses and stops nothing.
- **Zombies** have already exited, so no signal can remove them. `ports kill` names the parent process that must reap them.
- **Restarting servers**: if a watcher such as nodemon starts the server again under a new PID, `ports kill` says so and fails.

Without a terminal, `ports kill` needs `--yes`.

### Clean up detached and zombie processes

```bash
ports clean
```

Lists zombie and detached dev server processes, numbered, and asks which to kill (`1,3`, `all`, or `none`, the default). Only targets dev runtimes (node, python, etc.) -- won't touch your desktop apps.

Intentional daemons are left out: processes started with `--daemon` and jobs managed by launchd, such as `brew services`.

### Watch for port changes

```bash
ports watch
```

Prints the dev servers that are listening now, then one line each time a port opens, closes, or restarts under a new PID.

```
$ ports watch
  Listening now (2 ports)
  :3000 local ← next (42872) [frontend] Next.js
  :8000 local ← uvicorn (61204) [api] FastAPI

  Watching for changes...
  Press Ctrl+C to stop

  14:02:11 ↻ RESTARTED :8000 local ← uvicorn (61377) [api] FastAPI
  14:02:15 ▲ NEW       :5173 LAN ← vite (95380) [preview-app] Vite
  14:02:40 ▼ CLOSED    :3000 local ← next (42872) [frontend] Next.js
```

```bash
ports watch --interval 5    # poll every 5 seconds instead of 2 (minimum 0.2)
ports watch --all           # include system services and desktop apps
ports watch --json          # one JSON object per line, see Scripting
```

A restart shows up only when the old and the new process are seen on two polls in a row. If the port stays closed for longer than one interval, you see CLOSED and then NEW.

### Open a port in the browser

```bash
ports open 3000    # opens http://localhost:3000
```

Exits 1 and opens nothing when no process listens on the port.

### Find a free port

```bash
ports next         # first free port from 3000 up
ports next 8000    # first free port from 8000 up
```

Prints only the number, so you can use it in scripts: `npm run dev -- --port $(ports next)`. It checks the ports that `lsof` can see, which without `sudo` means your own processes.

### Show the servers of the current project

```bash
ports here
```

Shows only the ports whose server runs inside the current git repository. Outside git, the project is the nearest directory with a project file such as `package.json` or `pyproject.toml`. The dev filter does not apply here: every server of the project is shown.

## Scripting

| Flag | Works with | What it does |
|------|------------|--------------|
| `--json` | `ports`, `ports <port>`, `ports ps`, `ports watch` | Prints JSON instead of a table. No banner, no colour. |
| `-q`, `--quiet` | `ports`, `ports <port>`, `ports ps`, `ports clean`, `ports watch`, `ports here` | Hides the banner in a terminal. |
| `--version` | `ports` | Prints the installed version. |

The banner prints only when stdout is a terminal. `ports | cat` prints the table alone.

**JSON output.** `ports --json` prints an array of port objects; `ports <port> --json` prints one object. Each object holds every field the tool knows, such as `port`, `pid`, `process_name`, `tool`, `address`, `command`, `cwd`, `project_name`, `framework`, `status`, `container` and `image`. `start_time` is ISO 8601. `ports ps --json` lists Docker processes one by one instead of as one summary row. `ports watch --json` prints one object per line, with `event` (`snapshot`, `new`, `restarted` or `closed`) and `time` added to the port's fields.

```bash
ports --json | jq '.[] | select(.address != "127.0.0.1") | .port'
```

**Exit codes for `ports <port>`:**

| Code | Meaning |
|------|---------|
| 0 | A process listens on the port. |
| 1 | The port is free. With `--json`, nothing is printed. |
| 2 | The argument is not a valid port (1–65535). |
| 3 | The ports could not be checked, for example because `lsof` failed. |

```bash
ports 3000 --json > /dev/null || npm run dev    # start the server only if :3000 is free
```

`ports kill` exits 0 only when every port ends up free. `ports open` exits 1 when nothing listens on the port. Every command exits 3 when the `lsof` scan fails, so a failed scan never passes for a free port.

## Configuration

`ports` hides desktop apps and system services by default, using built-in lists. To change what counts as a dev process, create `~/.config/portwhisperer/config.toml` (or `$XDG_CONFIG_HOME/portwhisperer/config.toml`):

```toml
# Always show these processes, even without --all
dev_processes = ["air", "hugo"]

# Always hide these processes, unless you pass --all
ignore_processes = ["ollama"]
```

- Names match the process name exactly, ignoring case. The PROCESS column shows the tool for interpreters, so use the interpreter name (for example `python3.13`) when you want to match one; `ports <port>` shows it.
- Your entries win over the built-in lists. A name in both of your lists is hidden.
- The lists apply to `ports`, `ports ps`, `ports watch` and the detached check used by `ports clean`.
- If the file has invalid TOML, an unknown key, or a value that is not a list of strings, `ports` prints one warning and uses the built-in lists.

## How it works

Three shell calls, runs in ~0.2s:

1. **`lsof -iTCP -sTCP:LISTEN`** -- finds all listening processes
2. **`ps`** (single batched call) -- retrieves process details for all PIDs at once
3. **`lsof -d cwd`** (single batched call) -- resolves working directories

For Docker ports, a single `docker ps` call maps host ports to container names and images. It runs only when a container engine (Docker Desktop, OrbStack, Colima, Rancher Desktop or Podman) holds one of the ports.

Framework detection reads `package.json` dependencies and inspects process command lines.

## Platform support

| Platform | Status |
|----------|--------|
| macOS    | Supported |
| Linux    | Planned |
| Windows  | Not planned |

## License

[MIT](LICENSE)
