# qsv-clientd

`qsv-clientd` is a per-user daemon, shipped with qsv-client, that runs qsv for two kinds of
callers:

- **Local processes** — anything running as you. They get exactly what `Qsv.run` gives them
  (any command, stdin, `stdout_path`, typed errors) without spawning or locating qsv
  themselves, and share one capabilities probe and one job limit.
- **Data Schematics** — the self-contained HTML that `qsv viz smart` writes. When the daemon
  is running, an open schematic can, after you approve it once, drill into the rows behind
  a chart, refresh its statistics, re-render itself, run read-only ad-hoc queries, and fetch
  the latest data from its source URL or PID. Without the daemon the schematic works exactly
  as before.

It needs nothing beyond the Python standard library, and runs on Python 3.10+ like the rest
of the package.

## Running it

```bash
qsv-clientd serve -v          # foreground
qsv-clientd install           # launchd agent (macOS) or systemd --user unit (Linux)
qsv-clientd status
```

On Windows, `install` prints the `schtasks` command to run.

`serve` logs to stderr (`-v` adds INFO lines: pairing requests and decisions, datasets
registered and forgotten, renders, fetches, and revoked tokens; tokens themselves are never
logged). `--log-file PATH` also writes them to `PATH`, rotated at 5 MB with 3 old files
kept. The launchd agent already sends stderr to `~/Library/Logs/qsv-clientd.log`, and
systemd to the journal (`journalctl --user -u qsv-clientd`).

To embed the daemon in another program (a GUI that needs its main thread, say), run it on a
background thread with `qsv_client.daemon.host.DaemonHost(Daemon(config)).start()`; `stop()`
closes it, and `client()` returns a `DaemonClient` for it. A host starts once: to restart,
stop it and build a new `Daemon` and `DaemonHost`.

Services don't read your shell profile, so `install` finds qsv the way the daemon would
(`binary` in `daemon.json`, then `$QSV_BIN`, then your PATH) and writes that absolute path
into the service as `QSV_BIN`. It fails if no qsv is found. Run `install` again after
moving qsv. Other `QSV_*` settings in your shell are not copied, because the service file
is world-readable and they can hold secrets such as `QSV_LLM_APIKEY`. Set those in the
service's environment yourself if runs through the daemon need them. A Windows scheduled
task gets your user environment variables, as set with `setx`, not those of the current
shell.

