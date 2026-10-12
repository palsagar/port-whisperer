# portwhisperer

[![PyPI](https://img.shields.io/pypi/v/portwhisperer)](https://pypi.org/project/portwhisperer/)
[![Python](https://img.shields.io/pypi/pyversions/portwhisperer)](https://pypi.org/project/portwhisperer/)
[![License](https://img.shields.io/pypi/l/portwhisperer)](https://github.com/palsagar/port-whisperer/blob/main/LICENSE)

**A beautiful CLI tool to see what's running on your ports.**

Stop guessing which process is on port 3000. `portwhisperer` gives you a color-coded table of every dev server, database, and background process listening on your machine -- with framework detection, Docker container identification, and interactive process management.

## What it looks like

```
$ ports

+----------------------------+
| Port Whisperer             |
| listening to your ports... |
+----------------------------+

 PORT    PROCESS   PID     PROJECT              FRAMEWORK    UPTIME   STATUS
 ---------------------------------------------------------------------------
 :3000   node      42872   frontend             Next.js      1d 9h    ● healthy
 :3001   node      95380   preview-app          Next.js      2h 40m   ● healthy
 :4566   docker    58351   backend-localstack-1 LocalStack   10d 3h   ● healthy
 :5432   docker    58351   backend-postgres-1   PostgreSQL   10d 3h   ● healthy
 :6379   docker    58351   backend-redis-1      Redis        10d 3h   ● healthy

  5 ports active  |  Run ports <number> for details  |  --all to show everything
```

Colors: green = healthy, blue = detached (its parent shell is gone, which is often on purpose), red = zombie.

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

Detailed view: full process tree, repository path, current git branch, memory usage, and an interactive prompt to kill the process. The prompt appears only when stdin is a terminal, so scripts never hang on it.

```
$ ports 3000

+----------------------------+
| Port Whisperer             |
| listening to your ports... |
+----------------------------+

  Port :3000
  --------------------------

  Process          node
  PID              42872
  Status           ● healthy
  Framework        Next.js
  Memory           156.2 MB
  Uptime           1d 9h
  Started          4/2/2026, 3:14:00 AM

  Location
  --------------------------
  Directory        /Users/dev/frontend
  Project          frontend
  Git Branch       main

  Process Tree
  --------------------------
  > node (42872)
    +-- npm (42860)
      +-- zsh (42501)

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

 PID     PROCESS   CPU%   MEM        PROJECT    FRAMEWORK   UPTIME    WHAT
 -------------------------------------------------------------------------
 592     Docker    1.3    735.5 MB   -          Docker      13d 12h   14 processes
 36664   python3   0.2    17.6 MB    -          Python      6d 10h    browser_use...
 26408   node      0.1    9.2 MB     -          Node.js     10d 13h   jest jest_r...
 25752   node      0.0    17.3 MB    -          Node.js     10d 13h   server.js
 66921   Python    0.0    4.1 MB     -          Python      2h 25m    src.server

  5 processes  |  --all to show everything
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

Real-time monitoring that notifies you whenever a port starts or stops listening.

```bash
ports watch --interval 5    # poll every 5 seconds instead of default 2
```

## How it works

Three shell calls, runs in ~0.2s:

1. **`lsof -iTCP -sTCP:LISTEN`** -- finds all listening processes
2. **`ps`** (single batched call) -- retrieves process details for all PIDs at once
3. **`lsof -d cwd`** (single batched call) -- resolves working directories

For Docker ports, a single `docker ps` call maps host ports to container names and images.

Framework detection reads `package.json` dependencies and inspects process command lines.

## Platform support

| Platform | Status |
|----------|--------|
| macOS    | Supported |
| Linux    | Planned |
| Windows  | Not planned |

## License

[MIT](LICENSE)
