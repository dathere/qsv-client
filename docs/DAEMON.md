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
`browser_max_render`, `token_ttl`, `allow_fetch`. Unknown keys are an error.
`QSV_CLIENTD_HOME` puts every file under one directory (handy for tests).

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
origin the request came from. The page then holds a token for that one dataset for 8 hours
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
  out once, and is stored only as a hash.
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
| `GET /v1/datasets/<id>` | → `{"id","title","pid","can_fetch","can_render","fingerprint"}` |
| `POST /v1/datasets/<id>/rows` | `{"column"?, "value"?, "offset"=0, "limit"=100 (≤500)}` → `{"headers","rows","offset"}`; with `value`, the rows whose `column` equals it exactly |
| `POST /v1/datasets/<id>/refresh` | `{"stats"?: bool}` → `{"changed","fingerprint","rows","stats"?}`; `changed` compares with the data the schematic was rendered from |
| `POST /v1/datasets/<id>/query` | `{"command","args"}` → `{"stdout","truncated","bytes"}` |
| `POST /v1/datasets/<id>/render` | `{}` → the new schematic HTML (`text/html`); a local caller gets `{"schematic_path","bytes"}` instead. It is also written over the registered schematic file |
| `POST /v1/datasets/<id>/fetch` | `{}` → `{"changed","fingerprint","via"}`; follow with `render` |

### Local only

| Route | Body → reply |
|---|---|
| `GET /v1/status` | → daemon, qsv and job information |
| `POST /v1/run` | `{"command","args","cwd"?,"env"?,"stdin_b64"?,"stdout_path"?,"timeout"?}` → `{"result":{"args","exit_code","stdout_b64","stderr","duration","stdout_path"}}` or `{"error": <QsvError.to_dict()>}` (timeouts, version errors) or `{"os_error": {"errno","strerror","filename"}}`. The run itself uses `check=False`; the client applies `check` |
| `GET /v1/datasets`, `POST /v1/datasets`, `DELETE /v1/datasets/<id>` | list / register (`path`, `viz_args`, `schematic_path`, `title`, `pid`, `source_url`, `allow_replace`, `force`, `render`) / forget |
| `GET /v1/pairings`, `POST /v1/pairings/<code>/approve\|deny` | pending pairings / decide |
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

The download is used only if it has every column the current data has; new columns are fine.
That keeps an error page served as plain text from replacing your data. If columns were
renamed or dropped on purpose, run `qsv-clientd fetch <id> --force` (a page can't). The
download is kept in the daemon's cache and does not overwrite your file, unless you
registered with `--allow-replace`.
