"""Go: build output directories named in the config.

Go has no standard in-project artifact directory (``go build`` writes a binary wherever it
is told to, and the build cache lives outside the project). So this detector does nothing
until the user lists directory names under ``[detectors.go] artifact_dirs``, and even then
only matches them next to a ``go.mod``.
"""

from __future__ import annotations

from sift.core.config import GoConfig
from sift.core.detectors.base import Detector, Rule
from sift.core.models import Ecosystem


def build(config: GoConfig) -> Detector:
    rules = tuple(
        Rule(
            id=f"go.{name}",
            names=(name,),
            ecosystem=Ecosystem.GO,
            category="build-output",
            description=f"Go build output directory ({name}/, from your Sift config)",
            manifests=("go.mod",),
        )
        for name in dict.fromkeys(config.artifact_dirs)
    )
    return Detector(name="go", ecosystem=Ecosystem.GO, rules=rules)
