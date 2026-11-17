"""The shared fixture workspace.

``build_workspace`` writes a small multi-project tree with artifacts of known type and
exact size, and returns what a correct scan must report. The same fixture is used by the
core, CLI, TUI and API tests, so every interface is checked against one source of truth.

Artifact directories cannot be committed to git (``node_modules`` and friends are ignored
almost everywhere), which is why the tree is generated rather than stored.
"""

from __future__ import annotations

import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

IGNORE_TEXT = "# excluded from every scan\nignored-project/\n"
BIG_PACKAGES = 10
BIG_FILES_PER_PACKAGE = 10
BIG_FILE_SIZE = 300


@dataclass(frozen=True)
class Expected:
    relative_path: str
    rule: str
    ecosystem: str
    confidence: str
    ambiguous: bool
    cleanable: bool
    logical_bytes: int
    file_count: int
    kind: str = "directory"
    project: str = "."
    recursive_only: bool = False
    """Lives inside a nested repository, so only a recursive scan reports it."""
    needs_go_config: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class Workspace:
    root: Path
    expected: list[Expected] = field(default_factory=list)
    symlinks: bool = False
    junctions: bool = False

    def expect(self, *, recursive: bool, go: bool = False) -> dict[str, Expected]:
        """Expected artifacts keyed by relative path for the given scan mode."""
        return {
            e.relative_path: e
            for e in self.expected
            if (recursive or not e.recursive_only) and (go or not e.needs_go_config)
        }

    def path(self, relative: str) -> Path:
        return self.root / relative


def _write(path: Path, size: int | bytes) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = size if isinstance(size, bytes) else b"x" * size
    path.write_bytes(data)
    return len(data)


class _Builder:
    def __init__(self, root: Path) -> None:
        self.ws = Workspace(root)

    def source(self, relative: str, text: str = "") -> None:
        _write(self.ws.root / relative, text.encode())

    def artifact(
        self,
        relative: str,
        rule: str,
        ecosystem: str,
        confidence: str,
        files: dict[str, int | bytes],
        *,
        ambiguous: bool = False,
        cleanable: bool | None = None,
        project: str = ".",
        recursive_only: bool = False,
        needs_go_config: bool = False,
    ) -> None:
        total = 0
        for name, size in files.items():
            total += _write(self.ws.root / relative / name, size)
        if cleanable is None:
            cleanable = not ambiguous and confidence in {"high", "medium"}
        self.ws.expected.append(
            Expected(
                relative_path=relative,
                rule=rule,
                ecosystem=ecosystem,
                confidence=confidence,
                ambiguous=ambiguous,
                cleanable=cleanable,
                logical_bytes=total,
                file_count=len(files),
                project=project,
                recursive_only=recursive_only,
                needs_go_config=needs_go_config,
            )
        )


def _try_symlink(link: Path, target: Path, *, directory: bool) -> bool:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError):
        return False
    return True


def _try_junction(link: Path, target: Path) -> bool:
    if sys.platform != "win32":
        return False
    import _winapi

    try:
        _winapi.CreateJunction(str(target), str(link))
    except OSError:
        return False
    return True


