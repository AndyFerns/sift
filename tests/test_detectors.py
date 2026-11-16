"""Every detector rule, checked against real directories.

Each case lists the files to create, the entry to classify and the expected verdict.
A trailing slash creates a directory; anything else creates an empty file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sift.core.config import CustomRule, DetectorsConfig, GoConfig
from sift.core.detectors import Candidate, Detection, build_ruleset, list_dir

SQLITE = b"SQLite format 3\x00" + b"\x00" * 100


def make(root: Path, entries: list[str]) -> None:
    for entry in entries:
        target = root / entry
        if entry.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(SQLITE if entry.endswith("sqlite.coverage") else b"")


def classify(root: Path, name: str, config: DetectorsConfig | None = None) -> Detection | None:
    listing = list_dir(str(root))
    is_dir = name in listing.dirs
    return build_ruleset(config).classify(Candidate(str(root / name), name, is_dir, listing))


# (files, candidate, rule, confidence, ambiguous)
CASES: list[tuple[list[str], str, str, str, bool]] = [
    # --- Rust ---
    (["Cargo.toml", "target/CACHEDIR.TAG"], "target", "rust.target", "high", False),
    (["Cargo.toml", "target/.rustc_info.json"], "target", "rust.target", "high", False),
    (["Cargo.toml", "target/debug/"], "target", "rust.target", "medium", False),
    (["target/.rustc_info.json"], "target", "rust.target", "medium", False),
    (["target/debug/"], "target", "unknown.target", "low", True),
    # --- Java / Maven ---
    (["pom.xml", "target/classes/"], "target", "java.maven-target", "high", False),
    (["pom.xml", "target/maven-status/"], "target", "java.maven-target", "high", False),
    (["pom.xml", "target/stuff/"], "target", "java.maven-target", "medium", False),
    # "classes" alone is too generic to prove anything
    (["target/classes/"], "target", "unknown.target", "low", True),
    # --- Rust vs Maven in the same directory ---
    (["Cargo.toml", "pom.xml", "target/stuff/"], "target", "rust.target", "medium", True),
    (["Cargo.toml", "pom.xml", "target/.rustc_info.json"], "target", "rust.target", "high", False),
    (
        ["Cargo.toml", "pom.xml", "target/maven-status/"],
        "target",
        "java.maven-target",
        "high",
        False,
    ),
    # --- Node ---
    (["package.json", "node_modules/dep/"], "node_modules", "node.node_modules", "high", False),
    (["node_modules/.package-lock.json"], "node_modules", "node.node_modules", "high", False),
    (["node_modules/.pnpm/"], "node_modules", "node.node_modules", "high", False),
    (["node_modules/dep/"], "node_modules", "node.node_modules", "low", True),
    (["package.json", ".next/"], ".next", "node.next", "high", False),
    (["next.config.mjs", ".next/"], ".next", "node.next", "high", False),
    ([".next/BUILD_ID"], ".next", "node.next", "high", False),
    ([".next/"], ".next", "node.next", "low", True),
    (["package.json", ".nuxt/"], ".nuxt", "node.nuxt", "high", False),
    (["nuxt.config.ts", ".nuxt/"], ".nuxt", "node.nuxt", "high", False),
    ([".nuxt/"], ".nuxt", "node.nuxt", "low", True),
    (["turbo.json", ".turbo/"], ".turbo", "node.turbo", "high", False),
    ([".turbo/"], ".turbo", "node.turbo", "low", True),
    (["angular.json", ".angular/cache/"], ".angular", "node.angular-cache", "high", False),
    ([".angular/cache/"], ".angular", "node.angular-cache", "low", True),
    # --- Python ---
    (["__pycache__/a.cpython-312.pyc"], "__pycache__", "python.pycache", "high", False),
    (["__pycache__/"], "__pycache__", "python.pycache", "high", False),
    (["__pycache__/a.pyc", "__pycache__/notes.txt"], "__pycache__", "python.pycache", "low", True),
    (["__pycache__/sub/"], "__pycache__", "python.pycache", "low", True),
    ([".pytest_cache/CACHEDIR.TAG"], ".pytest_cache", "python.pytest-cache", "high", False),
    (["pyproject.toml", ".pytest_cache/"], ".pytest_cache", "python.pytest-cache", "high", False),
    ([".pytest_cache/"], ".pytest_cache", "python.pytest-cache", "low", True),
    ([".mypy_cache/CACHEDIR.TAG"], ".mypy_cache", "python.mypy-cache", "high", False),
    (["mypy.ini", ".mypy_cache/"], ".mypy_cache", "python.mypy-cache", "high", False),
    ([".mypy_cache/"], ".mypy_cache", "python.mypy-cache", "low", True),
    ([".ruff_cache/CACHEDIR.TAG"], ".ruff_cache", "python.ruff-cache", "high", False),
    (["ruff.toml", ".ruff_cache/"], ".ruff_cache", "python.ruff-cache", "high", False),
    ([".ruff_cache/"], ".ruff_cache", "python.ruff-cache", "low", True),
    (["tox.ini", ".tox/"], ".tox", "python.tox", "high", False),
    ([".tox/"], ".tox", "python.tox", "low", True),
    (["noxfile.py", ".nox/"], ".nox", "python.nox", "high", False),
    ([".nox/"], ".nox", "python.nox", "low", True),
    (["pyproject.toml", ".venv/pyvenv.cfg"], ".venv", "python.venv", "high", False),
    ([".venv/pyvenv.cfg"], ".venv", "python.venv", "medium", False),
    (["requirements.txt", "venv/pyvenv.cfg"], "venv", "python.venv", "high", False),
    (["venv/pyvenv.cfg"], "venv", "python.venv", "medium", False),
    # a manifest cannot stand in for the missing pyvenv.cfg
    (["pyproject.toml", "venv/lib/"], "venv", "python.venv", "low", True),
    (["pyproject.toml", ".venv/lib/"], ".venv", "python.venv", "low", True),
    # --- Java / Gradle ---
    (["build.gradle", "build/classes/"], "build", "java.gradle-build", "high", False),
    (["build.gradle.kts", "build/libs/"], "build", "java.gradle-build", "high", False),
    (["settings.gradle", "build/stuff/"], "build", "java.gradle-build", "medium", False),
    (["build/classes/"], "build", "unknown.build", "low", True),
    (["build.gradle", ".gradle/"], ".gradle", "java.gradle-cache", "high", False),
    (["gradlew", ".gradle/"], ".gradle", "java.gradle-cache", "high", False),
    ([".gradle/"], ".gradle", "java.gradle-cache", "low", True),
    # --- C/C++ ---
    (["CMakeLists.txt", "build/CMakeCache.txt"], "build", "cpp.build", "high", False),
    (["build/CMakeCache.txt"], "build", "cpp.build", "medium", False),
    (["build/build.ninja"], "build", "cpp.build", "medium", False),
    # CMakeLists.txt beside a build/ that holds no CMake output is only a hint
    (["CMakeLists.txt", "build/scripts/"], "build", "cpp.build", "low", False),
    (["build/scripts/"], "build", "unknown.build", "low", True),
    (
        ["CMakeLists.txt", "cmake-build-debug/"],
        "cmake-build-debug",
        "cpp.cmake-build",
        "high",
        False,
    ),
    (
        ["cmake-build-release/CMakeCache.txt"],
        "cmake-build-release",
        "cpp.cmake-build",
        "high",
        False,
    ),
    (["cmake-build-debug/"], "cmake-build-debug", "cpp.cmake-build", "low", True),
    (["CMakeCache.txt", "CMakeFiles/"], "CMakeFiles", "cpp.cmakefiles", "high", False),
    (["CMakeFiles/CMakeOutput.log"], "CMakeFiles", "cpp.cmakefiles", "high", False),
    (["CMakeFiles/"], "CMakeFiles", "cpp.cmakefiles", "low", True),
    # Gradle and CMake both have full evidence (Android NDK style): refuse to pick
    (
        ["build.gradle", "CMakeLists.txt", "build/CMakeCache.txt", "build/tmp/"],
        "build",
        "java.gradle-build",
        "high",
        True,
    ),
    # --- General ---
    (["package.json", "coverage/lcov.info"], "coverage", "general.coverage", "high", False),
    (["coverage/lcov-report/"], "coverage", "general.coverage", "medium", False),
    (["package.json", "coverage/__init__.py"], "coverage", "general.coverage", "low", False),
    (["coverage/__init__.py"], "coverage", "general.coverage", "low", True),
    (["pyproject.toml", "dist/pkg-1.0-py3-none-any.whl"], "dist", "general.dist", "high", False),
    (["setup.py", "dist/pkg-1.0.tar.gz"], "dist", "general.dist", "high", False),
    (["package.json", "dist/bundle.js"], "dist", "general.dist", "low", False),
    (["dist/pkg-1.0-py3-none-any.whl"], "dist", "general.dist", "low", True),
    (["dist/bundle.js"], "dist", "general.dist", "low", True),
]


@pytest.mark.parametrize(
    ("files", "name", "rule", "confidence", "ambiguous"),
    CASES,
    ids=[f"{c[2]}:{'+'.join(c[0])}" for c in CASES],
)
def test_rule(
    tmp_path: Path, files: list[str], name: str, rule: str, confidence: str, ambiguous: bool
) -> None:
    make(tmp_path, files)
    detection = classify(tmp_path, name)
    assert detection is not None
    assert detection.rule.id == rule
    assert detection.confidence.value == confidence
    assert detection.ambiguous is ambiguous
    assert detection.evidence, "every detection must explain itself"
    if ambiguous:
        assert detection.review_reason


def test_name_alone_is_never_enough() -> None:
    """No rule may reach a cleanable confidence without a manifest or a marker."""
    ruleset = build_ruleset(
        DetectorsConfig(
            go=GoConfig(artifact_dirs=["bin"]),
            custom=[CustomRule(name="tf", dir_names=[".terraform"], manifests=["main.tf"])],
        )
    )
    names = sorted(ruleset.dir_rules)
    assert len(names) >= 20
    import tempfile

    for name in names:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / name).mkdir()
            (root / name / "something.txt").write_text("x")
            detection = classify(root, name, DetectorsConfig(go=GoConfig(artifact_dirs=["bin"])))
            if detection is None:
                continue
            assert detection.ambiguous, f"{name} was classified on its name alone"
            assert detection.confidence.value == "low"


def test_coverage_data_file(tmp_path: Path) -> None:
    (tmp_path / ".coverage").write_bytes(SQLITE)
    detection = classify(tmp_path, ".coverage")
    assert detection is not None
    assert detection.rule.id == "general.coverage-data"
    assert detection.rule.kind == "file"
    assert detection.confidence.value == "high"
    assert not detection.ambiguous


def test_coverage_file_that_is_not_a_database(tmp_path: Path) -> None:
    (tmp_path / ".coverage").write_text("just some notes")
    detection = classify(tmp_path, ".coverage")
    assert detection is not None
    assert detection.ambiguous
    assert detection.confidence.value == "low"


def test_kind_must_match(tmp_path: Path) -> None:
    (tmp_path / ".coverage").mkdir()  # a directory with a file rule's name
    (tmp_path / "target").write_text("")  # a file with a directory rule's name
    (tmp_path / "Cargo.toml").write_text("")
    assert classify(tmp_path, ".coverage") is None
    assert classify(tmp_path, "target") is None


def test_angular_without_cache_is_not_an_artifact(tmp_path: Path) -> None:
    make(tmp_path, ["angular.json", ".angular/other/"])
    assert classify(tmp_path, ".angular") is None


def test_angular_artifact_is_the_cache_subdirectory(tmp_path: Path) -> None:
    make(tmp_path, ["angular.json", ".angular/cache/17/x.bin"])
    detection = classify(tmp_path, ".angular")
    assert detection is not None
    assert Path(detection.path) == tmp_path / ".angular" / "cache"


def test_go_does_nothing_until_configured(tmp_path: Path) -> None:
    make(tmp_path, ["go.mod", "bin/tool"])
    assert classify(tmp_path, "bin") is None

    config = DetectorsConfig(go=GoConfig(artifact_dirs=["bin"]))
    detection = classify(tmp_path, "bin", config)
    assert detection is not None
    assert detection.rule.id == "go.bin"
    assert detection.confidence.value == "medium"
    assert not detection.ambiguous


def test_go_directory_without_go_mod_is_ambiguous(tmp_path: Path) -> None:
    make(tmp_path, ["bin/tool"])
    detection = classify(tmp_path, "bin", DetectorsConfig(go=GoConfig(artifact_dirs=["bin"])))
    assert detection is not None
    assert detection.ambiguous


def test_custom_rule(tmp_path: Path) -> None:
    config = DetectorsConfig(
        custom=[
            CustomRule(
                name="terraform",
                description="Terraform provider cache",
                dir_names=[".terraform"],
                file_names=["terraform.tfstate.backup"],
                manifests=["main.tf"],
            )
        ]
    )
    make(tmp_path, ["main.tf", ".terraform/providers/", "terraform.tfstate.backup"])
    directory = classify(tmp_path, ".terraform", config)
    assert directory is not None
    assert directory.rule.id == "custom.terraform"
    assert directory.rule.ecosystem.value == "custom"
    assert directory.confidence.value == "medium"
    assert not directory.ambiguous

    file = classify(tmp_path, "terraform.tfstate.backup", config)
    assert file is not None
    assert file.rule.kind == "file"
    assert not file.ambiguous


def test_custom_rule_without_evidence_stays_ambiguous(tmp_path: Path) -> None:
    config = DetectorsConfig(custom=[CustomRule(name="gen", dir_names=["generated"])])
    make(tmp_path, ["generated/out.txt"])
    detection = classify(tmp_path, "generated", config)
    assert detection is not None
    assert detection.ambiguous


def test_disabled_detector_is_not_consulted(tmp_path: Path) -> None:
    make(tmp_path, ["package.json", "node_modules/dep/", "Cargo.toml", "target/CACHEDIR.TAG"])
    config = DetectorsConfig(enabled=["rust"])
    assert classify(tmp_path, "node_modules", config) is None
    assert classify(tmp_path, "target", config) is not None


def test_evidence_names_what_was_found(tmp_path: Path) -> None:
    make(tmp_path, ["Cargo.toml", "target/CACHEDIR.TAG"])
    detection = classify(tmp_path, "target")
    assert detection is not None
    kinds = {e.kind for e in detection.evidence}
    assert kinds == {"manifest", "marker"}
    details = " ".join(e.detail for e in detection.evidence)
    assert "Cargo.toml" in details
    assert "CACHEDIR.TAG" in details


def test_conflict_is_explained(tmp_path: Path) -> None:
    make(tmp_path, ["Cargo.toml", "pom.xml", "target/stuff/"])
    detection = classify(tmp_path, "target")
    assert detection is not None
    assert detection.ambiguous
    assert any(e.kind == "conflict" for e in detection.evidence)
    assert "java.maven-target" in (detection.review_reason or "")
