"""Node.js: installed dependencies and framework build caches."""

from __future__ import annotations

from sift.core.detectors.base import Detector, Rule
from sift.core.models import Ecosystem

_PACKAGE = ("package.json",)

DETECTOR = Detector(
    name="node",
    ecosystem=Ecosystem.NODE,
    rules=(
        Rule(
            id="node.node_modules",
            names=("node_modules",),
            ecosystem=Ecosystem.NODE,
            category="dependencies",
            description="Installed npm packages (restored by your package manager's install)",
            manifests=_PACKAGE,
            markers=(
                ".package-lock.json",  # npm
                ".yarn-integrity",  # yarn classic
                ".yarn-state.yml",  # yarn berry
                ".modules.yaml",  # pnpm
                ".pnpm",
                ".bin",
            ),
            distinctive=True,
        ),
        Rule(
            id="node.next",
            names=(".next",),
            ecosystem=Ecosystem.NODE,
            category="build-output",
            description="Next.js build output and cache",
            manifests=_PACKAGE,
            manifest_prefixes=("next.config.",),
            markers=("BUILD_ID", "build-manifest.json", "cache", "server", "static"),
            distinctive=True,
        ),
        Rule(
            id="node.nuxt",
            names=(".nuxt",),
            ecosystem=Ecosystem.NODE,
            category="build-output",
            description="Nuxt build output",
            manifests=_PACKAGE,
            manifest_prefixes=("nuxt.config.",),
            distinctive=True,
        ),
        Rule(
            id="node.turbo",
            names=(".turbo",),
            ecosystem=Ecosystem.NODE,
            category="cache",
            description="Turborepo task cache",
            manifests=("turbo.json", "package.json"),
            distinctive=True,
        ),
        Rule(
            id="node.angular-cache",
            names=(".angular",),
            subpath="cache",
            ecosystem=Ecosystem.NODE,
            category="cache",
            description="Angular CLI build cache",
            manifests=("angular.json", "package.json"),
            distinctive=True,
        ),
    ),
)
