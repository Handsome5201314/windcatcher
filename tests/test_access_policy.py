"""Route matrix for the public gateway's fail-closed authorization policy."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pipeline.access_policy import Policy, classify


CASE = "case-20261102-123456-abcd"
OPAQUE = "a" * 32
BATCH = "batch-" + "a" * 16
TOKEN = "A" * 43
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("case_id", ["synthetic", "synthetic-example_1"])
def test_existing_synthetic_case_ids_remain_accessible(case_id):
    assert classify(f"/api/cases/{case_id}/model", "GET") == Policy(
        "case", case_id=case_id, capability="view")


@pytest.mark.parametrize("path,method", [
    ("/", "GET"), ("/healthz", "GET"), ("/login", "GET"),
    ("/login", "POST"), ("/static/app.css", "GET"),
    ("/static/assets/logo-badge.svg", "GET"),
    ("/static/patient-view.js", "GET"), ("/static/cfd-viewer.js", "GET"),
    (f"/share/{TOKEN}", "GET"), (f"/share/{TOKEN}/model", "GET"),
    (f"/share/{TOKEN}/metadata", "GET"), (f"/share/{TOKEN}/unlock", "POST"),
    ("/references", "GET"), ("/api/references", "GET"),
    (f"/api/references/{OPAQUE}/model", "GET"),
])
def test_only_intended_patient_and_reference_paths_are_public(path, method):
    assert classify(path, method) == Policy("public")


@pytest.mark.parametrize("path,method,capability", [
    ("", "GET", "view"), ("", "DELETE", "owner"),
    ("/scenarios", "GET", "view"), ("/scenarios", "POST", "edit"),
    (f"/scenarios/{OPAQUE}", "GET", "view"),
    (f"/scenarios/{OPAQUE}/placement", "PUT", "edit"),
    (f"/scenarios/{OPAQUE}/review", "POST", "review"),
    (f"/scenarios/{OPAQUE}/cfd-prep", "POST", "edit"),
    (f"/scenarios/{OPAQUE}/cfd-run", "POST", "edit"),
    (f"/scenarios/{OPAQUE}/cfd-status", "GET", "view"),
    (f"/scenarios/{OPAQUE}/comparison", "GET", "view"),
    (f"/scenarios/{OPAQUE}/download", "GET", "view"),
    ("/segment", "POST", "edit"), ("/segment/import-mask", "POST", "edit"),
    ("/segment/edit-mask", "POST", "edit"), ("/segment/reset-mask", "POST", "edit"),
    ("/segment/remove-external-air", "POST", "edit"),
    ("/segment/undo-external-air", "POST", "edit"),
    ("/segment/mask-export", "GET", "view"),
    ("/segment/confirm", "POST", "review"),
    ("/segment/endplanes", "GET", "view"),
    ("/segment/ports/formal", "GET", "view"),
    ("/segment/endplanes/learning", "GET", "view"),
    ("/segment/endplanes/learning/propose", "POST", "view"),
    ("/segment/endplanes/learning/auto-propose", "POST", "view"),
    ("/segment/endplanes/learning/auto-propose-bilateral", "POST", "view"),
    ("/segment/endplanes/learning/feedback", "POST", "review"),
    ("/segment/selection-model", "GET", "view"),
    ("/segment/endplanes/section", "POST", "view"),
    ("/segment/endplanes/preview", "POST", "view"),
    ("/segment/endplanes/preview-model", "POST", "view"),
    ("/segment/endplanes", "POST", "edit"),
    ("/segment/endplanes/anterior", "POST", "edit"),
    ("/segment/endplanes/posterior", "POST", "edit"),
    ("/surface", "POST", "edit"),
    ("/cfd-prep", "POST", "edit"), ("/cfd-run", "POST", "edit"),
    ("/orchestration/advance", "POST", "edit"),
    ("/gate", "GET", "view"),
    ("/harness-advice", "POST", "view"),
    ("/cfd-postprocess", "POST", "edit"),
    ("/cfd-postprocess/cancel", "POST", "edit"),
    ("/cfd-results", "GET", "view"),
    ("/cfd-results/viewer", "GET", "view"),
    ("/cfd-sections", "GET", "view"),
    ("/cfd-sections", "POST", "edit"),
    ("/cfd-sections/preview", "POST", "view"),
    ("/cfd-sections/analyze", "POST", "edit"),
    ("/report", "GET", "view"),
    ("/ai-report", "GET", "view"), ("/ai-report", "POST", "edit"),
    ("/preview/axial", "GET", "view"),
    ("/viewer", "GET", "view"), ("/slice/coronal", "GET", "view"),
    ("/model", "GET", "view"),
    ("/report-file", "GET", "view"), ("/artifact", "GET", "view"),
    ("/review-events", "GET", "view"),
    ("/review-events", "POST", "review"),
    ("/cfd-reviews", "GET", "view"),
    ("/cfd-reviews", "POST", "review"),
    ("/shares", "GET", "view"), ("/shares", "POST", "review"),
    ("/shares/test", "POST", "review"),
    (f"/shares/{OPAQUE}/revoke", "POST", "review"),
    ("/grants", "GET", "manage"),
    (f"/grants/{OPAQUE}", "PUT", "manage"),
    (f"/grants/{OPAQUE}", "DELETE", "manage"),
])
def test_case_routes_require_explicit_capability(path, method, capability):
    assert classify(f"/api/cases/{CASE}{path}", method) == Policy(
        "case", case_id=CASE, capability=capability
    )


@pytest.mark.parametrize("path,method,expected", [
    ("/logout", "POST", Policy("session")),
    ("/api/admin/users", "GET", Policy("admin")),
    ("/api/admin/users", "POST", Policy("admin")),
    (f"/api/admin/users/{OPAQUE}/disable", "POST", Policy("admin")),
    ("/share-admin", "GET", Policy("session")),
    ("/reference-result", "GET", Policy("session")),
    ("/static/app.js", "GET", Policy("session")),
    ("/api/capabilities", "GET", Policy("session")),
    ("/api/cases", "GET", Policy("case_list")),
    ("/api/cases/upload", "POST", Policy("case_create")),
    ("/api/uploads", "POST", Policy("upload_create")),
    (f"/api/uploads/{OPAQUE}/chunks", "POST", Policy("upload", resource_id=OPAQUE, capability="owner")),
    (f"/api/uploads/{OPAQUE}/complete", "POST", Policy("upload", resource_id=OPAQUE, capability="owner")),
    (f"/api/uploads/{OPAQUE}/cancel", "POST", Policy("upload", resource_id=OPAQUE, capability="owner")),
    ("/api/reference-admin/cases", "GET", Policy("reference_candidates")),
    (f"/api/reference-admin/cases/{CASE}", "POST", Policy("case", case_id=CASE, capability="review")),
    (f"/api/reference-admin/{OPAQUE}/revoke", "POST", Policy("reference_revoke", resource_id=OPAQUE, capability="review")),
    ("/api/automation/status", "GET", Policy("monitor_read")),
    ("/api/automation/cases", "GET", Policy("automation_cases")),
    (f"/api/automation/cases/{CASE}", "GET", Policy("case", case_id=CASE, capability="view")),
    ("/api/automation/batches", "POST", Policy("automation_submit")),
    (f"/api/automation/batches/{BATCH}", "GET", Policy("automation_batch", resource_id=BATCH, capability="view")),
    (f"/api/automation/batches/{BATCH}/cancel", "POST", Policy("automation_batch", resource_id=BATCH, capability="edit")),
    ("/api/monitor/events", "POST", Policy("monitor_ingest")),
    ("/api/monitor/events", "GET", Policy("monitor_read")),
    ("/api/monitor/status", "GET", Policy("monitor_read")),
    ("/api/ai-report/config", "GET", Policy("provider")),
    ("/api/ai-report/config", "PUT", Policy("provider")),
    ("/api/ai-report/models", "POST", Policy("provider")),
    ("/api/operator/recipes", "GET", Policy("operator_recipes")),
    ("/api/operator/recipes", "POST", Policy("operator_recipes")),
    (f"/api/operator/recipes/recipe-{OPAQUE}", "GET", Policy("operator_recipes")),
    (f"/api/operator/recipes/recipe-{OPAQUE}", "PUT", Policy("operator_recipes")),
    (f"/api/operator/recipes/recipe-{OPAQUE}/revoke", "POST", Policy("operator_recipes")),
])
def test_non_case_resources_are_not_downgraded_to_plain_session(path, method, expected):
    assert classify(path, method) == expected


@pytest.mark.parametrize("path,method", [
    ("/api/cases", "POST"), ("/api/environment/installers", "GET"),
    ("/api/environment/install", "POST"),
    ("/api/environment/install/job", "GET"),
    ("/api/pick-directory", "GET"),
    (f"/api/cases/{CASE}/open-slicer", "POST"),
    ("/api/fetch", "POST"),
    ("/api/cases/../../secrets", "GET"),
    ("/api/cases/unlisted", "GET"),
    (f"/api/cases/{CASE}/future-output", "GET"),
    (f"/api/cases/{CASE}/harness-advice", "GET"),
    (f"/api/cases/{CASE}/orchestration/advance", "GET"),
    (f"/api/cases/{CASE}/shares", "DELETE"),
    ("/api/admin/users/invalid/disable", "POST"),
    (f"/api/cases/{CASE}/grants/invalid", "PUT"),
    (f"/api/cases/{CASE}/grants/{OPAQUE}", "POST"),
    (f"/api/uploads/{OPAQUE}/chunks", "GET"),
    (f"/api/reference-admin/{OPAQUE}/revoke", "GET"),
    (f"/api/operator/recipes/recipe-{OPAQUE}", "DELETE"),
    (f"/api/operator/recipes/recipe-{OPAQUE}/revoke", "GET"),
    ("/api/unknown", "GET"),
    (f"/share/{TOKEN}/model", "POST"),
    ("/share/short", "GET"),
])
def test_unknown_local_or_wrong_method_fails_closed(path, method):
    assert classify(path, method) == Policy("deny")


def test_head_and_delete_override_use_the_effective_method():
    assert classify(f"/api/cases/{CASE}", "HEAD") == Policy(
        "case", case_id=CASE, capability="view"
    )
    assert classify(f"/api/cases/{CASE}", "DELETE") == Policy(
        "case", case_id=CASE, capability="owner"
    )
    assert classify(f"/api/cases/{CASE}", "POST") == Policy("deny")


def test_every_declared_gateway_route_has_an_explicit_decision():
    replacements = {
        "case_id": CASE, "scenario_id": OPAQUE, "share_id": OPAQUE,
        "reference_id": OPAQUE, "upload_id": OPAQUE, "batch_id": BATCH,
        "user_id": OPAQUE, "recipe_id": "recipe-" + OPAQUE,
        "token": TOKEN, "view": "axial", "side": "anterior", "job_id": "job",
    }
    intentionally_local_only = {
        ("POST", "/api/cases"), ("GET", "/api/environment/installers"),
        ("POST", "/api/environment/install"),
        ("GET", "/api/environment/install/job"),
        ("GET", "/api/pick-directory"), ("POST", "/api/fetch"),
        ("POST", f"/api/cases/{CASE}/open-slicer"),
    }
    for source in (ROOT / "server.py", ROOT / "cloud.py", ROOT / "pipeline" / "reference_routes.py"):
        for line in source.read_text(encoding="utf-8").splitlines():
            match = re.search(r"@app\.(get|post|put|delete|patch)\(", line)
            if not match:
                continue
            quote = line[match.end()]
            assert quote in {"'", '"'}, (source.name, line)
            path = line[match.end() + 1:].split(quote, 1)[0]
            for name, value in replacements.items():
                path = path.replace("{" + name + "}", value)
            assert "{" not in path, (source.name, line)
            method = match.group(1).upper()
            policy = classify(path, method)
            if policy.kind == "deny":
                assert (method, path) in intentionally_local_only, (source.name, method, path)
