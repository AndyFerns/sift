from __future__ import annotations

from pathlib import Path

import pytest

from sift.core.config import (
    ConfigError,
    SiftConfig,
    config_path,
    data_dir,
    load_config,
    parse_config,
    save_config,
    write_default_config,
)
from sift.core.models import Confidence


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "nope.toml")
    assert config == SiftConfig()
    assert config.scan.min_confidence is Confidence.MEDIUM
    assert config.history.enabled is True
    assert config.server.host == "127.0.0.1"


def test_default_template_is_valid_and_matches_defaults(tmp_path: Path) -> None:
    target = write_default_config(tmp_path / "config.toml")
    assert load_config(target) == SiftConfig()


def test_init_refuses_to_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text("[scan]\nmax_depth = 2\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_default_config(target)
    assert load_config(target).scan.max_depth == 2
    write_default_config(target, force=True)
    assert load_config(target).scan.max_depth is None


def test_all_sections_parse() -> None:
    config = parse_config(
        """
        [scan]
        roots = ["~/code"]
        exclude = ["vendor/"]
        max_depth = 4
        min_confidence = "high"
        time_budget_seconds = 2.5

        [detectors]
        enabled = ["rust", "node"]

        [detectors.go]
        artifact_dirs = ["bin"]

        [[detectors.custom]]
        name = "terraform"
        dir_names = [".terraform"]
        manifests = ["main.tf"]

        [history]
        enabled = false
        """
    )
    assert config.scan.max_depth == 4
    assert config.scan.min_confidence is Confidence.HIGH
    assert config.detectors.enabled == ["rust", "node"]
    assert config.detectors.go.artifact_dirs == ["bin"]
    assert config.detectors.custom[0].dir_names == [".terraform"]
    assert config.history.enabled is False
    assert Path(config.resolved_roots()[0]).is_absolute()


@pytest.mark.parametrize(
    "text",
    [
        "[scan]\nmax_depht = 3\n",  # typo must not be silently ignored
        "[scan]\nmin_confidence = 'certain'\n",
        "[scan]\nmax_depth = -1\n",
        "[detectors]\nenabled = ['cobol']\n",
        "[detectors.go]\nartifact_dirs = ['../bin']\n",
        "[[detectors.custom]]\nname = 'x'\ndir_names = ['a/b']\n",
        "[server]\nhost = '0.0.0.0'\n",
        "this is not toml",
    ],
)
def test_invalid_config_is_an_error(text: str) -> None:
    with pytest.raises(ConfigError):
        parse_config(text)


def test_save_round_trips(tmp_path: Path) -> None:
    config = SiftConfig()
    config.scan.exclude = ["vendor/"]
    config.scan.max_depth = 3
    config.history.enabled = False
    target = save_config(config, tmp_path / "nested" / "config.toml")
    assert load_config(target) == config


def test_paths_honour_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIFT_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("SIFT_DATA_DIR", str(tmp_path / "data"))
    assert config_path() == tmp_path / "cfg" / "config.toml"
    assert data_dir() == tmp_path / "data"


def test_default_config_location(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIFT_CONFIG_DIR", raising=False)
    assert config_path() == Path.home() / ".config" / "sift" / "config.toml"