Configuration is optional: `daemon.json` in the config directory
(`~/.config/qsv-client/` on Linux, `~/Library/Application Support/qsv-client/` on macOS,
`%APPDATA%\qsv-client\` on Windows). Keys: `binary`, `port` (default `14470`; `0` turns the
browser listener off), `socket`, `max_jobs`, `max_timeout`, `inherit_env`, `kill_grace`,
`browser`, `browser_origins` (default `["null"]`), `browser_timeout`, `browser_max_output`,
`browser_max_result`, `browser_max_render`, `token_ttl`, `allow_fetch`, `fetch_max_bytes`
(default 10 GiB), `fetch_timeout` (default 1800s). Unknown keys are an error.
`QSV_CLIENTD_HOME` puts every file under one directory (handy for tests). The socket path
(`socket`, or `QSV_CLIENTD_SOCKET`) must fit a Unix socket address, about 104 bytes on macOS
and 108 on Linux; the daemon refuses a longer one with an error naming both settings.

## Tray

```bash
pipx install 'qsv-client[tray]'    # adds pystray and Pillow
qsv-clientd-tray
```

`qsv-clientd-tray` puts the qsv logo in the menu bar (macOS) or notification area (Windows,
Linux). Its dot shows the daemon's state: grey stopped, green running, amber busy, magenta
when a schematic is waiting to be paired, red on an error. The menu shows the status, qsv's
version, running jobs and datasets, and lets you start, stop and restart the daemon.

- **Hosted:** when no daemon is running, the tray runs one itself, on a background thread,
  with your `daemon.json`. Quitting the tray stops it. Restart reads `daemon.json` again, and
  unpairs every open schematic, since tokens are kept in memory.
- **Attached:** when a daemon is already running (the service `install` set up, or
  `qsv-clientd serve`), the tray only shows it. Quitting leaves it running. If that daemon
  is the launchd or systemd service, **Stop service and run it here** stops the service
  until your next login and hosts the daemon in the tray instead. A stopped tray attaches to
  a daemon that starts later.

One tray runs per user. It logs to `~/Library/Logs/qsv-client/qsv-clientd.log` on macOS, and
`qsv-clientd.log` in the state directory elsewhere (`-v` adds debug lines).

On Linux, pystray needs the AppIndicator or GTK bindings for Python (`python3-gi` and the
AppIndicator typelib from your distribution); in a pipx or uv environment, create it with
`--system-site-packages` so they are visible. GNOME also needs the AppIndicator extension.
Without them the icon falls back to plain X, which has no menus; the log says so.

## Local clients

```python
from qsv_client.daemon import DaemonClient, AsyncDaemonClient, qsv_or_daemon

qsv = DaemonClient()  # same run() arguments and results as Qsv
print(qsv.run("count", "data.csv").stdout)

client = qsv_or_daemon()  # the daemon if it is running, else a local Qsv
```

Relative paths resolve against the caller's working directory (`cwd=` to change it), as
they would locally. The daemon's own environment is used, plus any `env=` you pass. A
connection that drops mid-run cancels the run, and the whole qsv process tree is killed as
with any timeout. `timeout` also bounds the wait for one of the daemon's `max_jobs` slots;
if none frees up in time, the run raises `QsvTimeout` without starting qsv. Failures raise the
same `QsvError` subclasses as a local run, and an unwritable `stdout_path` or missing `cwd`
raises the same `OSError`. `QsvDaemonError` means the daemon refused the request, and
`QsvDaemonUnavailable` means none is listening.

From a shell: `qsv-clientd run count data.csv`. Add `--stdin` (or pass `-` as the input) to
send the shell's stdin: `cat data.csv | qsv-clientd run --stdin count`. Without it, stdin is
left alone, so `qsv-clientd run` is safe inside `while read` loops.

## Data Schematics

```bash
qsv viz smart data.csv -o data.html
qsv-clientd register data.csv --schematic data.html [--source-url URL] [-- <viz args>]
# or render through the daemon, which registers it in one step:
qsv-clientd viz data.csv -o data.html -- --dict-info
```

Open `data.html`. When it finds the daemon it offers to connect and shows a code such as
`K7Q-4MZ`; run `qsv-clientd approve K7Q-4MZ`, which tells you which dataset and which
origin the request came from (`null` for a page opened from a file, but also for a sandboxed
frame on any website, so approve only codes you asked for). The page then holds a token for that one dataset for 8 hours
(`token_ttl`). `qsv-clientd revoke` withdraws every token. `qsv-clientd forget <id>`
unregisters a dataset.

## Security model

| | Local tier | Browser tier |
|---|---|---|
| How | Unix socket (`0600`, in a `0700` directory), or loopback with the local token and no `Origin` header | loopback `127.0.0.1:<port>` |
| Proves | being you (filesystem permissions) | nothing until paired |
| Can | anything `Qsv.run` can | allowlisted read-only actions on one paired dataset |

The browser tier is built around three facts:

- **A browser can reach loopback.** Any web page can send requests to `127.0.0.1`.
- **Origin proves nothing.** A `file://` page sends `Origin: null`, and so does a sandboxed
  iframe on any website.
- **Approval is the real check.** So pairing needs a code that you approve in a shell.

On that basis the browser tier:
- **Checks Host.** Requests must name `127.0.0.1:<port>` or `localhost:<port>` as `Host`,
  which defeats DNS rebinding.
- **Restricts pairing origins.** Only origins in `browser_origins` may ask to pair.
- **Rate-limits and expires pairing.** Requests are limited to 5 a minute and 10 pending,
  and codes expire after 5 minutes.
- **Doesn't reveal registrations.** The answer to a pairing request is the same whether or
  not the schematic is registered. `hello` says only `{"service":"qsv-clientd","api":1}`.
- **Keeps tokens scoped and unstored.** A token is scoped to one dataset, expires, is handed
  out once, and is stored only as a hash. A page's Disconnect withdraws its own token.
- **Never takes a path from the page.** The daemon adds the registered file itself, and
  re-renders use only the viz arguments you registered.
- **Uses a command allowlist.** Ad-hoc queries are limited to `count`, `headers`, `stats`,
  `frequency`, `slice`, `search`, `select`, `sort`, `dedup`, `sample` and `table`. Queries
  run with `QSV_STATSCACHE_MODE=none` and `stats` with `--cache-threshold 0`, so a page never
  makes qsv write cache files beside your data. Each has its own set of flags, and positional
  arguments are limited. `table`'s `--width` and `--pad` are capped at 100, since they
  multiply the output size before the output cap applies.
  - **Refused:** `-o`/`--output`, attached short-flag values, extra positionals, and any
    flag not in the command's set.
  - **Never listed:** commands that read other files, use the network or run code, such as
    `sqlp`, `luau`, `py`, `fetch`, `describegpt`, `to` and `join`.
- **Caps outputs.** Output is capped at 4 MiB (`browser_max_output`), with a 60s timeout
  (`browser_timeout`).
- **Bounds connections.** A client gets 10s to send its request head and 60s for the body,
  and at most 64 loopback connections are open at once, so other local users can't exhaust
  the daemon's file descriptors. On POSIX your own clients use the socket and aren't affected
  by the cap; on Windows they share it.
- **Scrubs paths.** Error messages a page sees have local paths replaced by the dataset
  title.
- **Answers CORS only for allowed origins.** Replies carry CORS headers only for origins in
  `browser_origins`, so other websites cannot read even `hello`.

Tokens are kept in memory only, so restarting the daemon unpairs every open schematic; the
page notices (401), forgets its token and offers to connect again.

Known limitation (Windows): a second daemon whose `port` is `0` can start beside a running
one, since it binds a fresh loopback port and there is no socket to collide on, and then
overwrites the first one's `local-token` and runtime `daemon.json`, cutting local clients
off from the first. Run one daemon per user (with the default port, a second one fails to
bind).

Known limitation: the pairing rate limit is global (origins can't be told apart when every
`file://` page is `null`), so a page that floods `/v1/pair` can delay real pairing for about
a minute. It cannot pair without your approval.

Browsers' private/local network access rules for loopback requests (Chrome's Local Network
Access, Safari's and Firefox's equivalents) are still changing. The daemon answers
`Access-Control-Request-Private-Network` preflights, but a browser may still ask you for
permission the first time a schematic connects.

