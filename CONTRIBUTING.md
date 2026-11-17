# Contributing to Sift

Thanks for wanting to help. Sift deletes files, so the bar for changes is "boringly safe"
first and "clever" a distant second.

## Getting set up

You need Python 3.12+ and, for the dashboard, Node.js 22+.

```bash
git clone https://github.com/AndyFerns/sift
cd sift
python -m venv .venv
```

Activate the environment (`.venv\Scripts\activate` on Windows, `source .venv/bin/activate`
elsewhere), then:

```bash
python -m pip install -e ".[dev]"
```

For the dashboard:

```bash
cd web
npm install
```

## Checks to run before opening a pull request

```bash
pytest
ruff check .
ruff format --check .
mypy
```

And if you touched `web/`:

```bash
npm run typecheck
npm test
```

CI runs the same commands on Linux, macOS and Windows. Some code is platform-specific, and
mypy only checks the branches for the platform it runs on, so also check the other two:

```bash
mypy --platform linux
mypy --platform darwin
mypy --platform win32
```

## How the code is organised

- `src/sift/core/` is the only place that scans, classifies, measures or deletes.
  It must not import from the interfaces.
- `src/sift/cli/`, `src/sift/tui/` and `src/sift/api/` are thin layers over
  `sift.core.service.SiftService`. If you find yourself walking a directory or removing a
  file in one of them, that code belongs in the core.
- `web/` is the React dashboard. It only talks to the HTTP API.

## Ground rules

**Detection**

- A directory name is never enough to classify something. Every rule needs a manifest
  (a file beside the artifact) or a marker (something inside it) as evidence.
- When evidence is missing or conflicting, mark the artifact ambiguous. An ambiguous
  artifact can be shown, but never cleaned.
- New detectors need tests for the positive case, the name-only case and any lookalike
  you can think of.

**Safety**

- Nothing is deleted without explicit selection, a preview and a confirmation.
  Do not add flags that skip any of those.
- All deletion goes through `sift.core.cleanup`. Paths are re-validated immediately
  before removal.
- Never follow symlinks or junctions. Never run a command or script found in a scanned
  project.
- Never describe deletion as reversible in output or documentation.

**Tests**

- Tests use real files in pytest temporary directories (`tmp_path`). Anything that
  deletes must only ever touch a temporary directory.
- The shared fixture workspace in `tests/fixture_workspace.py` has known artifacts and
  known sizes. If you change scanner behaviour, the same fixture must still give the same
  answer through the core, CLI, TUI and API.
- Do not mock the scanner to make an interface test pass, and do not put made-up numbers
  in the dashboard.

**The dashboard and the API**

- API types in `web/src/api/schema.ts` are generated. After changing a Pydantic model that
  the API returns, run `python scripts/gen_api_types.py`; a test fails if you forget.
- The dashboard tests render responses recorded from the real API. After changing the
  fixture workspace or what a scan returns, run `python scripts/record_fixture_scan.py`.
- Sizes are formatted identically in Python and TypeScript. Both are tested against
  `tests/shared/format_bytes.json`; add cases there rather than in one language only.

**Honesty in docs**

- The README separates what is implemented from what is planned. Keep it that way.
- Do not add badges, release links or benchmark numbers that are not real.

## Commit and pull request style

Small, focused commits with a plain-language message are ideal. The pull request template
asks about safety impact; please fill it in if you touched detection, validation or
deletion.

## Reporting security issues

Please do not open a public issue. See [SECURITY.md](SECURITY.md).
