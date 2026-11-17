# Changelog

All notable changes to Sift are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project aims to follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing has been released yet. This is the work that will become 0.1.0.

### Added

- **Detection.** Evidence-based detectors for Rust, Node.js, Python, Java (Maven and
  Gradle), C/C++ (CMake), Go (directories named in the config), general artifacts
  (`coverage/`, `.coverage`, `dist/`) and user-defined rules. Every classification carries
  a confidence level and the evidence behind it; name-only and conflicting matches are
  marked ambiguous.
- **Scanning.** Read-only scanner with single-project and recursive modes, a depth limit,
  exclusions from the config and from `.siftignore` files, nested-repository handling, and
  reporting of unreadable or disappearing paths. Symlinks and junctions are never followed.
- **Sizing.** Logical and allocated size, file and directory counts, and an optional time
  budget that reports unfinished artifacts as not measured. Large trees are measured with
  helper processes.
- **Cleanup.** Explicit selection, preview, typed confirmation, re-validation immediately
  before each deletion, dry runs, and an audit record that keeps detected, selected,
  deleted, failed and refused sizes apart.
- **Command line.** `sift scan`, `inspect`, `clean`, `config show|init|path`, `tui` and
  `serve`, with table, JSON and CSV output.
- **Terminal interface.** Project list, sortable and filterable artifact table, detail
  pane, inspection, selection and the full cleanup flow.
- **Local API and dashboard.** FastAPI endpoints for health, configuration, scans, artifact
  details, cleanup preview/execute/cancel, history and report export; a React dashboard on
  top of them. Loopback only, restricted to configured scan roots.
- **History.** Optional local SQLite record of scans and cleanups.
- **Configuration.** `~/.config/sift/config.toml` for scan roots, exclusions, depth,
  detectors, the confidence threshold, history and the server address.

### Known limitations

- Not published to PyPI; install from source.
- On Windows the allocated size is an estimate and hard-linked files are counted at full
  size.
- Tests that need symbolic links or POSIX permissions have only been written, not yet run:
  they are skipped on Windows, where the suite has been run, and the CI workflow that would
  run them on Linux and macOS has not executed yet.
- Scan time grows with the number of files and directories inside artifacts and depends on
  the disk and its cache; see the README for measurements.