## Protocol (API v1)

HTTP/1.1, one request per connection, JSON bodies with `Content-Length` (no chunked
bodies). Daemon refusals are `{"daemon_error": {"code", "message"}}` with a 4xx/5xx status.

### Any caller

| Route | Body → reply |
|---|---|
| `GET /v1/hello` | → `{"service":"qsv-clientd","api":1}` |
| `POST /v1/pair` | `{"schematic_uuid"?, "digest"?}` → `{"request_id","code","approve_with"}` |
| `GET /v1/pair/<request_id>` | → `{"status":"pending"\|"denied"\|"unknown"}` or `{"status":"approved","token","expires_in","dataset"}` (the token is returned once) |

### Paired browser (`Authorization: Bearer <token>`) or local

| Route | Body → reply |
|---|---|
| `GET /v1/datasets/<id>` | → `{"id","title","pid","can_fetch","can_render","fingerprint","fetched"}`; `fetched` is true while the dataset reads a fetched copy (see below) |
| `POST /v1/datasets/<id>/rows` | `{"column"?, "value"?, "offset"=0, "limit"=100 (≤500)}` → `{"headers","rows","offset","result"?}`; with `value`, the rows whose `column` equals it exactly, also kept as a stored result (below) |
| `POST /v1/datasets/<id>/refresh` | `{"stats"?: bool}` → `{"changed","fingerprint","rows","stats"?}`; `changed` compares with the data the schematic was rendered from |
| `POST /v1/datasets/<id>/query` | `{"command","args"}` → `{"stdout","truncated","bytes","headers"?,"rows"?}`; `headers` and `rows` are the parsed output of commands that print CSV (not `count`, `headers`, `table`, or `--json`/`--toon`), without the last, partial record when `truncated`; CSV over `browser_max_output` is kept as a stored result instead: the reply has `"result"` and, when they fit the cap, the first 100 rows, and `stdout` holds the capped text |
| `POST /v1/datasets/<id>/page` | `{"result", "start"=0, "length"=100 (≤500), "search"?, "order"?: {"column", "desc"}}` → `{"headers","rows","start","total","filtered"}`; 410 `result_gone` if the result was replaced, 409 `result_stale` if the data changed |
| `POST /v1/datasets/<id>/render` | `{}` → the new schematic HTML (`text/html`); a local caller gets `{"schematic_path","bytes"}` instead. It is also written over the registered schematic file |
| `POST /v1/datasets/<id>/fetch` | `{}` → `{"changed","fingerprint","via"}`; follow with `render` |
| `POST /v1/datasets/<id>/disconnect` | `{}` → `{"revoked": 1}`; withdraws the calling page's own token and deletes its stored result (`0` from a local caller, which has no token) |

### Stored results

A query result too big to send whole (CSV over `browser_max_output`), and every filtered
drill-down, is kept on disk so the page can read any page of it. `result` is
`{"id","total","headers"}`; `/page` reads `length` rows from `start`, after an optional
`search` (case-insensitive, matching any column) and `order` (natural order on one 0-based
column). qsv does the work on the stored file: `index` it, `slice` pages, `search` and `sort`
build the searched and ordered view, rebuilt only when those change.

