# Sift

**Find the clutter. Keep the code.**

Sift finds regeneratable development artifacts — `target/`, `node_modules/`, `__pycache__/`,
build trees, caches, virtual environments — tells you how much disk they use, and helps you
delete them safely.

It is not a general disk cleaner. It only looks for things your tools can rebuild, it never
decides to delete anything on its own, and it refuses when it is not sure what something is.

Sift is local-first: nothing is uploaded anywhere, and the web dashboard only listens on
your own machine.

> **Status: alpha, unreleased.** Version 0.1.0 has not been published to PyPI and there are
> no release builds. Install from source (below). Deletion is permanent, so please read
> [Safety](#safety) before using `sift clean`.

## Contents

- [Install](#install)
- [Quick start](#quick-start)
- [What it detects](#what-it-detects)
- [Command line](#command-line)
- [Terminal interface](#terminal-interface)
- [Web dashboard](#web-dashboard)
- [Configuration](#configuration)
- [Sizes: in files vs on disk](#sizes-in-files-vs-on-disk)
- [Safety](#safety)
- [Performance](#performance)
- [Implemented and planned](#implemented-and-planned)
- [Development](#development)

## Install

Requires Python 3.12 or newer. Building the dashboard also needs Node.js 22 or newer.

```bash
git clone https://github.com/AndyFerns/sift
cd sift
python -m venv .venv
```

Activate the environment (`.venv\Scripts\activate` on Windows, `source .venv/bin/activate`
elsewhere), then:

```bash
python -m pip install -e .
```

That gives you the `sift` command with the CLI and the terminal interface. The web dashboard
is a separate build step, because its compiled files are not kept in git:

```bash
cd web
npm install
npm run build
```

Without that step `sift serve` still runs the API and says the dashboard has not been built.

## Quick start

```bash
sift scan ~/code/my-project              # what is here, and how big is it?
sift scan ~/code --recursive             # every project under a folder
sift clean ~/code/my-project --dry-run   # what would be deleted? (deletes nothing)
sift clean ~/code/my-project --interactive
```

`sift scan` is read-only. `sift clean --interactive` shows a numbered list, asks which
artifacts you want, shows exactly what will be deleted, and only proceeds when you type
`delete`. Pressing Enter at either prompt backs out.

## What it detects

Sift never classifies something from its name alone. A directory called `build` could be
anything. Each rule looks for evidence:

- a **manifest** — a file next to the artifact (`Cargo.toml` beside `target/`)
- a **marker** — something the tool always writes inside it (`pyvenv.cfg` in `.venv/`)

| Ecosystem | Artifacts | Evidence looked for |
|---|---|---|
| Rust | `target/` | `Cargo.toml`; `CACHEDIR.TAG` or `.rustc_info.json` inside |
| Node.js | `node_modules/`, `.next/`, `.nuxt/`, `.turbo/`, `.angular/cache/` | `package.json` and framework config files; lockfile markers inside `node_modules` |
| Python | `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `.tox/`, `.nox/`, `.venv/`, `venv/` | Python project files; `CACHEDIR.TAG`; `pyvenv.cfg` (required for virtual environments); bytecode-only contents for `__pycache__` |
| Java | Maven `target/`, Gradle `build/`, `.gradle/` | `pom.xml`; `build.gradle[.kts]` / `settings.gradle[.kts]`; typical output folders inside |
| C/C++ | `build/`, `cmake-build-debug/`, `cmake-build-release/`, `CMakeFiles/` | `CMakeLists.txt`; `CMakeCache.txt` and friends inside |
| Go | directories you name in the config | `go.mod` next to them |
| General | `coverage/`, `.coverage`, `dist/` | project manifests; `lcov.info` and similar; built wheels/sdists |
| Custom | whatever you define in the config | the manifests and markers you list |

Go has no standard in-project build directory, so the Go detector does nothing until you
tell it which directory names to treat as build output (see [Configuration](#configuration)).

### Confidence and ambiguity

| Evidence found | Result |
|---|---|
| manifest **and** marker | **high** |
| a tool-reserved name (like `node_modules`) plus a manifest or a marker | **high** |
| marker only, or manifest only | **medium** for most rules; **low** for the generic names `build/` (CMake), `dist/` and `coverage/` |
| nothing but the name | **low** and **ambiguous** |

An artifact is **cleanable** only when it is not ambiguous and its confidence meets the
threshold (`medium` by default). Everything else is still listed, so you can see it, but
cannot be selected for cleanup.

**Ambiguous** means the evidence is missing or contradictory — for example `target/` in a
folder that has both `Cargo.toml` and `pom.xml` and nothing inside to say which tool made
it. Ambiguous artifacts are marked `?` and can never be cleaned, whatever the threshold.

One deliberate choice: `dist/` next to a `package.json` with nothing else to go on is **low**
confidence. Some projects commit `dist/` on purpose, so Sift will not offer to delete it
unless you lower the threshold yourself.

Sift treats an artifact as opaque. It does not look inside one for more artifacts, so a
`node_modules` inside `node_modules` is part of its parent, not a second entry.

## Command line

```text
sift scan PATH [--recursive] [--format table|json|csv] [--depth N] [--exclude PATTERN]...
               [--min-confidence high|medium|low] [--time-budget SECONDS] [--no-size]
               [--output FILE] [--no-history]
sift inspect ARTIFACT_ID [--root PATH] [--format table|json]
sift clean PATH [--dry-run] [--interactive] [--recursive] [--depth N] [--exclude PATTERN]...
sift config show [--format toml|json]
sift config init [--force]
sift config path
sift tui [PATH] [--recursive]
sift serve [--root PATH]... [--host HOST] [--port PORT] [--open]
```

**Scan modes.** By default `PATH` is treated as one project: Sift looks everywhere inside it
but does not go into nested repositories (folders below `PATH` that have their own `.git`);
it lists them as skipped. `--recursive` scans every project under `PATH`, nested
repositories included. Symlinks and Windows junctions are never followed in either mode.

**`sift inspect`** takes the ID shown by `sift scan` and re-examines that artifact on disk:
whether it still exists, its current size, the largest entries inside, the evidence, and
whether a cleanup would be allowed. IDs come from scan history; with history turned off,
pass `--root PATH` to scan first.

**`sift clean`** never deletes without `--interactive`. With no flags, or with `--dry-run`,
it shows what a cleanup of every cleanable artifact would do and stops.

**Exit codes**

| Code | Meaning |
|---|---|
| 0 | Success, including dry runs and cleanups you cancelled |
| 1 | An error, or part of a cleanup could not be deleted |
| 2 | Bad usage: unknown option, missing path, invalid config file |
| 3 | A cleanup was refused for safety (and nothing in it failed) |

**Reports.** `--format json` and `--format csv` write machine-readable output to stdout (or
to `--output FILE`); progress messages go to stderr, and none are printed when the output
is piped. The JSON is the complete scan result. Cleanup output always keeps four numbers
apart: **detected**, **selected**, **deleted** and **failed** (plus **refused**).

## Terminal interface

```bash
sift tui ~/code --recursive
```

A full-screen interface with a project list, a sortable and filterable artifact table, a
detail pane, and the same preview-then-confirm cleanup flow.

| Key | Action |
|---|---|
| Up / Down, PgUp / PgDn | move through artifacts |
| Tab / Shift+Tab | switch between projects, filter and table |
| Enter | inspect the highlighted artifact |
| `/` | filter by path, type or ecosystem (Esc clears) |
| `s` / `o` | change sort column / reverse order |
| `e` | cycle the ecosystem filter |
| `c` | show only cleanable artifacts |
| Space | select or unselect (only cleanable artifacts can be selected) |
| `a` / `n` | select all cleanable artifacts shown / clear the selection |
| `d` | preview a cleanup of the selection |
| `r` | scan again |
| `?` | help |
| `q` | quit |

## Web dashboard

```bash
sift serve --root ~/code
```

Then open <http://127.0.0.1:8765>. The dashboard has an overview (totals and size by
ecosystem), a scan page, an artifact explorer with search, filters and size bars, scan and
cleanup history, settings, and a cleanup preview with typed confirmation. Every number on
it comes from a real scan through the API; there is no sample data.

The dashboard may only scan and clean inside **scan roots**: directories listed under
`[scan] roots` in the config file, plus any given with `--root`. Roots cannot be changed
from the browser.

It binds to loopback addresses only and has no remote mode. See [Safety](#safety).

The API is documented at `/api/docs` while the server is running. Endpoints:

| Method and path | Purpose |
|---|---|
| `GET /api/health` | liveness and version |
| `GET /api/config`, `PUT /api/config` | settings (roots are read-only) |
| `POST /api/scans` | scan a directory inside a root |
| `GET /api/scans`, `/api/scans/latest`, `/api/scans/{id}` | stored scans |
| `GET /api/scans/{id}/artifacts/{artifact_id}` | one artifact, re-checked on disk |
| `GET /api/scans/{id}/report?format=json\|csv` | export |
| `POST /api/cleanup/preview` | build a preview; deletes nothing |
| `POST /api/cleanup/execute` | run a preview (needs its token and the typed word) |
| `POST /api/cleanup/cancel` | record that the user backed out |
| `GET /api/history`, `GET /api/cleanups/{id}` | scan and cleanup history |

## Configuration

Sift reads `~/.config/sift/config.toml` on every platform. The file is optional. Create a
commented starter with `sift config init`, and see what is in effect with `sift config show`.

```toml
[scan]
roots = ["~/code"]            # where the dashboard may scan and clean
exclude = ["vendor/"]         # gitignore-style, relative to the scanned directory
# max_depth = 8               # levels below the scan root; unset means no limit
min_confidence = "medium"     # high | medium | low
# time_budget_seconds = 3.0   # stop measuring after this long

[detectors]
enabled = ["rust", "node", "python", "java", "cpp", "go", "general"]

[detectors.go]
artifact_dirs = ["bin"]       # only matched next to a go.mod

[[detectors.custom]]
name = "terraform"
description = "Terraform provider cache"
dir_names = [".terraform"]
manifests = ["main.tf"]       # without a manifest or marker a custom rule is never cleanable

[history]
enabled = true                # past scans and cleanup audits, in a local SQLite file
max_entries = 100

[server]
host = "127.0.0.1"            # loopback addresses only
port = 8765
```

A setting Sift does not recognise is an error, not something it silently ignores: a typo in
a threshold or an exclusion should never quietly change what can be deleted.

**`.siftignore`.** Put a `.siftignore` file in any directory to exclude things below it,
using gitignore syntax:

```gitignore
# never look in here
third_party/
*.generated/
```

**Where things are stored.** History lives in your platform's user data directory
(`sift/history.db`). Set `SIFT_CONFIG_DIR` or `SIFT_DATA_DIR` to move the config or the data.
Scanning writes nothing inside the directories it scans.

## Sizes: in files vs on disk

Sift reports two sizes because they answer different questions.

- **Size (in files)** — the sum of the files' lengths. This is what the files contain.
- **On disk** — the space those files occupy. Filesystems store files in whole blocks, so
  ten thousand tiny files take far more room than their contents add up to. This is the
  better guide to what deleting an artifact frees.

On Linux and macOS the on-disk size comes from the filesystem (`st_blocks`) and files with
several hard links are counted once. On Windows that would cost an extra system call per
file, so Sift rounds each file up to the volume's cluster size and marks the result as an
estimate (`~`). Estimates do not account for compression, sparse files, or very small files
stored inside the file table, and on Windows hard-linked files (pnpm stores, for example)
are counted at full size.

If part of an artifact cannot be read, its size is shown as a lower bound (`>=`). If a time
budget runs out before an artifact is measured it is shown as **not measured** — Sift does
not report a partial count as if it were the size.

Cleanup totals (selected, deleted, failed) are sizes in files.

## Safety

Deletion is permanent. Sift does not use a recycle bin and cannot restore anything.

- **Nothing is deleted automatically.** Every cleanup needs an explicit selection, a preview
  listing exactly what will be removed, and a typed confirmation. There is no flag to skip
  any of these, and the core refuses to delete without the confirmation token issued for
  that exact preview.
- **You can always back out.** Cancel is the default at every prompt: Enter, Escape, Ctrl+C
  or anything other than the word `delete` leaves everything in place.
- **Paths are checked twice** — when the preview is built, and again immediately before each
  deletion. A target is refused if it is outside the scan root, is the scan root itself, a
  filesystem root or your home directory (or contains it), is a symlink or junction or is
  reached through one, contains a `.git` entry, is ambiguous or below the confidence
  threshold, no longer matches the rule it had in the preview, or is not the same directory
  that was previewed.
- **Links are never followed**, when scanning or when deleting. A link inside an artifact is
  removed as a link; what it points to is left alone.
- **Nothing found in a project is ever run.** Sift reads directory listings and file
  metadata, and at most a few bytes of specific files to confirm what they are.
- **Every cleanup is recorded** in local history, including dry runs and cancellations.

The dashboard binds only to loopback addresses, rejects requests whose `Host` header is not
a loopback name, rejects cross-site state-changing requests, sends no CORS headers, forbids
framing, and only accepts paths under the configured scan roots.

[SECURITY.md](SECURITY.md) has the full model, its known limits, and how to report a problem.

## Performance

Sift scans in two phases. **Discovery** walks your source directories and stops at the edge
of every artifact, so it never reads inside `node_modules` or `target`. **Sizing** then has
to visit every file in each artifact; there is no shortcut for that. Small scans are sized
on two threads. If sizing is still running after a quarter of a second, helper processes
join in so the work can use more than one CPU core.

Measured on the development machine (Windows 11, NTFS, 16 logical CPUs) with
`scripts/bench.py`, which builds synthetic Node-style projects:

| Tree | Total scan time |
|---|---|
| the test fixture: about 160 files in 35 artifacts | about 50 ms |
| 60,000 files in 3,000 directories, warm cache | 0.3 – 1.2 s |
| 300,000 files in 25,000 directories, warm cache | 1.8 – 2.2 s |
| the same 300,000 files with helper processes turned off | about 7.8 s |
| the same 300,000 files, first scan after creating them | about 14 s |

Timings on that machine varied several-fold between runs of the same scan, which is why
ranges are given. A real-world check, measuring a Windows system directory with 164,000
files spread over 170,000 directories, took about 12 seconds: that tree is dominated by the
cost of opening directories, which Sift cannot avoid.

So: scans of small and medium trees finish in milliseconds to a couple of seconds once the
operating system has the directory entries cached. A cold cache, a slow disk, antivirus
scanning, or a tree with a very large number of directories will take longer, and Sift
cannot promise a fixed upper bound. If you would rather have a fast answer than a complete
one, `--time-budget SECONDS` stops sizing early and marks the rest as not measured, and
`--no-size` skips sizing altogether.

These numbers are from one machine. Run `python scripts/bench.py` to measure yours.

## Implemented and planned

**Implemented**

- Evidence-based detectors for Rust, Node.js, Python, Java, C/C++, Go (configured), general
  and custom rules, with confidence levels and ambiguity
- Read-only scanner: single-project and recursive modes, depth limit, config and
  `.siftignore` exclusions, nested repositories, unreadable and disappearing paths, links
  never followed, long Windows paths
- Logical and allocated sizes, file counts, largest-first ordering, optional time budget
- Safe cleanup: selection, preview, typed confirmation, re-validation, dry run, audit
- CLI: `scan`, `inspect`, `clean`, `config`, `tui`, `serve`; JSON, CSV and terminal reports
- Terminal interface (Textual)
- Local API (FastAPI) and web dashboard (React)
- Optional local history (SQLite)

**Tested, but not on every platform yet**

The test suite runs real filesystem operations in temporary directories. It has been run on
Windows. Tests that need symbolic links, POSIX permissions or hard-link accounting are
skipped there; they are written to run on Linux and macOS in CI, and the included GitHub
Actions workflow covers all three systems, but that workflow has not run yet because the
repository has not been pushed. On Windows, junctions and simulated permission errors
stand in for those cases.

**Planned, not implemented**

- A published package on PyPI and release builds
- Using `.gitignore` as extra evidence that a directory is generated
- Global caches outside projects (`~/.cargo`, the npm cache, Gradle's user home)
- More ecosystems (.NET, Swift, Elixir, PHP and others)
- Non-interactive cleanup for scripts — deliberately left out until there is a design that
  keeps the same guarantees

## Development

```bash
python -m pip install -e ".[dev]"
pytest
ruff check .
ruff format --check .
mypy
```

and for the dashboard, in `web/`:

```bash
npm install
npm run typecheck
npm test
npm run build
```

`npm run dev` serves the dashboard with hot reload and proxies API calls to a running
`sift serve`.

How the pieces fit:

```text
src/sift/core/   scanning, detection, sizing, safety, cleanup, config, history, reports
src/sift/cli/    Typer + Rich command line
src/sift/tui/    Textual interface
src/sift/api/    FastAPI app; serves the built dashboard from api/static/
web/             React + TypeScript + Vite dashboard
tests/           pytest suite and the shared fixture workspace
scripts/         benchmark, fixture builder, API type generator
```

Every interface goes through `sift.core.service.SiftService`; none of them scans or deletes
on its own. One test (`tests/test_consistency.py`) runs the same fixture through the core,
the CLI, the API and the terminal interface and requires identical results. The dashboard's
TypeScript types are generated from the API schema (`python scripts/gen_api_types.py`), and
a test fails if they are out of date.

`python scripts/make_fixture.py SOME_EMPTY_DIR` creates the fixture workspace somewhere you
can try Sift on by hand.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the ground rules.

## License

[MIT](LICENSE)
