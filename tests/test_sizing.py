from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from conftest import make_junction
from sift.core import sizing
from sift.core.models import SizeInfo, SizeMethod
from sift.core.sizing import cluster_size, extended_path, measure, measure_all


def write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_logical_size_and_counts_are_exact(tmp_path: Path) -> None:
    write(tmp_path / "a.bin", 1)
    write(tmp_path / "sub" / "b.bin", 4096)
    write(tmp_path / "sub" / "deeper" / "c.bin", 10_000)
    (tmp_path / "empty").mkdir()
    info = measure(str(tmp_path))
    assert info.logical_bytes == 14_097
    assert info.file_count == 3
    assert info.dir_count == 3
    assert info.link_count == 0
    assert info.error_count == 0
    assert info.complete


def test_empty_directory(tmp_path: Path) -> None:
    info = measure(str(tmp_path))
    assert (info.logical_bytes, info.file_count, info.dir_count) == (0, 0, 0)
    assert info.complete


def test_allocated_size_is_distinct_from_logical(tmp_path: Path) -> None:
    """One-byte files occupy far more disk than they contain."""
    for index in range(20):
        write(tmp_path / f"tiny-{index}", 1)
    info = measure(str(tmp_path))
    assert info.logical_bytes == 20
    if sys.platform == "win32":
        cluster = cluster_size(str(tmp_path))
        assert cluster and cluster >= 512
        assert info.allocated_method is SizeMethod.ESTIMATED
        assert info.allocated_bytes == 20 * cluster
    else:
        assert info.allocated_method is SizeMethod.EXACT
        assert info.allocated_bytes is not None
        assert info.allocated_bytes % 512 == 0
        assert info.allocated_bytes >= 20 * 512


@pytest.mark.skipif(sys.platform != "win32", reason="cluster rounding is the Windows path")
def test_windows_estimate_rounds_each_file_to_a_cluster(tmp_path: Path) -> None:
    cluster = cluster_size(str(tmp_path))
    assert cluster
    write(tmp_path / "exact", cluster)
    write(tmp_path / "over", cluster + 1)
    write(tmp_path / "zero", 0)
    info = measure(str(tmp_path))
    assert info.allocated_bytes == cluster + 2 * cluster + 0


@pytest.mark.skipif(sys.platform == "win32", reason="hard links are deduplicated on POSIX only")
def test_hard_links_are_allocated_once(tmp_path: Path) -> None:
    write(tmp_path / "original", 64 * 1024)
    alone = measure(str(tmp_path))
    os.link(tmp_path / "original", tmp_path / "second-name")
    linked = measure(str(tmp_path))
    assert linked.file_count == 2
    assert linked.logical_bytes == 2 * 64 * 1024
    assert linked.allocated_bytes == alone.allocated_bytes


def test_single_file(tmp_path: Path) -> None:
    write(tmp_path / ".coverage", 5000)
    info = measure(str(tmp_path / ".coverage"), "file")
    assert info.logical_bytes == 5000
    assert info.file_count == 1
    assert info.complete
    assert info.allocated_bytes is None or info.allocated_bytes >= 4096


def test_missing_path_is_incomplete_not_a_crash(tmp_path: Path) -> None:
    info = measure(str(tmp_path / "gone"))
    assert not info.complete
    assert info.error_count == 1
    assert info.logical_bytes == 0
    missing_file = measure(str(tmp_path / "gone.txt"), "file")
    assert not missing_file.complete


@pytest.mark.usefixtures("symlinks")
def test_symlinks_are_counted_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    write(outside / "big.bin", 100_000)
    tree = tmp_path / "tree"
    write(tree / "real.bin", 10)
    os.symlink(outside, tree / "dir-link", target_is_directory=True)
    os.symlink(outside / "big.bin", tree / "file-link")
    os.symlink(tmp_path / "missing", tree / "broken")
    info = measure(str(tree))
    assert info.logical_bytes == 10
    assert info.file_count == 1
    assert info.link_count == 3
    assert info.complete


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows feature")
def test_junctions_are_counted_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    write(outside / "big.bin", 100_000)
    tree = tmp_path / "tree"
    write(tree / "real.bin", 10)
    make_junction(tree / "junction", outside)
    info = measure(str(tree))
    assert info.logical_bytes == 10
    assert info.link_count == 1
    assert info.dir_count == 0