Each paired page (and the local user) has at most one, in `results/` under the daemon's
state directory, never beside your data. The next query or drill-down replaces it, even one that
stores nothing. It is removed when its page disconnects or its token is revoked, within five
minutes of the token expiring, when the dataset is forgotten, and when the daemon stops or
starts. A result whose page disconnected, or whose dataset was forgotten, while qsv was
still producing it is discarded (410). It goes stale (409) when the file it came from changes
size or modification time, as with an edit or a fetch with `--allow-replace`. A stored file over `browser_max_result` (default 1 GiB) is deleted and refused with
413.

### Local only

| Route | Body → reply |
|---|---|
| `GET /v1/status` | → daemon, qsv and job information |
| `POST /v1/run` | `{"command","args","cwd"?,"env"?,"stdin_b64"?,"stdout_path"?,"timeout"?}` → `{"result":{"args","exit_code","stdout_b64","stderr","duration","stdout_path"}}` or `{"error": <QsvError.to_dict()>}` (timeouts, version errors) or `{"os_error": {"errno","strerror","filename"}}`. The run itself uses `check=False`; the client applies `check` |
| `GET /v1/datasets`, `POST /v1/datasets`, `DELETE /v1/datasets/<id>` | list / register (`path`, `viz_args`, `schematic_path`, `title`, `pid`, `source_url`, `allow_replace`, `force`, `render`) / forget |
| `GET /v1/pairings`, `POST /v1/pairings/<code>/approve\|deny` | pending pairings (`code`, `origin`, `dataset`, `approved`, `denied`, `expires_in` seconds) / decide |
| `POST /v1/tokens/revoke` | `{"dataset_id"?}` → `{"revoked": n}` |

### What a schematic embeds

`qsv viz smart` writes the following JSON into the page, and the daemon matches the page to
a registered dataset by it:

```html
<script type="application/json" id="qsv-schematic-source">{"schema":1,"qsv_version":"26.0.0",
  "schematic_uuid":"…","fingerprint":{"algo":"blake3","digest":"…","bytes":123},
  "rows":4,"viz_args":["--dict-info"],"redacted":[],"pid":"https://doi.org/…"}</script>
```

The page pairs with `schematic_uuid`, falling back to `fingerprint.digest`. A
`fingerprint` is `{"algo","digest","bytes"}`. The daemon fingerprints data with BLAKE3,
computed by `qsv blake3` (the standard library has no BLAKE3), or with SHA-256 when its qsv
has no `blake3` command (qsvlite). A schematic rendered with `--no-daemon-bridge` has no
fingerprint.

`viz_args` has the input and output paths removed; `register --schematic` offers it for
re-rendering. Flags whose value is a file keep only its basename and are listed in
`"redacted"`; registering then asks you to pass the full arguments after `--`.

## Fetching the latest data

`fetch` downloads from the registered `--source-url`, or else from the PID.

- **qsv's `get` (qsv 26+).** When qsv has `get`, it downloads http(s), `ckan://`,
  `dathere://` and cloud URLs, with conditional requests.
- **urllib fallback.** Otherwise plain http(s) is downloaded with urllib.
- **DOIs.** A DOI is resolved through DataCite content negotiation, looking for a
  `contentUrl`. Most DOIs resolve to a landing page instead, so register `--source-url` for
  those.

A download is limited to `fetch_max_bytes` (10 GiB by default) and `fetch_timeout` (30
minutes, for the whole download, headers included; looking up the host's address is bounded
by the operating system's resolver instead). Over either, the fetch is refused (413
`too_large` or 504 `download_timeout`) and your data is left as it was. The urllib fallback
stops as soon as a download passes a limit and deletes what it got. qsv's `get` downloads
into qsv's own cache first, so there the size is checked once the download is done: the
daemon's copy is deleted, but the one in qsv's cache stays until qsv evicts or replaces it.
The time limit applies to each `get` run.

The download is used only if it has every column the current data has; new columns are fine.
That keeps an error page served as plain text from replacing your data. If columns were
renamed or dropped on purpose, run `qsv-clientd fetch <id> --force` (a page can't).

With `--allow-replace`, the download replaces your file. Without it, your file is left alone
and the dataset switches to the downloaded copy, kept in the daemon's cache under your file's
name: from then on refresh, rows, queries and re-renders read that copy, so later edits to
your file don't show. The page shows when it is reading a fetched copy, and `qsv-clientd
datasets` lists both `path` (the copy) and `original_path` (your file). To go back to your
file, run `qsv-clientd register <file>` again, then re-render.
