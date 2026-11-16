"""Configuration: ``~/.config/sift/config.toml`` plus sensible defaults.

The config file is optional. A missing file means defaults; a malformed file is an error
rather than something to silently ignore, because a typo in an exclusion or threshold
should never quietly change what Sift is willing to delete.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Annotated

import platformdirs
import tomli_w
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from sift.core.models import Confidence

BUILTIN_DETECTORS: tuple[str, ...] = ("rust", "node", "python", "java", "cpp", "go", "general")
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})


class ConfigError(Exception):
    """The configuration file exists but cannot be used."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def _check_component(value: str) -> str:
    """Rule names must be a single path component, never a path."""
    stripped = value.strip()
    if not stripped or stripped in {".", ".."}:
        raise ValueError(f"{value!r} is not a valid file or directory name")
    if any(sep in stripped for sep in ("/", "\\", ":", "\0")):
        raise ValueError(f"{value!r} must be a plain name, not a path")
    return stripped


Name = Annotated[str, Field(min_length=1)]


class ScanConfig(_Section):
    roots: list[str] = Field(default_factory=list)
    """Directories the dashboard API is allowed to scan and clean."""
    exclude: list[str] = Field(default_factory=list)
    """gitignore-style patterns, relative to the scan root."""
    max_depth: int | None = Field(default=None, ge=0)
    """How many directory levels below the scan root to descend. Unset means unlimited."""
    min_confidence: Confidence = Confidence.MEDIUM
    """Artifacts below this confidence are reported but can never be cleaned."""
    time_budget_seconds: float | None = Field(default=None, gt=0)
    workers: int | None = Field(default=None, ge=0, le=64)
    """Helper processes used to measure large trees. Unset picks from the CPU count."""


class GoConfig(_Section):
    artifact_dirs: list[Name] = Field(default_factory=list)
    """Directory names treated as Go build output when they sit next to a go.mod."""

    @field_validator("artifact_dirs")
    @classmethod
    def _names(cls, value: list[str]) -> list[str]:
        return [_check_component(v) for v in value]


class CustomRule(_Section):
    name: Name
    description: str = "Custom generated artifact"
    dir_names: list[Name] = Field(default_factory=list)
    file_names: list[Name] = Field(default_factory=list)
    manifests: list[Name] = Field(default_factory=list)
    """Files that must sit next to the artifact for it to be recognised."""
    markers: list[Name] = Field(default_factory=list)
    """Entries expected inside the artifact directory."""

    @field_validator("dir_names", "file_names", "manifests", "markers")
    @classmethod
    def _names(cls, value: list[str]) -> list[str]:
        return [_check_component(v) for v in value]


class DetectorsConfig(_Section):
    enabled: list[str] = Field(default_factory=lambda: list(BUILTIN_DETECTORS))
    go: GoConfig = Field(default_factory=GoConfig)
    custom: list[CustomRule] = Field(default_factory=list)

    @field_validator("enabled")
    @classmethod
    def _known(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(BUILTIN_DETECTORS))
        if unknown:
            known = ", ".join(BUILTIN_DETECTORS)
            raise ValueError(f"unknown detector(s) {', '.join(unknown)}; known: {known}")
        return value


class HistoryConfig(_Section):
    enabled: bool = True
    max_entries: int = Field(default=100, ge=1, le=10_000)


class ServerConfig(_Section):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)

    @field_validator("host")
    @classmethod
    def _loopback(cls, value: str) -> str:
        if value not in LOOPBACK_HOSTS:
            raise ValueError(
                "the dashboard has no authentication and only binds to loopback addresses "
                f"({', '.join(sorted(LOOPBACK_HOSTS))})"
            )
        return value


class SiftConfig(_Section):
    scan: ScanConfig = Field(default_factory=ScanConfig)
    detectors: DetectorsConfig = Field(default_factory=DetectorsConfig)
    history: HistoryConfig = Field(default_factory=HistoryConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)

    def resolved_roots(self) -> list[str]:
        """Scan roots with ``~`` expanded, made absolute, without duplicates."""
        seen: dict[str, None] = {}
        for raw in self.scan.roots:
            seen.setdefault(os.path.abspath(os.path.expanduser(raw)), None)
        return list(seen)


def config_dir() -> Path:
    override = os.environ.get("SIFT_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".config" / "sift"


def config_path() -> Path:
    return config_dir() / "config.toml"


def data_dir() -> Path:
    override = os.environ.get("SIFT_DATA_DIR")
    if override:
        return Path(override)
    return Path(platformdirs.user_data_dir("sift", appauthor=False))


def _describe(error: ValidationError) -> str:
    lines = []
    for item in error.errors():
        where = ".".join(str(part) for part in item["loc"]) or "(top level)"
        lines.append(f"  {where}: {item['msg']}")
    return "\n".join(lines)


def parse_config(text: str, *, source: str = "config") -> SiftConfig:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{source} is not valid TOML: {exc}") from exc
    try:
        return SiftConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{source} has invalid settings:\n{_describe(exc)}") from exc


def load_config(path: Path | None = None) -> SiftConfig:
    """Load the config file, or return defaults when it does not exist."""
    target = path or config_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return SiftConfig()
    except OSError as exc:
        raise ConfigError(f"cannot read {target}: {exc}") from exc
    return parse_config(text, source=str(target))


def dump_config(config: SiftConfig) -> str:
    return tomli_w.dumps(config.model_dump(mode="json", exclude_none=True))


def save_config(config: SiftConfig, path: Path | None = None) -> Path:
    """Rewrite the config file from ``config``. Comments in an existing file are not kept."""
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dump_config(config), encoding="utf-8")
    return target


DEFAULT_CONFIG_TEMPLATE = """\
# Sift configuration.
# Every setting is optional; the values below are the defaults.

[scan]
# Directories the local dashboard is allowed to scan and clean.
# The CLI and TUI take the path as an argument and do not need this.
roots = []

# gitignore-style patterns to skip, relative to the directory being scanned.
# Per-project patterns can also go in a .siftignore file.
exclude = []

# How many directory levels below the scan root to descend. Leave unset for no limit.
# max_depth = 8

# Artifacts below this confidence are still reported, but can never be cleaned.
# One of: "high", "medium", "low". Ambiguous artifacts are never cleanable at any setting.
min_confidence = "medium"

# Stop measuring sizes after this many seconds. Artifacts not reached are reported as
# "not measured" rather than guessed. Leave unset to always measure everything.
# time_budget_seconds = 3.0

[detectors]
enabled = ["rust", "node", "python", "java", "cpp", "go", "general"]

[detectors.go]
# Go has no standard in-project build directory, so name yours here.
# These are only matched when they sit next to a go.mod file.
artifact_dirs = []

# Add your own generated-artifact rules. A rule needs at least one manifest (a file that
# must sit next to the artifact) or marker (an entry expected inside it) - Sift never
# treats a directory as cleanable on its name alone.
#
# [[detectors.custom]]
# name = "terraform"
# description = "Terraform provider cache"
# dir_names = [".terraform"]
# manifests = ["main.tf"]

[history]
# Keep past scans and cleanup audits in a local SQLite database.
enabled = true
max_entries = 100

[server]
# The dashboard only binds to loopback addresses.
host = "127.0.0.1"
port = 8765
"""


def write_default_config(path: Path | None = None, *, force: bool = False) -> Path:
    """Create a commented starter config. Refuses to overwrite unless ``force`` is set."""
    target = path or config_path()
    if target.exists() and not force:
        raise FileExistsError(str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    return target
