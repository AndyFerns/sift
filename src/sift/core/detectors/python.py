"""Python: bytecode and tool caches, test environments and virtual environments."""

from __future__ import annotations

from sift.core.detectors.base import Candidate, Detector, Rule
from sift.core.models import Ecosystem, Evidence

MANIFESTS = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "Pipfile",
    "tox.ini",
    "pytest.ini",
    "noxfile.py",
)


def _only_bytecode(candidate: Candidate) -> Evidence | None:
    """A real ``__pycache__`` holds compiled bytecode files and nothing else."""
    inside = candidate.children()
    if inside.dirs or inside.links:
        return None
    if not all(name.endswith((".pyc", ".pyo")) for name in inside.files):
        return None
    detail = "contains only compiled bytecode" if inside.files else "is empty"
    return Evidence(kind="marker", detail=detail, path=candidate.path)


DETECTOR = Detector(
    name="python",
    ecosystem=Ecosystem.PYTHON,
    rules=(
        Rule(
            id="python.pycache",
            names=("__pycache__",),
            ecosystem=Ecosystem.PYTHON,
            category="cache",
            description="Compiled bytecode cache (recreated on import)",
            check=_only_bytecode,
            distinctive=True,
            require_marker=True,
        ),
        Rule(
            id="python.pytest-cache",
            names=(".pytest_cache",),
            ecosystem=Ecosystem.PYTHON,
            category="cache",
            description="pytest cache",
            manifests=MANIFESTS,
            markers=("CACHEDIR.TAG", "v"),
            distinctive=True,
        ),
        Rule(
            id="python.mypy-cache",
            names=(".mypy_cache",),
            ecosystem=Ecosystem.PYTHON,
            category="cache",
            description="mypy incremental cache",
            manifests=(*MANIFESTS, "mypy.ini"),
            markers=("CACHEDIR.TAG",),
            distinctive=True,
        ),
        Rule(
            id="python.ruff-cache",
            names=(".ruff_cache",),
            ecosystem=Ecosystem.PYTHON,
            category="cache",
            description="Ruff cache",
            manifests=(*MANIFESTS, "ruff.toml", ".ruff.toml"),
            markers=("CACHEDIR.TAG",),
            distinctive=True,
        ),
        Rule(
            id="python.tox",
            names=(".tox",),
            ecosystem=Ecosystem.PYTHON,
            category="environment",
            description="tox test environments (recreated by `tox`)",
            manifests=("tox.ini", "pyproject.toml", "setup.cfg"),
            distinctive=True,
        ),
        Rule(
            id="python.nox",
            names=(".nox",),
            ecosystem=Ecosystem.PYTHON,
            category="environment",
            description="nox sessions (recreated by `nox`)",
            manifests=("noxfile.py",),
            distinctive=True,
        ),
        Rule(
            id="python.venv",
            names=(".venv", "venv"),
            ecosystem=Ecosystem.PYTHON,
            category="environment",
            description="Python virtual environment (must be recreated and reinstalled)",
            manifests=MANIFESTS,
            # Every virtual environment has this file; a folder without it is not one.
            markers=("pyvenv.cfg",),
            require_marker=True,
        ),
    ),
)
