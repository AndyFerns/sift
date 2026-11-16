"""Java: Maven ``target/``, Gradle ``build/`` and the project ``.gradle/`` cache."""

from __future__ import annotations

from sift.core.detectors.base import Detector, Rule
from sift.core.models import Ecosystem

GRADLE_MANIFESTS = (
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
)

DETECTOR = Detector(
    name="java",
    ecosystem=Ecosystem.JAVA,
    rules=(
        Rule(
            id="java.maven-target",
            names=("target",),
            ecosystem=Ecosystem.JAVA,
            category="build-output",
            description="Maven build output (rebuilt by `mvn package`)",
            manifests=("pom.xml",),
            markers=(
                "maven-status",
                "maven-archiver",
                "classes",
                "test-classes",
                "generated-sources",
                "surefire-reports",
            ),
            # Folders called "classes" exist outside Maven too, so markers alone prove nothing.
            marker_only=None,
        ),
        Rule(
            id="java.gradle-build",
            names=("build",),
            ecosystem=Ecosystem.JAVA,
            category="build-output",
            description="Gradle build output (rebuilt by `gradle build`)",
            manifests=GRADLE_MANIFESTS,
            markers=(
                "classes",
                "libs",
                "tmp",
                "generated",
                "intermediates",
                "outputs",
                "reports",
                "test-results",
                "kotlin",
                "resources",
            ),
            marker_only=None,
        ),
        Rule(
            id="java.gradle-cache",
            names=(".gradle",),
            ecosystem=Ecosystem.JAVA,
            category="cache",
            description="Gradle project cache",
            manifests=(*GRADLE_MANIFESTS, "gradlew"),
            distinctive=True,
        ),
    ),
)
