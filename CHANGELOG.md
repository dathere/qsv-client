# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.6.0] - 2026-10-26

This release accompanies qsv 27.0.0, the first qsv whose Data Schematics connect to
`qsv-clientd`. `qsv-clientd viz <URL>` turns a download link or a CKAN page into a live
schematic in one step, and a schematic remembers where its data came from, so Fetch latest
can get new data.

### Added

- **`qsv-clientd viz <URL>`** ([#34](https://github.com/dathere/qsv-client/pull/34)) downloads the data first, then renders, registers and opens
  the schematic in one step. The URL can be an http(s) download link, `ckan://<id>`, or a
  CKAN resource page copied from the browser. It is also where Fetch latest gets new data.
  The data is saved beside the schematic (`--save-as` to choose), readable only by you
  since it may be private, never replacing an existing file. `-o` now defaults to
  the data's name with `.html`. `--no-open` skips the browser.
- **CKAN pages as sources.** ([#34](https://github.com/dathere/qsv-client/pull/34)) A CKAN resource page (`<site>/dataset/<name>/resource/<id>`) is
  fetched as `ckan://<id>` from that site. A CKAN dataset page with exactly one CSV or TSV
  resource, found with `package_show`, is fetched as that resource; with several, the
  error lists them. A private dataset's page is refused with a message naming the fix
  (its resource page, with `qsv get` and `QSV_CKAN_TOKEN`).
- **`--ckan-api`** ([#34](https://github.com/dathere/qsv-client/pull/34)) on `register` and `viz` names a dataset's CKAN site (or its Action API
  URL) for `ckan://` sources.
- `register --schematic` takes the source URL (and its CKAN site) from a schematic that
  records one, as qsv records it for data downloaded with `qsv get`. ([#34](https://github.com/dathere/qsv-client/pull/34))

### Changed

- The docs now name the release that brings live Data Schematics: qsv 27.0.0 or later
  (before, "a qsv newer than 26.0.0"). Other uses still need only qsv 25.0.0.

### Fixed

- `ckan://` sources were resolved against qsv's default CKAN site (data.dathere.com) unless
  `QSV_CKAN_API` happened to be in the daemon's environment. They now use the dataset's own
  CKAN site, then `QSV_CKAN_API`, and are refused when neither is set. ([#34](https://github.com/dathere/qsv-client/pull/34))
- A source that serves a web page (a landing page, a sign-in form) is refused with "serves
  a web page, not data", naming the PID or source URL. Before, a download through `qsv get`
  kept the page and failed with a CSV parse error. ([#34](https://github.com/dathere/qsv-client/pull/34))
- Downloads without `qsv get` (the prebuilt qsv has none) failed with 403 on CKAN sites
  that store data in S3. Their download links redirect to a presigned URL written with
  `:443`, and urllib sent that port in `Host`, which breaks the URL's signature. The default
  port is now dropped first. ([#34](https://github.com/dathere/qsv-client/pull/34))
- `ckan://` sources, including CKAN resource pages, now work without `qsv get`: the daemon
  looks the resource up with `resource_show` and downloads its data itself. Private
  resources still need `qsv get` and `QSV_CKAN_TOKEN`. ([#34](https://github.com/dathere/qsv-client/pull/34))
- A daemon re-render dropped the schematic's `source`: it renders from the daemon's own copy
  of the data, for which qsv records none. The new page now keeps the previous page's
  `source` while it still names the dataset's source URL. The daemon never adds one itself,
  since only qsv knows whether a download was public. A page rendered from other data (an
  existing `-o` file, or one registered with `--force`) lends the dataset no source and no
  PID. Neither does a BLAKE3 page that a qsv without `blake3` can't check; `--force` registers
  it again, as before.
  ([#35](https://github.com/dathere/qsv-client/pull/35))
- `register` printed the schematic's viz arguments, and its "Use these arguments?"
  question, on stdout before the registration's JSON. They are on stderr now, so stdout can
  be piped to `jq`. So is `approve`'s question. ([#35](https://github.com/dathere/qsv-client/pull/35))
- The `fingerprint_mismatch` refusal said "pass force"; it now says to register with
  `--force`. ([#35](https://github.com/dathere/qsv-client/pull/35))

## [0.5.0] - 2026-10-21

This release adds `qsv-clientd`, a per-user daemon that runs qsv for your own processes and
lets a Data Schematic from `qsv viz smart` work with its live data, and `qsv-clientd-tray`, a
system tray icon that runs and controls it.

### Added

- **`qsv-clientd`**, a per-user daemon that runs qsv for local processes and for Data
  Schematics. It uses only the standard library. See `docs/DAEMON.md`.
  ([#12](https://github.com/dathere/qsv-client/pull/12), [#13](https://github.com/dathere/qsv-client/pull/13), [#14](https://github.com/dathere/qsv-client/pull/14), [#15](https://github.com/dathere/qsv-client/pull/15), [#16](https://github.com/dathere/qsv-client/pull/16), [#18](https://github.com/dathere/qsv-client/pull/18), [#19](https://github.com/dathere/qsv-client/pull/19), [#24](https://github.com/dathere/qsv-client/pull/24), [#25](https://github.com/dathere/qsv-client/pull/25))
  - **Local clients.** Local processes connect over a Unix socket, or on Windows over
    loopback with a per-user token. `DaemonClient` and `AsyncDaemonClient` take the same
    `run()` arguments as `Qsv` and raise the same errors, including `QsvTimeout` when no job
    slot frees up within the timeout and the same `OSError` for an unwritable `stdout_path`.
    `qsv-clientd run` forwards stdin only with `--stdin` or a `-` argument.
  - **Live Data Schematics.** A schematic from a qsv newer than 26.0.0 offers to connect.
    It pairs once you approve its code with `qsv-clientd approve`, which shows the dataset and
    the request's origin. A paired schematic can drill into the rows behind a chart, refresh
    its statistics, re-render itself, run allowlisted read-only queries and fetch the latest
    data. Its Disconnect withdraws the token. `GET /v1/pairings` items include `expires_in`,
    the seconds left before the code expires.
  - **Large results are paged.** A query result over `browser_max_output` (4 MiB), and the
    rows behind a chart, are kept by the daemon and read a page at a time. qsv runs each page,
    search and column sort on the stored file, so every row is reachable.
    `browser_max_result` (default 1 GiB) caps a stored result.
  - **Fetching the latest data.** The daemon downloads from a registered source URL or PID,
    with qsv's `get` where available. It uses the download only if every record parses and
    every current column is still there; `qsv-clientd fetch --force` accepts renamed or
    dropped columns. Unless the dataset was registered with `--allow-replace`, the download
    is kept in the daemon's cache under your file's name and your file is left alone. The
    schematic and `qsv-clientd datasets` show when the dataset reads that copy.
    `fetch_max_bytes` (default 10 GiB) and `fetch_timeout` (default 30 minutes) limit how
    much a fetch may download and for how long.
  - **Fingerprints.** Data is fingerprinted with BLAKE3 through `qsv blake3`, or with
    SHA-256 when the binary has no `blake3` (qsvlite).
  - **Browser security.** The browser tier:
    - checks `Host` and restricts and rate-limits pairing
    - stores tokens only as hashes
    - never takes a path from a page, and allowlists commands and their flags
    - caps output and bounds connections
    - removes local paths from error messages
  - **Logging.** `qsv-clientd serve --log-file PATH` writes the log to a file too (rotated at
    5 MB, 3 kept), and `-v` logs pairing requests and decisions, datasets registered and
    forgotten, renders, fetches and revoked tokens. Tokens and pairing codes are never logged.
  - `qsv_client.daemon.host.DaemonHost` runs the daemon on a background thread, for programs
    that need the main thread themselves.
- **`qsv-clientd-tray`**, a system tray icon that runs and controls qsv-clientd, in the
  optional `tray` extra (`pip install 'qsv-client[tray]'`, which adds pystray and Pillow).
  ([#26](https://github.com/dathere/qsv-client/pull/26), [#29](https://github.com/dathere/qsv-client/pull/29), [#30](https://github.com/dathere/qsv-client/pull/30), [#31](https://github.com/dathere/qsv-client/pull/31), [#33](https://github.com/dathere/qsv-client/pull/33))
  - It hosts the daemon itself, or attaches to one already running, and starts, stops and
    restarts it.
  - It announces pairing requests with a desktop notification and lets you approve or deny
    them from its menu, with the same checks as `qsv-clientd approve`. Approving means
    picking the code the page shows from four; a wrong pick denies the request.
  - Its Datasets menu opens, re-renders, fetches and forgets registered datasets, and it can
    revoke all tokens.
  - It opens `daemon.json` and its log, and starts at login on macOS, Linux and Windows (a
    menu check box, or `qsv-clientd-tray --enable-autostart`).
  - On Windows, the qsv commands it runs open no console windows.
- `QsvError.to_dict()` and `qsv_client.errors.error_from_dict()`.
- `QsvDaemonError` and `QsvDaemonUnavailable`.

### Changed

- A `min_version` that is not a version (`"major.minor.patch"`) now raises `ValueError` when
  `Qsv` or `AsyncQsv` is created, instead of on every run. ([#15](https://github.com/dathere/qsv-client/pull/15))
- `QsvNotFound` is now a `FileNotFoundError`. A run whose binary was removed after the
  client was created raises it, instead of a bare `FileNotFoundError`; existing `OSError`
  handlers still catch it. ([#23](https://github.com/dathere/qsv-client/pull/23))
- The sdist also contains `docs/`, which holds the daemon's documentation. ([#12](https://github.com/dathere/qsv-client/pull/12))

### Fixed

- On Windows, a run that timed out or was cancelled in a process without a console, such as
  one started with `pythonw`, raised `SystemError` and left qsv running. It is killed after
  `kill_grace` now. ([#33](https://github.com/dathere/qsv-client/pull/33))
- `DaemonClient` raises `QsvDaemonError` for a garbled reply from `qsv-clientd`, as
  `AsyncDaemonClient` already did, instead of leaking `http.client` exceptions. ([#23](https://github.com/dathere/qsv-client/pull/23))
- `DaemonClient` closes its socket when it can't connect to `qsv-clientd`, so checking for a
  daemon that isn't running no longer leaks one (a `ResourceWarning`). ([#23](https://github.com/dathere/qsv-client/pull/23))
- The `schtasks` command that `qsv-clientd install` prints on Windows works when a path
  contains spaces. ([#23](https://github.com/dathere/qsv-client/pull/23))
- A Data Schematic query's option values must be strings or numbers, like its other
  arguments. ([#23](https://github.com/dathere/qsv-client/pull/23))
- `qsv-clientd run search -- -x` passes the `--` on to qsv, so a pattern or argument that
  starts with `-` works. argparse used to drop it. ([#23](https://github.com/dathere/qsv-client/pull/23))

## [0.4.0] - 2026-10-15

qsv 25.0.0 or later is now required. It is the first release whose binaries (`qsv`,
`qsvlite`, `qsvdp`, `qsvmcp`) all answer `--capabilities` and report errors as JSON, so the
code that supported older qsv is gone.

### Changed

- **Behavior change:** qsv 25.0.0 or later is now required (exported as `MIN_QSV_VERSION`).
  Before the first command, `qsv --capabilities` is now always run, even without
  `min_version=`. A qsv that is too old, or that rejects `--capabilities`, raises
  `QsvVersionError` instead of running. `min_version=` can still raise the floor, but not
  lower it. The check is bounded by the run's `timeout` (at most 60s) separately from the
  command, so a client's first run can take up to about twice its timeout. A check that fails
  for another reason, such as a crash or a startup error, raises the usual `QsvError` with
  qsv's stderr. ([#9](https://github.com/dathere/qsv-client/pull/9))
- **Behavior change:** `QsvVersionError` is now a `QsvError` (kind `version`), so
  `except QsvError` around `run()` also catches a too-old binary. An `except QsvVersionError`
  placed after `except QsvError` is no longer reached, so put it first. Generic `QsvError`
  handlers now see version errors too: retry logic should not retry them, since a too-old
  qsv will never succeed. Its `exit_code`, `args_run` and `stderr` describe the
  `--capabilities` check. ([#10](https://github.com/dathere/qsv-client/pull/10))
- `capabilities` is probed once per binary per Python process and shared by every `Qsv` and
  `AsyncQsv`, so creating a client per call doesn't start an extra qsv each time. Concurrent
  first calls from threads, or from coroutines on the same event loop, share one probe; a
  sync and an async client starting at the same moment can each probe once. A binary
  replaced in place (new size or modification time) is probed again. Each client still
  applies its own `min_version` and gets its own copy of the result, so modifying
  `Capabilities.raw` or `feature_versions` affects only that client. Values in `raw` that
  depend on the environment, such as `max_jobs`, come from the client that probed first.
  ([#9](https://github.com/dathere/qsv-client/pull/9), [#10](https://github.com/dathere/qsv-client/pull/10))

### Removed

- The fallback that parsed `--version` and `--list` when `--capabilities` was unavailable,
  including `qsv_client.capabilities.from_version_output`. If `--capabilities` succeeds but
  its output can't be used, `capabilities` raises `QsvError`.
  ([#9](https://github.com/dathere/qsv-client/pull/9))
- `Capabilities.supports_json_errors` and `Capabilities.error_formats`: every supported qsv
  reports JSON errors. The value is still in `raw["error_formats"]`.
  ([#10](https://github.com/dathere/qsv-client/pull/10))
- `Capabilities.raw` is no longer optional; it is always the parsed `--capabilities` document.
  `Capabilities` fields are now keyword-only. ([#10](https://github.com/dathere/qsv-client/pull/10))

## [0.3.0] - 2026-10-10

### Changed

- **Behavior change:** a relative `stdout_path` is now resolved against the client's `cwd=`,
  like a relative `--output` path given to qsv. Previously it was resolved against Python's
  working directory, so code combining `cwd=` with a relative `stdout_path` will now write its
  output file somewhere else, without any error. Pass an absolute path to keep the old location.
  ([#7](https://github.com/dathere/qsv-client/pull/7))

### Fixed

- `AsyncQsv`: `QsvTimeout.stderr` now includes what qsv wrote before the timeout, as it
  already did for `Qsv`. Previously, stderr already read when the timeout fired was lost.
  ([#6](https://github.com/dathere/qsv-client/pull/6))
- `capabilities` (and `run` with `min_version`) raise `QsvError` instead of a bare
  `ValueError` when the binary's `--version` output isn't recognized, and fall back to `--version`
  when `--capabilities` returns JSON of an unexpected shape or without a usable version.
  ([#7](https://github.com/dathere/qsv-client/pull/7), [#8](https://github.com/dathere/qsv-client/pull/8))
- The `QsvError` docstring named the argv attribute `args`; it is `args_run`. ([#7](https://github.com/dathere/qsv-client/pull/7))
- On Windows, `find_qsv` (and `binary=` / `QSV_BIN`) accept a path without its extension,
  such as `C:/tools/qsv` for `C:/tools/qsv.exe`, trying each `PATHEXT` extension.
  ([#8](https://github.com/dathere/qsv-client/pull/8))

## [0.2.0] - 2026-10-09

### Added

- Windows support. qsv is started suspended and placed in its own Job Object before it runs,
  so a timeout, cancellation or Ctrl-C stops qsv *and* everything it started:
  `CTRL_BREAK_EVENT` first, then `TerminateJobObject` after `kill_grace`. Previously only
  `qsv.exe` itself was killed. With `inherit_env=False`, `SYSTEMROOT` is still passed on
  Windows, since many programs fail to start without it. CI runs the full test suite on
  Windows. ([#4](https://github.com/dathere/qsv-client/pull/4),
  [#5](https://github.com/dathere/qsv-client/pull/5))

### Changed

- The sdist now contains only the package, tests, README, CHANGELOG and LICENSE. 0.1.0's also
  included repository tooling files (`CLAUDE.md`, `.serena/`, `uv.lock`, `.github/`). The wheel
  is unchanged. ([#2](https://github.com/dathere/qsv-client/pull/2))
- Tested on Python 3.14 as well as 3.10–3.13. ([#3](https://github.com/dathere/qsv-client/pull/3))

### Fixed

- A timeout now raises within a few `kill_grace` periods even when a process qsv started has
  left the process group and still holds its output pipes, including when qsv ignores SIGTERM.
  Previously the run waited for that process to exit. Output it still holds is dropped, and
  `AsyncQsv` closes its pipe file descriptors instead of keeping them open until it exits.
  ([#3](https://github.com/dathere/qsv-client/pull/3))
- Cancelling an `AsyncQsv` run now always kills qsv and everything it started, even when the
  cancellation arrives during timeout cleanup or comes from `asyncio.run()` shutting down. A
  cancelled run finishes its teardown before the cancellation propagates.
  ([#3](https://github.com/dathere/qsv-client/pull/3))
- A second Ctrl-C while a sync run is waiting out `kill_grace` no longer skips the forced kill,
  which left a qsv that ignores the polite stop running. A Ctrl-C right after qsv starts is
  handled the same way. ([#5](https://github.com/dathere/qsv-client/pull/5))
- On Windows with Python 3.10–3.12, a run whose child stops reading a large `stdin=` payload
  no longer hangs past its timeout. ([#5](https://github.com/dathere/qsv-client/pull/5))
- A failed run whose stdout is not valid UTF-8 raises the typed `QsvError` instead of
  `UnicodeDecodeError`. ([#3](https://github.com/dathere/qsv-client/pull/3))
- `count()` and `headers()` work with `stdout_path=` and `text=False` instead of raising
  `IndexError`. ([#3](https://github.com/dathere/qsv-client/pull/3))
- A relative `binary` path now works together with `cwd=`. `find_qsv` returns absolute paths.
  ([#3](https://github.com/dathere/qsv-client/pull/3))

## [0.1.0] - 2026-10-08

First release. ([#1](https://github.com/dathere/qsv-client/pull/1))

### Added

- `Qsv` (sync) and `AsyncQsv` (asyncio) clients with the same API: `run(command, *args)` plus
  `count`, `headers`, `stats`, `frequency` and `describegpt` conveniences, returning a
  `QsvResult` with `json()` and `csv_rows()` helpers.
- Binary discovery: an explicit path, then `$QSV_BIN`, then `qsv`, `qsvmcp`, `qsvdp` or
  `qsvlite` on `PATH`.
- Timeouts and cancellation that stop the whole process group (SIGTERM, then SIGKILL after a
  grace period), including children qsv started itself. On Windows, qsv is killed directly.
- Typed errors: `QsvError` and its subclasses `QsvUsageError`, `QsvCsvError`, `QsvIOError`,
  `QsvNoMatch`, `QsvNetworkError`, `QsvOutOfMemory`, `QsvEncodingError`, `QsvInferenceError`,
  `QsvWriteError` and `QsvTimeout`. They are read from qsv's JSON error line
  (`QSV_ERROR_FORMAT=json`, [dathere/qsv#4765](https://github.com/dathere/qsv/pull/4765)) when the
  binary supports it, and classified by exit code otherwise. `QsvNotFound` and `QsvVersionError`
  are raised before anything runs.
- Capability detection (`Qsv().capabilities`): binary, version, features and commands, read from
  `qsv --capabilities` where available, otherwise from `--version` and `--list`. A `min_version=`
  check is also available.
- `stdout_path=` streams large output to a file instead of holding it in memory.
- `llm_api_key`, `llm_base_url` and `llm_model` are passed to `describegpt` as `QSV_LLM_*`
  environment variables, so keys never appear on the command line.
- Type hints (`py.typed`), no runtime dependencies, Python 3.10+.

[Unreleased]: https://github.com/dathere/qsv-client/compare/0.6.0...HEAD
[0.6.0]: https://github.com/dathere/qsv-client/compare/0.5.0...0.6.0
[0.5.0]: https://github.com/dathere/qsv-client/compare/0.4.0...0.5.0
[0.4.0]: https://github.com/dathere/qsv-client/compare/0.3.0...0.4.0
[0.3.0]: https://github.com/dathere/qsv-client/compare/0.2.0...0.3.0
[0.2.0]: https://github.com/dathere/qsv-client/compare/0.1.0...0.2.0
[0.1.0]: https://github.com/dathere/qsv-client/releases/tag/0.1.0
