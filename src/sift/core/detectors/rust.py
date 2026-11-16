"""Rust: Cargo's ``target/`` directory."""

from __future__ import annotations

from sift.core.detectors.base import Detector, Rule
from sift.core.models import Ecosystem

DETECTOR = Detector(
    name="rust",
    ecosystem=Ecosystem.RUST,
    rules=(
        Rule(
            id="rust.target",
            names=("target",),
            ecosystem=Ecosystem.RUST,
            category="build-output",
            description="Cargo build output (rebuilt by `cargo build`)",
            manifests=("Cargo.toml",),
            # Cargo writes both of these into every target directory it creates.
            markers=(".rustc_info.json", "CACHEDIR.TAG"),
        ),
    ),
)
