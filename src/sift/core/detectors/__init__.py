"""Artifact detectors, one module per ecosystem."""

from __future__ import annotations

from sift.core.config import DetectorsConfig
from sift.core.detectors import cpp, custom, general, go, java, node, python, rust
from sift.core.detectors.base import (
    Candidate,
    Detection,
    Detector,
    DirListing,
    Rule,
    RuleSet,
    is_link,
    list_dir,
)
from sift.core.models import Ecosystem

__all__ = [
    "PROJECT_MANIFESTS",
    "Candidate",
    "Detection",
    "Detector",
    "DirListing",
    "Rule",
    "RuleSet",
    "build_ruleset",
    "is_link",
    "list_dir",
]

# Files that mark a directory as a project root, and the ecosystem they imply.
PROJECT_MANIFESTS: dict[str, Ecosystem] = {
    "Cargo.toml": Ecosystem.RUST,
    "package.json": Ecosystem.NODE,
    "pyproject.toml": Ecosystem.PYTHON,
    "setup.py": Ecosystem.PYTHON,
    "requirements.txt": Ecosystem.PYTHON,
    "pom.xml": Ecosystem.JAVA,
    "build.gradle": Ecosystem.JAVA,
    "build.gradle.kts": Ecosystem.JAVA,
    "settings.gradle": Ecosystem.JAVA,
    "settings.gradle.kts": Ecosystem.JAVA,
    "CMakeLists.txt": Ecosystem.CPP,
    "go.mod": Ecosystem.GO,
}


def build_ruleset(config: DetectorsConfig | None = None) -> RuleSet:
    """Assemble the enabled detectors, in a fixed order so results are deterministic."""
    config = config or DetectorsConfig()
    available: dict[str, Detector] = {
        "rust": rust.DETECTOR,
        "node": node.DETECTOR,
        "python": python.DETECTOR,
        "java": java.DETECTOR,
        "cpp": cpp.DETECTOR,
        "go": go.build(config.go),
        "general": general.DETECTOR,
    }
    detectors = [det for name, det in available.items() if name in config.enabled]
    if config.custom:
        detectors.append(custom.build(config.custom))
    return RuleSet(detectors)
