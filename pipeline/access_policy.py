"""Classify public-gateway routes before applying identity and case grants.

This module has no access to user accounts or case data. The gateway must
resolve indirect resource IDs and perform the returned authorization check
before invoking a route or any deletion side effect. Unknown API paths fail
closed, so adding an endpoint requires an explicit policy decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Policy:
    kind: str
    case_id: str | None = None
    resource_id: str | None = None
    capability: str | None = None


_CASE = r"(?:case-\d{8}-\d{6}-[0-9a-f]{4}|synthetic(?:-[A-Za-z0-9_-]{1,64})?)"
_ID = r"[A-Za-z0-9_-]{1,80}"
_CASE_PATH = re.compile(rf"/api/cases/({_CASE})(/.*)?\Z", re.ASCII)
_UPLOAD_PATH = re.compile(r"/api/uploads/([0-9a-f]{32})/(chunks|complete|cancel)\Z", re.ASCII)
_REFERENCE_MODEL = re.compile(r"/api/references/([0-9a-f]{32})/model\Z", re.ASCII)
_REFERENCE_SAVE = re.compile(rf"/api/reference-admin/cases/({_CASE})\Z", re.ASCII)
_REFERENCE_REVOKE = re.compile(r"/api/reference-admin/([0-9a-f]{32})/revoke\Z", re.ASCII)
_ADMIN_DISABLE = re.compile(r"/api/admin/users/[0-9a-f]{32}/disable\Z", re.ASCII)
_OPERATOR_RECIPE = re.compile(r"/api/operator/recipes/(recipe-[0-9a-f]{32})(/revoke)?\Z", re.ASCII)
_AUTOMATION_CASE = re.compile(rf"/api/automation/cases/({_CASE})\Z", re.ASCII)
_AUTOMATION_BATCH = re.compile(r"/api/automation/batches/(batch-[0-9a-f]{16})(/cancel)?\Z", re.ASCII)
_ORCHESTRATION_BATCH = re.compile(r"/api/orchestration/batches/(batch-[0-9a-f]{16})(/cancel)?\Z", re.ASCII)
_SHARE = re.compile(r"/share/[A-Za-z0-9_-]{43}(?:/(?:unlock|model|metadata))?\Z", re.ASCII)

_PUBLIC_ASSETS = {
    "/static/app.css", "/static/assets/logo-badge.svg", "/static/patient-view.css",
    "/static/patient-view.js", "/static/vendor/three.min.js",
    "/static/vendor/OrbitControls.js", "/static/cfd-viewer.js",
    "/static/reference-view.js",
}

# Exact case suffixes are intentionally enumerated. A newly added case API
# does not silently inherit view or edit permission from its HTTP verb.
_CASE_EXACT = {
    ("GET", ""): "view",
    ("DELETE", ""): "owner",
    ("GET", "/scenarios"): "view",
    ("POST", "/scenarios"): "edit",
    ("POST", "/segment"): "edit",
    ("POST", "/segment/import-mask"): "edit",
    ("POST", "/segment/edit-mask"): "edit",
    ("POST", "/segment/reset-mask"): "edit",
    ("POST", "/segment/remove-external-air"): "edit",
    ("POST", "/segment/undo-external-air"): "edit",
    ("GET", "/segment/mask-export"): "view",
    ("POST", "/segment/confirm"): "review",
    ("GET", "/segment/learning"): "view",
    ("POST", "/segment/learning/candidate"): "view",
    ("POST", "/segment/learning/reference"): "review",
    ("GET", "/segment/models"): "view",
    ("POST", "/segment/models"): "edit",
    ("GET", "/segment/endplanes"): "view",
    ("GET", "/segment/ports/formal"): "view",
    ("GET", "/segment/endplanes/learning"): "view",
    ("POST", "/segment/endplanes/learning/propose"): "view",
    ("POST", "/segment/endplanes/learning/auto-propose"): "view",
    ("POST", "/segment/endplanes/learning/auto-propose-bilateral"): "view",
    ("POST", "/segment/endplanes/learning/feedback"): "review",
    ("GET", "/segment/selection-model"): "view",
    ("POST", "/segment/endplanes/section"): "view",
    ("POST", "/segment/endplanes/section-targets"): "view",
    ("POST", "/segment/endplanes/suggest-single-port"): "view",
    ("POST", "/segment/endplanes/preview"): "view",
    ("POST", "/segment/endplanes/preview-model"): "view",
    ("POST", "/segment/endplanes"): "edit",
    ("POST", "/surface"): "edit",
    ("POST", "/cfd-prep"): "edit",
    ("POST", "/orchestration/advance"): "edit",
    ("POST", "/cfd-run"): "edit",
    ("GET", "/gate"): "view",
    ("POST", "/harness-advice"): "view",
    ("POST", "/cfd-postprocess"): "edit",
    ("POST", "/cfd-postprocess/cancel"): "edit",
    ("GET", "/cfd-results"): "view",
    ("GET", "/cfd-results/viewer"): "view",
    ("GET", "/cfd-sections"): "view",
    ("POST", "/cfd-sections/preview"): "view",
    ("POST", "/cfd-sections/analyze"): "edit",
    ("POST", "/cfd-sections"): "edit",
    ("GET", "/report"): "view",
    ("POST", "/ai-report"): "edit",
    ("GET", "/ai-report"): "view",
    ("GET", "/viewer"): "view",
    ("GET", "/model"): "view",
    ("GET", "/report-file"): "view",
    ("GET", "/artifact"): "view",
    ("GET", "/review-events"): "view",
    ("POST", "/review-events"): "review",
    ("GET", "/cfd-reviews"): "view",
    ("POST", "/cfd-reviews"): "review",
    ("GET", "/shares"): "view",
    ("POST", "/shares"): "review",
    ("POST", "/shares/test"): "review",
    ("GET", "/grants"): "manage",
}

_CASE_VARIABLE = (
    ("GET", re.compile(r"/segment/models/model-[0-9a-f]{32}\Z", re.ASCII), "view"),
    ("POST", re.compile(r"/segment/models/model-[0-9a-f]{32}/cancel\Z", re.ASCII), "edit"),
    ("POST", re.compile(r"/segment/models/model-[0-9a-f]{32}/draft\Z", re.ASCII), "edit"),
    ("GET", re.compile(r"/segment/models/model-[0-9a-f]{32}/slice/(?:axial|coronal|sagittal)\Z", re.ASCII), "view"),
    ("GET", re.compile(rf"/scenarios/{_ID}\Z", re.ASCII), "view"),
    ("PUT", re.compile(rf"/scenarios/{_ID}/placement\Z", re.ASCII), "edit"),
    ("POST", re.compile(rf"/scenarios/{_ID}/review\Z", re.ASCII), "review"),
    ("POST", re.compile(rf"/scenarios/{_ID}/cfd-(?:prep|run)\Z", re.ASCII), "edit"),
    ("GET", re.compile(rf"/scenarios/{_ID}/(?:cfd-status|comparison|download)\Z", re.ASCII), "view"),
    ("POST", re.compile(r"/segment/endplanes/(?:anterior|posterior)\Z", re.ASCII), "edit"),
    ("GET", re.compile(r"/preview/(?:axial|coronal|sagittal)\Z", re.ASCII), "view"),
    ("GET", re.compile(r"/slice/(?:axial|coronal|sagittal)\Z", re.ASCII), "view"),
    ("POST", re.compile(rf"/shares/{_ID}/revoke\Z", re.ASCII), "review"),
    ("PUT", re.compile(r"/grants/[0-9a-f]{32}\Z", re.ASCII), "manage"),
    ("DELETE", re.compile(r"/grants/[0-9a-f]{32}\Z", re.ASCII), "manage"),
)


def classify(path: str, method: str) -> Policy:
    """Describe a route's authorization need without granting access itself.

    Pass the effective method after the gateway validates and applies its
    DELETE override. HEAD has the same access requirement as GET.
    """
    if not isinstance(path, str) or not path.startswith("/") or not isinstance(method, str):
        return Policy("deny")
    path = path.rstrip("/") or "/"
    method = method.upper()
    if method == "HEAD":
        method = "GET"

    if path in {"/", "/healthz"} and method == "GET":
        return Policy("public")
    if path == "/login" and method in {"GET", "POST"}:
        return Policy("public")
    if path in _PUBLIC_ASSETS and method == "GET":
        return Policy("public")
    if _SHARE.fullmatch(path):
        if path.endswith("/unlock") and method == "POST":
            return Policy("public")
        if not path.endswith("/unlock") and method == "GET":
            return Policy("public")
    if path in {"/references", "/api/references"} and method == "GET":
        return Policy("public")
    if _REFERENCE_MODEL.fullmatch(path) and method == "GET":
        return Policy("public")

    if path == "/logout" and method == "POST":
        return Policy("session")
    if path == "/api/admin/users" and method in {"GET", "POST"}:
        return Policy("admin")
    if _ADMIN_DISABLE.fullmatch(path) and method == "POST":
        return Policy("admin")
    if path in {"/share-admin", "/reference-result", "/api/capabilities"} and method == "GET":
        return Policy("session")
    if path.startswith("/static/") and method == "GET":
        return Policy("session")

    if path == "/api/monitor/events":
        if method == "POST":
            return Policy("monitor_ingest")
        if method == "GET":
            return Policy("monitor_read")
    if path == "/api/monitor/status" and method == "GET":
        return Policy("monitor_read")
    if path == "/api/automation/status" and method == "GET":
        return Policy("monitor_read")
    if path == "/api/automation/cases" and method == "GET":
        return Policy("automation_cases")
    match = _AUTOMATION_CASE.fullmatch(path)
    if match and method == "GET":
        return Policy("case", case_id=match.group(1), capability="view")
    if path == "/api/automation/batches" and method == "POST":
        return Policy("automation_submit")
    match = _AUTOMATION_BATCH.fullmatch(path)
    if match:
        if method == "GET" and match.group(2) is None:
            return Policy("automation_batch", resource_id=match.group(1), capability="view")
        if method == "POST" and match.group(2) == "/cancel":
            return Policy("automation_batch", resource_id=match.group(1), capability="edit")
    if path == "/api/orchestration/batches" and method == "POST":
        return Policy("orchestration_submit")
    match = _ORCHESTRATION_BATCH.fullmatch(path)
    if match:
        if method == "GET" and match.group(2) is None:
            return Policy("orchestration_batch", resource_id=match.group(1), capability="view")
        if method == "POST" and match.group(2) == "/cancel":
            return Policy("orchestration_batch", resource_id=match.group(1), capability="edit")

    if path == "/api/reference-admin/cases" and method == "GET":
        return Policy("reference_candidates")
    match = _REFERENCE_SAVE.fullmatch(path)
    if match and method == "POST":
        return Policy("case", case_id=match.group(1), capability="review")
    match = _REFERENCE_REVOKE.fullmatch(path)
    if match and method == "POST":
        return Policy("reference_revoke", resource_id=match.group(1), capability="review")

    if path == "/api/ai-report/config" and method in {"GET", "PUT"}:
        return Policy("provider")
    if path == "/api/ai-report/models" and method == "POST":
        return Policy("provider")
    if path == "/api/operator/preferences" and method in {"GET", "PUT"}:
        return Policy("operator_preferences")
    if path == "/api/operator/recipe-candidates" and method == "GET":
        return Policy("operator_recipes")
    if path == "/api/operator/recipes" and method in {"GET", "POST"}:
        return Policy("operator_recipes")
    match = _OPERATOR_RECIPE.fullmatch(path)
    if match and ((method in {"GET", "PUT"} and match.group(2) is None)
                  or (method == "POST" and match.group(2) == "/revoke")):
        return Policy("operator_recipes")

    if path == "/api/cases" and method == "GET":
        return Policy("case_list")
    if path == "/api/cases/upload" and method == "POST":
        return Policy("case_create")
    if path == "/api/uploads" and method == "POST":
        return Policy("upload_create")
    match = _UPLOAD_PATH.fullmatch(path)
    if match and method == "POST":
        return Policy("upload", resource_id=match.group(1), capability="owner")

    match = _CASE_PATH.fullmatch(path)
    if match:
        case_id, suffix = match.group(1), match.group(2) or ""
        capability = _CASE_EXACT.get((method, suffix))
        if capability is not None:
            return Policy("case", case_id=case_id, capability=capability)
        for verb, pattern, capability in _CASE_VARIABLE:
            if method == verb and pattern.fullmatch(suffix):
                return Policy("case", case_id=case_id, capability=capability)

    return Policy("deny")
