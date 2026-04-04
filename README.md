# portwhisperer

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

Colors: green = healthy, yellow = orphaned, red = zombie.

## Install

```bash
pip install portwhisperer
```

```bash
uv tool install portwhisperer
```

```bash
pipx install portwhisperer
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

Detailed view: full process tree, repository path, current git branch, memory usage, and an interactive prompt to kill the process.

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

### Clean up orphaned processes

```bash
ports clean
```

Finds and kills orphaned or zombie dev server processes. Only targets dev runtimes (node, python, etc.) -- won't touch your desktop apps.

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