def test_wide_and_deep_tree_is_measured_in_parallel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enough directories that the walk is shared out in chunks.

    The answer must be identical whether the chunks are walked by the two in-process
    threads or by helper processes.
    """
    expected_bytes = 0
    for a in range(12):
        for b in range(12):
            for c in range(3):
                size = a + b + c + 1
                write(tmp_path / f"a{a}" / f"b{b}" / f"c{c}" / "f.bin", size)
                expected_bytes += size
    monkeypatch.setattr(sizing, "_BUDGET", 20)  # tiny chunks, so there is plenty to share
    monkeypatch.setattr(sizing, "_CHUNK_DIRS", 4)
    modes: list[dict[str, object]] = [
        {"workers": 0},
        {"workers": 1, "helper_delay": 0.0},
        {"workers": 3, "helper_delay": 0.0},
    ]
    for mode in modes:
        info = measure_all([(str(tmp_path), "directory")], **mode)[str(tmp_path)]  # type: ignore[arg-type]
        assert info is not None, mode
        assert info.logical_bytes == expected_bytes, mode
        assert info.file_count == 12 * 12 * 3, mode
        assert info.dir_count == 12 + 12 * 12 + 12 * 12 * 3, mode
        assert info.complete, mode


def test_helper_processes_really_do_the_walking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(200):
        write(tmp_path / f"d{index:03d}" / "f.bin", 7)
    monkeypatch.setattr(sizing, "_BUDGET", 5)
    monkeypatch.setattr(sizing, "_CHUNK_DIRS", 2)
    monkeypatch.setattr(sizing, "_LOCAL_THREADS", 0)  # nothing can happen without a helper
    served = 0
    real = sizing._Helper.walk

    def counting(self: sizing._Helper, stack: list[str], cluster: int, budget: int) -> object:
        nonlocal served
        served += 1
        return real(self, stack, cluster, budget)

    monkeypatch.setattr(sizing._Helper, "walk", counting)
    info = measure_all([(str(tmp_path), "directory")], workers=2, helper_delay=0.0)[str(tmp_path)]
    assert info is not None
    assert (info.file_count, info.logical_bytes, info.dir_count) == (200, 1400, 200)
    assert served > 10


def test_dead_helper_falls_back_to_walking_in_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(60):
        write(tmp_path / f"d{index:02d}" / "f.bin", 3)
    monkeypatch.setattr(sizing, "_BUDGET", 5)
    monkeypatch.setattr(sizing, "_CHUNK_DIRS", 2)

    def broken(self: sizing._Helper, stack: list[str], cluster: int, budget: int) -> object:
        raise RuntimeError("the helper process exited")

    monkeypatch.setattr(sizing._Helper, "walk", broken)
    info = measure_all([(str(tmp_path), "directory")], workers=2, helper_delay=0.0)[str(tmp_path)]
    assert info is not None
    assert (info.file_count, info.logical_bytes) == (60, 180)
    assert info.complete


def test_helper_that_cannot_start_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path / "f.bin", 9)

    def no_process(*args: object, **kwargs: object) -> object:
        raise OSError("cannot spawn")

    monkeypatch.setattr(sizing.subprocess, "Popen", no_process)
    info = measure_all([(str(tmp_path), "directory")], workers=4, helper_delay=0.0)[str(tmp_path)]
    assert info is not None and info.logical_bytes == 9


def test_walker_runs_standalone_with_only_the_standard_library(tmp_path: Path) -> None:
    """The helper is launched with -I -S, so it must not import anything from Sift."""
    import json
    import subprocess

    from sift.core import _walker

    write(tmp_path / "a" / "one.bin", 10)
    write(tmp_path / "a" / "b" / "two.bin", 20)
    request = json.dumps([[str(tmp_path)], 0, 1000]).encode("ascii") + b"\n"
    done = subprocess.run(
        [sys.executable, "-I", "-S", _walker.__file__],
        input=request,
        capture_output=True,
        timeout=60,
        check=True,
    )
    logical, _allocated, files, dirs, links, errors, leftover, _hardlinks = json.loads(done.stdout)
    assert (logical, files, dirs, links, errors, leftover) == (30, 2, 2, 0, 0, [])


def test_many_targets_at_once(tmp_path: Path) -> None:
    targets: list[tuple[str, sizing.Kind]] = []
    for index in range(30):
        write(tmp_path / f"d{index}" / "f.bin", index)
        targets.append((str(tmp_path / f"d{index}"), "directory"))
    write(tmp_path / "single.bin", 77)
    targets.append((str(tmp_path / "single.bin"), "file"))

    seen: dict[str, SizeInfo | None] = {}
    results = measure_all(targets, on_measured=seen.__setitem__)
    assert len(results) == 31
    for index in range(30):
        info = results[str(tmp_path / f"d{index}")]
        assert info is not None and info.logical_bytes == index
    single = results[str(tmp_path / "single.bin")]
    assert single is not None and single.logical_bytes == 77
    assert seen == results


def test_expired_budget_returns_none_rather_than_a_partial_number(tmp_path: Path) -> None:
    for index in range(50):
        write(tmp_path / f"d{index}" / "f.bin", 10)
    results = measure_all([(str(tmp_path), "directory")], time_budget=1e-9)
    assert results == {str(tmp_path): None}


@pytest.mark.skipif(sys.platform != "win32", reason="MAX_PATH is a Windows limit")
def test_paths_longer_than_260_characters(tmp_path: Path) -> None:
    deep = tmp_path
    while len(str(deep)) < 320:
        deep = deep / ("d" * 40)
    os.makedirs(extended_path(str(deep)))
    with open(extended_path(str(deep / "file.bin")), "wb") as handle:
        handle.write(b"x" * 1234)
    info = measure(str(tmp_path))
    assert info.logical_bytes == 1234
    assert info.file_count == 1
    assert info.complete


def test_extended_path_is_idempotent() -> None:
    once = extended_path(os.getcwd())
    assert extended_path(once) == once
    if sys.platform == "win32":
        assert once.startswith("\\\\?\\")
        assert extended_path("\\\\server\\share\\x") == "\\\\?\\UNC\\server\\share\\x"
    else:
        assert once == os.getcwd()
