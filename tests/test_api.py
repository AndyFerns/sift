"""The HTTP API, exercised through FastAPI's test client against real directories."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import tree_snapshot
from fixture_workspace import Workspace
from sift import __version__
from sift.api.app import create_app
from sift.core.config import SiftConfig, config_path, load_config
from sift.core.models import ScanOptions
from sift.core.scanner import scan
from sift.core.service import SiftService

BASE = "http://127.0.0.1:8765"


def client_for(*roots: Path, static_dir: Path | None = None, **kwargs: object) -> TestClient:
    service = SiftService(roots=[str(r) for r in roots], **kwargs)  # type: ignore[arg-type]
    missing = Path(__file__).parent / "no-such-static-dir"
    return TestClient(create_app(service, static_dir=static_dir or missing), base_url=BASE)


@pytest.fixture
def api(workspace_ro: Workspace) -> TestClient:
    return client_for(workspace_ro.root)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    base = tmp_path / "work"
    rust = base / "rust"
    (rust / "target" / "debug").mkdir(parents=True)
    (rust / "Cargo.toml").write_text("[package]\n")
    (rust / "target" / "CACHEDIR.TAG").write_bytes(b"x" * 43)
    (rust / "target" / "debug" / "app").write_bytes(b"x" * 5000)
    node = base / "node"
    (node / "node_modules" / "dep").mkdir(parents=True)
    (node / "package.json").write_text("{}")
    (node / "node_modules" / "dep" / "index.js").write_bytes(b"x" * 3000)
    (base / "misc" / "build").mkdir(parents=True)
    (base / "misc" / "build" / "important.txt").write_text("hand written")
    return base


def scan_via(api: TestClient, path: Path, **body: object) -> dict:
    response = api.post("/api/scans", json={"path": str(path), **body})
    assert response.status_code == 200, response.text
    return response.json()


def ids_by_path(scan_data: dict) -> dict[str, str]:
    return {a["relative_path"]: a["id"] for a in scan_data["artifacts"]}


# --- health and config ------------------------------------------------------------------


def test_health(api: TestClient) -> None:
    assert api.get("/api/health").json() == {"status": "ok", "version": __version__, "roots": 1}


def test_config_reports_roots_and_settings(api: TestClient, workspace_ro: Workspace) -> None:
    data = api.get("/api/config").json()
    assert [Path(r) for r in data["roots"]] == [workspace_ro.root.resolve()]
    assert data["config_path"] == str(config_path())
    assert data["config_exists"] is False
    assert data["config"]["scan"]["min_confidence"] == "medium"
    assert "rust" in data["available_detectors"]


def test_settings_can_be_changed_and_are_saved(api: TestClient, workspace_ro: Workspace) -> None:
    response = api.put(
        "/api/config",
        json={
            "exclude": ["node-app/"],
            "max_depth": 3,
            "min_confidence": "high",
            "detectors_enabled": ["rust", "node", "go"],
            "go_artifact_dirs": ["bin"],
            "history_enabled": False,
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["config_exists"] is True
    assert data["config"]["scan"]["exclude"] == ["node-app/"]
    assert data["config"]["scan"]["max_depth"] == 3
    saved = load_config()
    assert saved.scan.min_confidence.value == "high"
    assert saved.detectors.enabled == ["rust", "node", "go"]
    assert saved.history.enabled is False

    # the new settings apply to the next scan
    paths = set(ids_by_path(scan_via(api, workspace_ro.root)))
    assert "go-app/bin" in paths
    assert "rust-app/target" in paths
    assert not [p for p in paths if p.startswith(("node-app/", "py-app/"))]

    # a depth limit can be removed again by sending null
    cleared = api.put("/api/config", json={"max_depth": None}).json()
    assert cleared["config"]["scan"]["max_depth"] is None
    assert cleared["config"]["scan"]["exclude"] == ["node-app/"]  # untouched


def test_scan_roots_cannot_be_changed_through_the_api(api: TestClient, tmp_path: Path) -> None:
    before = api.get("/api/config").json()["roots"]
    response = api.put("/api/config", json={"roots": [str(tmp_path)]})
    assert response.status_code == 422
    assert response.json()["code"] == "invalid-request"
    assert api.get("/api/config").json()["roots"] == before
    assert not config_path().exists()


@pytest.mark.parametrize(
    "body",
    [
        {"detectors_enabled": ["cobol"]},
        {"go_artifact_dirs": ["../bin"]},
        {"min_confidence": "certain"},
        {"max_depth": -1},
    ],
)
def test_invalid_settings_are_rejected(api: TestClient, body: dict) -> None:
    response = api.put("/api/config", json=body)
    assert response.status_code in (400, 422)
    assert response.json()["detail"]
    assert not config_path().exists()


# --- scanning ---------------------------------------------------------------------------


def test_scan_matches_the_core_scanner(api: TestClient, workspace_ro: Workspace) -> None:
    data = scan_via(api, workspace_ro.root, recursive=True)
    core = scan(workspace_ro.root, ScanOptions(recursive=True))
    assert data["root"] == core.root
    assert data["totals"] == json.loads(core.totals.model_dump_json())
    assert [a["id"] for a in data["artifacts"]] == [a.id for a in core.artifacts]
    for got, want in zip(data["artifacts"], core.artifacts, strict=True):
        assert want.size is not None
        assert (
            got["path"],
            got["rule"],
            got["confidence"],
            got["ambiguous"],
            got["cleanable"],
        ) == (
            want.path,
            want.rule,
            want.confidence.value,
            want.ambiguous,
            want.cleanable,
        )
        assert got["size"]["logical_bytes"] == want.size.logical_bytes
        assert got["size"]["allocated_bytes"] == want.size.allocated_bytes
        assert got["size"]["file_count"] == want.size.file_count


def test_scan_fixture_expectations(api: TestClient, workspace_ro: Workspace) -> None:
    data = scan_via(api, workspace_ro.root)
    expected = workspace_ro.expect(recursive=False)
    got = {a["relative_path"]: a for a in data["artifacts"]}
    assert set(got) == set(expected)
    for relative, want in expected.items():
        assert got[relative]["rule"] == want.rule
        assert got[relative]["size"]["logical_bytes"] == want.logical_bytes
        assert got[relative]["cleanable"] is want.cleanable
    assert [i["kind"] for i in data["issues"]] == ["nested-repository"]


def test_scan_a_subdirectory_of_a_root(api: TestClient, workspace_ro: Workspace) -> None:
    data = scan_via(api, workspace_ro.root / "py-app", max_depth=0)
    assert Path(data["root"]) == workspace_ro.root.resolve() / "py-app"
    assert "src/pkg/__pycache__" not in ids_by_path(data)
    assert ".venv" in ids_by_path(data)


def test_scan_outside_the_roots_is_forbidden(
    api: TestClient, workspace_ro: Workspace, tmp_path: Path
) -> None:
    (tmp_path / "elsewhere").mkdir()
    sneaky = str(workspace_ro.root / "rust-app" / ".." / ".." / "..")
    for path in (tmp_path / "elsewhere", workspace_ro.root.parent, Path.home(), sneaky):
        response = api.post("/api/scans", json={"path": str(path)})
        assert response.status_code == 403, path
        assert response.json()["code"] == "outside-roots"
    assert api.get("/api/scans").json() == []


def test_scan_missing_path(api: TestClient, workspace_ro: Workspace) -> None:
    response = api.post("/api/scans", json={"path": str(workspace_ro.root / "nope")})
    assert response.status_code == 404
    assert response.json()["code"] == "path-not-found"


def test_scan_without_any_roots_is_refused(tmp_path: Path) -> None:
    api = client_for()
    assert api.get("/api/health").json()["roots"] == 0
    response = api.post("/api/scans", json={"path": str(tmp_path)})
    assert response.status_code == 403
    assert response.json()["code"] == "no-roots"
    assert "sift serve --root" in response.json()["detail"]


def test_scan_request_validation(api: TestClient, workspace_ro: Workspace) -> None:
    assert api.post("/api/scans", json={}).status_code == 422
    bad_depth = api.post("/api/scans", json={"path": str(workspace_ro.root), "max_depth": -2})
    assert bad_depth.status_code == 422
    unknown = api.post("/api/scans", json={"path": str(workspace_ro.root), "follow_links": True})
    assert unknown.status_code == 422


def test_scanning_changes_nothing_on_disk(workspace: Workspace) -> None:
    api = client_for(workspace.root)
    before = tree_snapshot(workspace.root)
    data = scan_via(api, workspace.root, recursive=True)
    for artifact in data["artifacts"][:5]:
        api.get(f"/api/scans/{data['id']}/artifacts/{artifact['id']}")
    api.get(f"/api/scans/{data['id']}/report?format=csv")
    assert tree_snapshot(workspace.root) == before


def test_stored_scans(api: TestClient, workspace_ro: Workspace) -> None:
    assert api.get("/api/scans/latest").status_code == 404
    first = scan_via(api, workspace_ro.root / "rust-app")
    second = scan_via(api, workspace_ro.root / "node-app")
    listed = api.get("/api/scans").json()
    assert [s["id"] for s in listed] == [second["id"], first["id"]]
    assert listed[0]["artifact_count"] == len(second["artifacts"])
    assert api.get("/api/scans/latest").json()["id"] == second["id"]
    assert api.get(f"/api/scans/{first['id']}").json() == first
    missing = api.get("/api/scans/nope")
    assert missing.status_code == 404 and missing.json()["code"] == "scan-not-found"


def test_scans_outside_the_roots_are_invisible(workspace_ro: Workspace, tmp_path: Path) -> None:
    """History is shared with the CLI, which may scan anywhere. The API must not expose it."""
    (tmp_path / "private" / "node_modules" / "dep").mkdir(parents=True)
    (tmp_path / "private" / "package.json").write_text("{}")
    private = SiftService().scan(tmp_path / "private")

    api = client_for(workspace_ro.root)
    visible = scan_via(api, workspace_ro.root / "rust-app")
    assert [s["id"] for s in api.get("/api/scans").json()] == [visible["id"]]
    assert [s["id"] for s in api.get("/api/history").json()["scans"]] == [visible["id"]]
    assert api.get("/api/scans/latest").json()["id"] == visible["id"]
    for url in (
        f"/api/scans/{private.id}",
        f"/api/scans/{private.id}/artifacts/{private.artifacts[0].id}",
        f"/api/scans/{private.id}/report",
    ):
        response = api.get(url)
        assert response.status_code == 403, url
        assert response.json()["code"] == "outside-roots"
    preview = api.post(
        "/api/cleanup/preview",
        json={"scan_id": private.id, "artifact_ids": [private.artifacts[0].id]},
    )
    assert preview.status_code == 403
    assert (tmp_path / "private" / "node_modules").exists()


# --- artifacts and reports --------------------------------------------------------------


def test_artifact_detail(api: TestClient, workspace_ro: Workspace) -> None:
    data = scan_via(api, workspace_ro.root)
    ids = ids_by_path(data)
    detail = api.get(f"/api/scans/{data['id']}/artifacts/{ids['rust-app/target']}").json()
    assert detail["artifact"]["relative_path"] == "rust-app/target"
    assert detail["exists"] is True and detail["still_matches"] is True
    assert detail["current_size"]["logical_bytes"] == 16307
    assert detail["cleanup_allowed"] is True
    assert [c["name"] for c in detail["children"]] == ["debug", ".rustc_info.json", "CACHEDIR.TAG"]

    refused = api.get(f"/api/scans/{data['id']}/artifacts/{ids['ambiguous/build']}").json()
    assert refused["cleanup_allowed"] is False
    assert "ambiguous" in refused["cleanup_refusal"]

    missing = api.get(f"/api/scans/{data['id']}/artifacts/000000000000")
    assert missing.status_code == 404 and missing.json()["code"] == "artifact-not-found"


def test_report_export(api: TestClient, workspace_ro: Workspace) -> None:
    data = scan_via(api, workspace_ro.root)
    as_json = api.get(f"/api/scans/{data['id']}/report")
    assert as_json.headers["content-type"].startswith("application/json")
    assert (
        as_json.headers["content-disposition"] == f'attachment; filename="sift-{data["id"]}.json"'
    )
    assert as_json.json() == data

    as_csv = api.get(f"/api/scans/{data['id']}/report?format=csv")
    assert as_csv.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(as_csv.text)))
    assert [r["id"] for r in rows] == [a["id"] for a in data["artifacts"]]
    assert sum(int(r["logical_bytes"]) for r in rows) == data["totals"]["logical_bytes"]

    assert api.get(f"/api/scans/{data['id']}/report?format=xml").status_code == 422


# --- cleanup ----------------------------------------------------------------------------


def preview(api: TestClient, scan_data: dict, *paths: str, **extra: object) -> dict:
    ids = ids_by_path(scan_data)
    response = api.post(
        "/api/cleanup/preview",
        json={"scan_id": scan_data["id"], "artifact_ids": [ids[p] for p in paths], **extra},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_preview_lists_exactly_the_selection_and_deletes_nothing(project: Path) -> None:
    api = client_for(project)
    data = scan_via(api, project)
    before = tree_snapshot(project)
    plan = preview(api, data, "rust/target", "misc/build")
    assert tree_snapshot(project) == before
    items = {i["relative_path"]: i for i in plan["items"]}
    assert set(items) == {"rust/target", "misc/build"}
    assert items["rust/target"]["allowed"] is True
    assert Path(items["rust/target"]["path"]) == project.resolve() / "rust" / "target"
    assert items["rust/target"]["logical_bytes"] == 5043
    assert items["misc/build"]["allowed"] is False
    assert items["misc/build"]["refusal_code"] == "ambiguous"
    totals = plan["totals"]
    assert (totals["detected_bytes"], totals["selected_bytes"], totals["deletable_bytes"]) == (
        8055,
        5055,
        5043,
    )
    assert plan["confirmation_token"] and plan["dry_run"] is False


@pytest.mark.parametrize(
    "confirm",
    [
        {},
        {"confirmation": "delete"},  # the word without the token
        {"confirmation_token": "TOKEN"},  # the token without the word
        {"confirmation_token": "TOKEN", "confirmation": "yes"},
        {"confirmation_token": "0" * 64, "confirmation": "delete"},
    ],
)
def test_execute_needs_both_the_token_and_the_typed_word(project: Path, confirm: dict) -> None:
    api = client_for(project)
    plan = preview(api, scan_via(api, project), "rust/target", "node/node_modules")
    before = tree_snapshot(project)
    body = {k: plan["confirmation_token"] if v == "TOKEN" else v for k, v in confirm.items()}
    response = api.post("/api/cleanup/execute", json={"plan_id": plan["id"], **body})
    assert response.status_code == 409
    assert response.json()["code"] == "not-confirmed"
    assert tree_snapshot(project) == before


def test_confirmed_cleanup_deletes_the_selection_only(project: Path) -> None:
    api = client_for(project)
    data = scan_via(api, project)
    plan = preview(api, data, "rust/target", "misc/build")
    response = api.post(
        "/api/cleanup/execute",
        json={
            "plan_id": plan["id"],
            "confirmation_token": plan["confirmation_token"],
            "confirmation": " Delete ",
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert {i["relative_path"]: i["status"] for i in result["items"]} == {
        "rust/target": "deleted",
        "misc/build": "refused",
    }
    totals = result["totals"]
    assert (totals["detected_bytes"], totals["selected_bytes"]) == (8055, 5055)
    assert (totals["deleted_bytes"], totals["failed_bytes"], totals["refused_bytes"]) == (
        5043,
        0,
        12,
    )
    assert not (project / "rust" / "target").exists()
    assert (project / "rust" / "Cargo.toml").exists()
    assert (project / "node" / "node_modules" / "dep" / "index.js").exists()
    assert (project / "misc" / "build" / "important.txt").read_text() == "hand written"

    # the preview is spent
    again = api.post(
        "/api/cleanup/execute",
        json={
            "plan_id": plan["id"],
            "confirmation_token": plan["confirmation_token"],
            "confirmation": "delete",
        },
    )
    assert again.status_code == 404 and again.json()["code"] == "plan-not-found"

    history = api.get("/api/history").json()
    assert history["enabled"] is True
    assert [(c["id"], c["deleted_bytes"], c["cancelled"]) for c in history["cleanups"]] == [
        (result["id"], 5043, False)
    ]
    assert api.get(f"/api/cleanups/{result['id']}").json() == result
    assert api.get("/api/cleanups/nope").status_code == 404


def test_cancelling_a_preview(project: Path) -> None:
    api = client_for(project)
    plan = preview(api, scan_via(api, project), "rust/target")
    before = tree_snapshot(project)
    response = api.post("/api/cleanup/cancel", json={"plan_id": plan["id"]})
    assert response.status_code == 200
    result = response.json()
    assert result["cancelled"] is True
    assert result["totals"]["deleted_bytes"] == 0
    assert [i["status"] for i in result["items"]] == ["cancelled"]
    assert tree_snapshot(project) == before
    # a cancelled preview cannot be executed afterwards
    late = api.post(
        "/api/cleanup/execute",
        json={
            "plan_id": plan["id"],
            "confirmation_token": plan["confirmation_token"],
            "confirmation": "delete",
        },
    )
    assert late.status_code == 404
    assert tree_snapshot(project) == before
    assert api.get("/api/history").json()["cleanups"][0]["cancelled"] is True


def test_dry_run(project: Path) -> None:
    api = client_for(project)
    data = scan_via(api, project)
    plan = preview(api, data, "rust/target", "node/node_modules", dry_run=True)
    before = tree_snapshot(project)
    response = api.post("/api/cleanup/execute", json={"plan_id": plan["id"]})
    assert response.status_code == 200
    result = response.json()
    assert result["dry_run"] is True
    assert {i["status"] for i in result["items"]} == {"dry-run"}
    assert result["totals"]["would_delete_bytes"] == 8043
    assert result["totals"]["deleted_bytes"] == 0
    assert tree_snapshot(project) == before


def test_artifact_changed_after_the_preview_is_refused(project: Path) -> None:
    api = client_for(project)
    plan = preview(api, scan_via(api, project), "node/node_modules")
    (project / "node" / "package.json").unlink()
    response = api.post(
        "/api/cleanup/execute",
        json={
            "plan_id": plan["id"],
            "confirmation_token": plan["confirmation_token"],
            "confirmation": "delete",
        },
    )
    assert response.status_code == 200
    assert [i["status"] for i in response.json()["items"]] == ["refused"]
    assert (project / "node" / "node_modules" / "dep" / "index.js").exists()


def test_cleanup_request_errors(project: Path) -> None:
    api = client_for(project)
    data = scan_via(api, project)
    unknown = api.post(
        "/api/cleanup/preview", json={"scan_id": data["id"], "artifact_ids": ["000000000000"]}
    )
    assert unknown.status_code == 404 and unknown.json()["code"] == "artifact-not-found"
    empty = api.post("/api/cleanup/preview", json={"scan_id": data["id"], "artifact_ids": []})
    assert empty.status_code == 422
    no_scan = api.post("/api/cleanup/preview", json={"scan_id": "nope", "artifact_ids": ["x"]})
    assert no_scan.status_code == 404
    no_plan = api.post("/api/cleanup/execute", json={"plan_id": "nope", "confirmation": "delete"})
    assert no_plan.status_code == 404 and no_plan.json()["code"] == "plan-not-found"
    assert api.post("/api/cleanup/cancel", json={"plan_id": "nope"}).status_code == 404


def test_history_can_be_disabled(project: Path) -> None:
    config = SiftConfig()
    config.history.enabled = False
    api = client_for(project, config=config)
    data = scan_via(api, project)
    history = api.get("/api/history").json()
    assert history["enabled"] is False
    assert [s["id"] for s in history["scans"]] == [data["id"]]  # this session only
    assert history["cleanups"] == []
    assert api.get(f"/api/scans/{data['id']}").status_code == 200


# --- keeping the API local --------------------------------------------------------------


def test_requests_for_other_host_names_are_rejected(api: TestClient) -> None:
    for host in (
        "evil.example",
        "evil.example:8765",
        "192.168.1.20:8765",
        "127.0.0.1.evil.example",
    ):
        response = api.get("/api/health", headers={"host": host})
        assert response.status_code == 400, host
        assert response.json()["code"] == "bad-host"
    for host in ("127.0.0.1:8765", "localhost:8765", "localhost", "[::1]:8765"):
        assert api.get("/api/health", headers={"host": host}).status_code == 200, host


def test_cross_site_writes_are_rejected(api: TestClient, workspace_ro: Workspace) -> None:
    body = {"path": str(workspace_ro.root / "rust-app")}
    for headers in (
        {"origin": "https://evil.example"},
        {"origin": "http://127.0.0.1.evil.example:8765"},
        {"origin": "null"},
        {"sec-fetch-site": "cross-site"},
    ):
        response = api.post("/api/scans", json=body, headers=headers)
        assert response.status_code == 403, headers
        assert response.json()["code"] == "cross-origin"
    assert api.get("/api/scans").json() == []

    for origin in ("http://127.0.0.1:8765", "http://localhost:5173"):
        assert api.post("/api/scans", json=body, headers={"origin": origin}).status_code == 200


def test_writes_must_be_json(api: TestClient, workspace_ro: Workspace) -> None:
    """HTML forms can post cross-site without a preflight, but they cannot send JSON."""
    path = str(workspace_ro.root / "rust-app")
    form = api.post("/api/scans", data={"path": path})
    assert form.status_code == 415 and form.json()["code"] == "not-json"
    text = api.post(
        "/api/scans", content=json.dumps({"path": path}), headers={"content-type": "text/plain"}
    )
    assert text.status_code == 415
    assert api.get("/api/scans").json() == []


def test_no_cors_permission_is_ever_given(api: TestClient) -> None:
    preflight = api.options(
        "/api/scans",
        headers={
            "origin": "https://evil.example",
            "access-control-request-method": "POST",
            "access-control-request-headers": "content-type",
        },
    )
    assert "access-control-allow-origin" not in preflight.headers
    read = api.get("/api/health", headers={"origin": "https://evil.example"})
    assert "access-control-allow-origin" not in read.headers


def test_security_headers(api: TestClient) -> None:
    response = api.get("/api/health")
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_unknown_api_routes_return_json(api: TestClient) -> None:
    response = api.get("/api/nope")
    assert response.status_code == 404
    assert response.json() == {"detail": "no such endpoint: /api/nope", "code": "not-found"}


# --- the dashboard files ----------------------------------------------------------------


def test_without_a_built_dashboard_the_root_explains_itself(api: TestClient) -> None:
    response = api.get("/")
    assert response.status_code == 200
    assert "has not been built" in response.text
    assert "/api/docs" in response.text


def test_built_dashboard_is_served_with_spa_fallback(
    workspace_ro: Workspace, tmp_path: Path
) -> None:
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><title>Sift</title><div id=root></div>")
    (static / "assets" / "app.js").write_text("console.log('sift')")
    (static / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    api = client_for(workspace_ro.root, static_dir=static)

    for url in ("/", "/explorer", "/scans/abc/artifacts/def", "/history"):
        response = api.get(url)
        assert response.status_code == 200, url
        assert "<div id=root>" in response.text
        assert response.headers["content-type"].startswith("text/html")
    assert api.get("/assets/app.js").text == "console.log('sift')"
    assert api.get("/favicon.svg").text.startswith("<svg")
    assert api.get("/api/health").json()["status"] == "ok"
    assert api.get("/api/nope").status_code == 404
    assert api.get("/api/nope").headers["content-type"].startswith("application/json")
    # nothing outside the static directory can be fetched
    (tmp_path / "secret.txt").write_text("secret")
    for url in (
        "/../secret.txt",
        "/%2e%2e/secret.txt",
        "/..%2fsecret.txt",
        "/assets/../../secret.txt",
    ):
        assert "secret" not in api.get(url).text.replace("<title>Sift</title>", ""), url


def test_openapi_schema_describes_every_endpoint(api: TestClient) -> None:
    schema = api.get("/api/openapi.json").json()
    assert schema["info"]["title"] == "Sift"
    for path in (
        "/api/health",
        "/api/config",
        "/api/scans",
        "/api/scans/latest",
        "/api/scans/{scan_id}",
        "/api/scans/{scan_id}/artifacts/{artifact_id}",
        "/api/scans/{scan_id}/report",
        "/api/cleanup/preview",
        "/api/cleanup/execute",
        "/api/cleanup/cancel",
        "/api/history",
    ):
        assert path in schema["paths"], path
    assert {"ScanResult", "Artifact", "CleanupPlan", "CleanupResult", "ArtifactDetail"} <= set(
        schema["components"]["schemas"]
    )