def build_workspace(root: Path) -> Workspace:
    """Create the fixture under ``root`` (which must be empty or absent)."""
    root.mkdir(parents=True, exist_ok=True)
    b = _Builder(root)
    b.source(".siftignore", IGNORE_TEXT)

    # --- Rust ------------------------------------------------------------------------
    b.source("rust-app/Cargo.toml", '[package]\nname = "rust-app"\nversion = "0.1.0"\n')
    b.source("rust-app/src/main.rs", "fn main() {}\n")
    b.artifact(
        "rust-app/target",
        "rust.target",
        "rust",
        "high",
        {
            "CACHEDIR.TAG": 43,
            ".rustc_info.json": 120,
            "debug/rust-app": 4096,
            "debug/deps/librust_app.rlib": 10_000,
            "debug/incremental/state.bin": 2048,
        },
        project="rust-app",
    )

    # --- Node ------------------------------------------------------------------------
    b.source("node-app/package.json", '{"name": "node-app", "version": "1.0.0"}\n')
    b.source("node-app/src/index.js", "console.log('hi')\n")
    b.artifact(
        "node-app/node_modules",
        "node.node_modules",
        "node",
        "high",
        {
            ".package-lock.json": 200,
            ".bin/tool": 10,
            "left-pad/package.json": 150,
            "left-pad/index.js": 1024,
            # A nested node_modules is part of its parent, never a second artifact.
            "left-pad/node_modules/inner/index.js": 512,
        },
        project="node-app",
    )
    b.artifact(
        "node-app/.next",
        "node.next",
        "node",
        "high",
        {"BUILD_ID": 21, "cache/webpack/0.pack": 8192},
        project="node-app",
    )
    b.artifact(
        "node-app/.nuxt", "node.nuxt", "node", "high", {"nuxt.d.ts": 300}, project="node-app"
    )
    b.artifact(
        "node-app/.turbo",
        "node.turbo",
        "node",
        "high",
        {"cache/abc123.tar.zst": 2000},
        project="node-app",
    )
    b.artifact(
        "node-app/.angular/cache",
        "node.angular-cache",
        "node",
        "high",
        {"17.0.0/babel/entry.bin": 700},
        project="node-app",
    )
    # dist/ next to a manifest but with nothing proving it is generated: reported with low
    # confidence, not cleanable at the default threshold.
    b.artifact(
        "node-app/dist",
        "general.dist",
        "general",
        "low",
        {"bundle.js": 5000},
        project="node-app",
    )
    b.artifact(
        "node-app/coverage",
        "general.coverage",
        "general",
        "high",
        {"lcov.info": 900, "lcov-report/index.html": 400},
        project="node-app",
    )

    # --- Python ----------------------------------------------------------------------
    b.source("py-app/pyproject.toml", '[project]\nname = "py-app"\nversion = "0.1.0"\n')
    b.source("py-app/noxfile.py", "import nox\n")
    b.source("py-app/src/pkg/mod.py", "VALUE = 1\n")
    b.artifact(
        "py-app/.venv",
        "python.venv",
        "python",
        "high",
        {
            "pyvenv.cfg": 80,
            "Scripts/python.exe": 3000,
            "Lib/site-packages/dep/__init__.py": 64,
            # Bytecode inside a venv belongs to the venv.
            "Lib/site-packages/dep/__pycache__/__init__.cpython-312.pyc": 128,
        },
        project="py-app",
    )
    # Called venv, but there is no pyvenv.cfg, so it is not a virtual environment.
    b.artifact(
        "py-app/venv",
        "python.venv",
        "python",
        "low",
        {"notes.txt": 50},
        ambiguous=True,
        project="py-app",
    )
    b.artifact(
        "py-app/src/pkg/__pycache__",
        "python.pycache",
        "python",
        "high",
        {"mod.cpython-312.pyc": 700, "__init__.cpython-312.pyc": 300},
        project="py-app",
    )
    b.artifact(
        "py-app/.pytest_cache",
        "python.pytest-cache",
        "python",
        "high",
        {"CACHEDIR.TAG": 191, "v/cache/lastfailed": 2},
        project="py-app",
    )
    b.artifact(
        "py-app/.mypy_cache",
        "python.mypy-cache",
        "python",
        "high",
        {"CACHEDIR.TAG": 191, "3.12/pkg.data.json": 4000},
        project="py-app",
    )
    b.artifact(
        "py-app/.ruff_cache",
        "python.ruff-cache",
        "python",
        "high",
        {"CACHEDIR.TAG": 43, "0.6.0/1234567890": 256},
        project="py-app",
    )
    b.artifact(
        "py-app/.tox", "python.tox", "python", "high", {"py312/pyvenv.cfg": 80}, project="py-app"
    )
    b.artifact(
        "py-app/.nox", "python.nox", "python", "high", {"tests/pyvenv.cfg": 80}, project="py-app"
    )
    b.artifact(
        "py-app/dist",
        "general.dist",
        "general",
        "high",
        {"py_app-0.1.0-py3-none-any.whl": 6000, "py_app-0.1.0.tar.gz": 5000},
        project="py-app",
    )
    coverage_data = b"SQLite format 3\x00" + b"\x00" * 4080
    _write(root / "py-app/.coverage", coverage_data)
    b.ws.expected.append(
        Expected(
            relative_path="py-app/.coverage",
            rule="general.coverage-data",
            ecosystem="general",
            confidence="high",
            ambiguous=False,
            cleanable=True,
            logical_bytes=len(coverage_data),
            file_count=1,
            kind="file",
            project="py-app",
        )
    )

    # --- Java ------------------------------------------------------------------------
    b.source("java-maven/pom.xml", "<project></project>\n")
    b.artifact(
        "java-maven/target",
        "java.maven-target",
        "java",
        "high",
        {"classes/App.class": 1500, "maven-status/inputFiles.lst": 30},
        project="java-maven",
    )
    b.source("java-gradle/build.gradle.kts", "plugins { java }\n")
    b.source("java-gradle/settings.gradle.kts", 'rootProject.name = "java-gradle"\n')
    b.artifact(
        "java-gradle/build",
        "java.gradle-build",
        "java",
        "high",
        {"classes/java/main/App.class": 1500, "libs/app.jar": 9000, "tmp/jar/MANIFEST.MF": 25},
        project="java-gradle",
    )
    b.artifact(
        "java-gradle/.gradle",
        "java.gradle-cache",
        "java",
        "high",
        {"8.5/fileHashes/fileHashes.bin": 2222},
        project="java-gradle",
    )

    # --- C/C++ -----------------------------------------------------------------------
    b.source("cpp-app/CMakeLists.txt", "project(cpp-app)\n")
    b.artifact(
        "cpp-app/build",
        "cpp.build",
        "cpp",
        "high",
        {"CMakeCache.txt": 600, "CMakeFiles/main.dir/main.o": 3000, "cpp-app": 7000},
        project="cpp-app",
    )
    b.artifact(
        "cpp-app/cmake-build-debug",
        "cpp.cmake-build",
        "cpp",
        "high",
        {"CMakeCache.txt": 600, "cpp-app": 6000},
        project="cpp-app",
    )
    b.artifact(
        "cpp-app/cmake-build-release",
        "cpp.cmake-build",
        "cpp",
        "high",
        {"CMakeCache.txt": 600, "cpp-app": 2500},
        project="cpp-app",
    )
    b.source("cpp-insource/CMakeLists.txt", "project(insource)\n")
    b.source("cpp-insource/CMakeCache.txt", "CMAKE_BUILD_TYPE:STRING=Debug\n")
    b.artifact(
        "cpp-insource/CMakeFiles",
        "cpp.cmakefiles",
        "cpp",
        "high",
        {"CMakeOutput.log": 100, "main.dir/main.o": 1800},
        project="cpp-insource",
    )

    # --- Go (only detected when the config names "bin") ------------------------------
    b.source("go-app/go.mod", "module example.com/go-app\n")
    b.artifact(
        "go-app/bin",
        "go.bin",
        "go",
        "medium",
        {"tool": 12_000},
        project="go-app",
        needs_go_config=True,
    )

    # --- Ambiguous: the right name, no evidence ---------------------------------------
    b.source("ambiguous/README.md", "nothing here is a build artifact\n")
    b.artifact(
        "ambiguous/build", "unknown.build", "general", "low", {"out.txt": 100}, ambiguous=True
    )
    b.artifact(
        "ambiguous/target", "unknown.target", "general", "low", {"out.txt": 100}, ambiguous=True
    )
    b.artifact("ambiguous/dist", "general.dist", "general", "low", {"out.txt": 100}, ambiguous=True)
    b.artifact(
        "ambiguous/coverage",
        "general.coverage",
        "general",
        "low",
        {"__init__.py": 10},
        ambiguous=True,
    )
    b.artifact(
        "ambiguous/__pycache__",
        "python.pycache",
        "python",
        "low",
        {"notes.txt": 10},
        ambiguous=True,
    )
    b.artifact(
        "ambiguous/node_modules",
        "node.node_modules",
        "node",
        "low",
        {"readme.txt": 10},
        ambiguous=True,
    )

    # --- Conflict: two ecosystems claim the same directory ---------------------------
    b.source("conflict/Cargo.toml", '[package]\nname = "conflict"\n')
    b.source("conflict/pom.xml", "<project></project>\n")
    b.artifact(
        "conflict/target",
        "rust.target",
        "rust",
        "medium",
        {"output.bin": 500},
        ambiguous=True,
        project="conflict",
    )

    # --- Nested repositories ---------------------------------------------------------
    b.source("nested/outer/.git/HEAD", "ref: refs/heads/main\n")
    b.source("nested/outer/package.json", '{"name": "outer"}\n')
    b.artifact(
        "nested/outer/node_modules",
        "node.node_modules",
        "node",
        "high",
        {".package-lock.json": 100, "dep/index.js": 900},
        project="nested/outer",
        recursive_only=True,
    )
    b.source("nested/outer/inner/.git/HEAD", "ref: refs/heads/main\n")
    b.source("nested/outer/inner/Cargo.toml", '[package]\nname = "inner"\n')
    b.artifact(
        "nested/outer/inner/target",
        "rust.target",
        "rust",
        "high",
        {"CACHEDIR.TAG": 43, ".rustc_info.json": 100, "debug/inner": 1234},
        project="nested/outer/inner",
        recursive_only=True,
    )

    # --- Excluded by .siftignore -----------------------------------------------------
    b.source("ignored-project/package.json", '{"name": "ignored"}\n')
    _write(root / "ignored-project/node_modules/.package-lock.json", 100)
    _write(root / "ignored-project/node_modules/a/index.js", 100)

    # --- The largest artifact: many small files -------------------------------------
    b.source("big/package.json", '{"name": "big"}\n')
    big: dict[str, int | bytes] = {".package-lock.json": 10}
    for package in range(BIG_PACKAGES):
        for index in range(BIG_FILES_PER_PACKAGE):
            big[f"pkg-{package:02d}/lib/file-{index:02d}.js"] = BIG_FILE_SIZE
    b.artifact("big/node_modules", "node.node_modules", "node", "high", big, project="big")

    # --- Links: never followed, never classified -------------------------------------
    links = root / "links"
    (links / "proj").mkdir(parents=True)
    b.source("links/proj/package.json", '{"name": "linked"}\n')
    real = root / "node-app" / "node_modules"
    b.ws.symlinks = _try_symlink(links / "proj" / "node_modules", real, directory=True)
    if b.ws.symlinks:
        _try_symlink(links / "broken", root / "does-not-exist", directory=False)
        _try_symlink(links / "to-rust", root / "rust-app", directory=True)
    b.ws.junctions = _try_junction(links / "junction-target", root / "rust-app" / "target")

    return b.ws
