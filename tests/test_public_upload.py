"""Regression tests for the browser upload path used by the public entry point."""

import json

import pytest
from fastapi.testclient import TestClient

import pipeline.cases as cases
import pipeline.config as config
import server
from tests.make_phantom import build_phantom

REAL_PREPARE_CASE = server._prepare_case


@pytest.fixture
def upload_client(tmp_path, monkeypatch):
    root = tmp_path / "cases"
    monkeypatch.setattr(server, "CASES_DIR", root)
    monkeypatch.setattr(cases, "CASES_DIR", root)
    monkeypatch.setattr(config, "CASES_DIR", root)
    monkeypatch.setattr(server, "_prepare_case", lambda *_args: None)
    with TestClient(server.app) as client:
        yield client, root


def test_browser_upload_stores_relative_dicom_files_and_starts_case(upload_client):
    client, root = upload_client
    response = client.post(
        "/api/cases/upload",
        data={
            "synthetic": "false",
            "anonymize": "true",
            "relative_paths": json.dumps(["Series/001.dcm", "Series/nested/002.dcm"]),
        },
        files=[
            ("files", ("001.dcm", b"first", "application/dicom")),
            ("files", ("002.dcm", b"second", "application/dicom")),
        ],
    )

    assert response.status_code == 200
    case_id = response.json()["case_id"]
    source = root / case_id / "import" / "dicom"
    assert (source / "001.dcm").read_bytes() == b"first"
    assert (source / "nested" / "002.dcm").read_bytes() == b"second"
    case = json.loads((root / case_id / "case.json").read_text(encoding="utf-8"))
    assert case["source_type"] == "browser_upload"
    assert case["source_path"] == "<浏览器上传的 DICOM 文件夹>"
    assert case["job"]["status"] == "running"


def test_browser_upload_rejects_path_traversal_without_creating_case(upload_client):
    client, root = upload_client
    response = client.post(
        "/api/cases/upload",
        data={"relative_paths": json.dumps(["../outside.dcm"])},
        files=[("files", ("outside.dcm", b"secret", "application/dicom"))],
    )

    assert response.status_code == 400
    assert "非法目录跳转" in response.json()["detail"]["reason"]
    assert not list(root.glob("case-*/case.json"))
    assert not list(root.glob(".windcatcher-upload-*"))


def test_browser_upload_runs_synthetic_ct_import_qc_and_preview(upload_client, tmp_path, monkeypatch):
    client, root = upload_client
    monkeypatch.setattr(server, "_prepare_case", REAL_PREPARE_CASE)
    series = build_phantom(tmp_path / "synthetic-series")
    dicom_files = sorted(series.glob("*.dcm"))
    response = client.post(
        "/api/cases/upload",
        data={
            "synthetic": "true",
            "anonymize": "true",
            "relative_paths": json.dumps([f"Synthetic-Series/{path.name}" for path in dicom_files]),
        },
        files=[("files", (path.name, path.read_bytes(), "application/dicom")) for path in dicom_files],
    )

    assert response.status_code == 200
    case_id = response.json()["case_id"]
    case = json.loads((root / case_id / "case.json").read_text(encoding="utf-8"))
    assert case["synthetic"] is True
    assert case["job"]["status"] == "completed"
    assert case["stages"]["import"]["status"] == "ok"
    assert case["stages"]["qc"]["status"] == "ok"
    assert case["stages"]["anonymize"]["status"] == "ok"
    assert (root / case_id / "preview" / "axial.png").is_file()
    assert (root / case_id / "preview" / "index.json").is_file()
    assert not list(root.glob(".windcatcher-upload-*"))


def test_upload_path_normalizer_rejects_absolute_windows_path():
    with pytest.raises(server._UploadRejected, match="绝对路径"):
        server._safe_upload_path(r"C:\\patient\\001.dcm")
