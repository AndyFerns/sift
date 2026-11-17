"""Record real API responses for the dashboard's tests.

    python scripts/record_fixture_scan.py

Builds the shared fixture workspace in a temporary directory, runs a recursive scan, an
artifact inspection and a cleanup preview through the real FastAPI app, and writes the
responses to ``web/src/test/fixture.json``. Nothing in that file is hand-written: the
dashboard tests render exactly what the scanner produced.

The only edit made to the responses is replacing the temporary directory's path with a
fixed one, so the file does not change every time it is regenerated on the same platform.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))

from fixture_workspace import build_workspace  # noqa: E402
from sift.api.app import create_app  # noqa: E402
from sift.core.service import SiftService  # noqa: E402

TARGET = ROOT / "web" / "src" / "test" / "fixture.json"
STABLE_ROOT = "C:\\sift-fixture" if os.name == "nt" else "/sift-fixture"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="sift-record-") as tmp:
        os.environ["SIFT_CONFIG_DIR"] = str(Path(tmp) / "config")
        os.environ["SIFT_DATA_DIR"] = str(Path(tmp) / "data")
        workspace = build_workspace(Path(tmp) / "fixture")
        service = SiftService(roots=[str(workspace.root)])
        api = TestClient(
            create_app(service, static_dir=Path(tmp) / "none"), base_url="http://127.0.0.1:8765"
        )

        config = api.get("/api/config").json()
        scan = api.post("/api/scans", json={"path": str(workspace.root), "recursive": True}).json()
        ids = {a["relative_path"]: a["id"] for a in scan["artifacts"]}
        detail = api.get(f"/api/scans/{scan['id']}/artifacts/{ids['rust-app/target']}").json()
        plan = api.post(
            "/api/cleanup/preview",
            json={
                "scan_id": scan["id"],
                "artifact_ids": [
                    ids["rust-app/target"],
                    ids["node-app/.next"],
                    ids["ambiguous/build"],
                ],
            },
        ).json()
        dry = api.post(
            "/api/cleanup/preview",
            json={"scan_id": scan["id"], "artifact_ids": [ids["rust-app/target"]], "dry_run": True},
        ).json()
        dry_result = api.post("/api/cleanup/execute", json={"plan_id": dry["id"]}).json()
        history = api.get("/api/history").json()

        recorded = {
            "_note": "Recorded from the real API by scripts/record_fixture_scan.py. Do not edit.",
            "config": config,
            "scan": scan,
            "detail": detail,
            "plan": plan,
            "dryRunResult": dry_result,
            "history": history,
        }
        text = json.dumps(recorded, indent=2)
        real = json.dumps(os.path.realpath(workspace.root))[1:-1]
        text = text.replace(real, json.dumps(STABLE_ROOT)[1:-1])
        text = text.replace(
            json.dumps(str(Path(tmp)))[1:-1], json.dumps(STABLE_ROOT + "-tmp")[1:-1]
        )

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(text + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {TARGET} ({len(scan['artifacts'])} artifacts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
