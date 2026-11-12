"""The public workbench routes must preserve the three-opening contract."""

import asyncio

import pytest
from fastapi import HTTPException

import server
from pipeline import endplanes


def _body(**changes):
    plane = lambda y: {"point_lps_mm": [0.0, y, 0.0], "normal_lps": [0.0, -1.0, 0.0]}
    values = {
        "scope": "main_airway", "mask_version": 3,
        "format": endplanes.BILATERAL_LOCAL_FORMAT,
        "left_inlet_plane": plane(1.0), "right_inlet_plane": plane(2.0),
        "posterior_plane": plane(30.0),
        "left_inlet_seed_lps_mm": [-5.0, 0.0, 0.0],
        "right_inlet_seed_lps_mm": [5.0, 0.0, 0.0],
        "posterior_seed_lps_mm": [0.0, 31.0, 0.0],
    }
    values.update(changes)
    return server.EndplanesBody(**values)


def test_bilateral_preview_save_and_mesh_route_all_three_seeds(monkeypatch):
    monkeypatch.setattr(server, "_require_case", lambda _case_id: None)
    received = []

    def preview(*args):
        received.append(("preview", args))
        return {"format": endplanes.BILATERAL_LOCAL_FORMAT}

    def save(*args):
        received.append(("save", args))
        return {"format": endplanes.BILATERAL_LOCAL_FORMAT, "complete": True}

    def mesh(*args):
        received.append(("mesh", args))
        return b"solid test\nendsolid test\n"

    monkeypatch.setattr(endplanes, "preview_bilateral_local_endplanes", preview)
    monkeypatch.setattr(endplanes, "save_bilateral_local_endplanes", save)
    monkeypatch.setattr(endplanes, "render_bilateral_local_preview_model", mesh)
    body = _body()
    assert asyncio.run(server.api_preview_oblique_endplanes("case-test", body))["format"] == body.format
    assert asyncio.run(server.api_save_endplanes("case-test", body))["complete"] is True
    response = asyncio.run(server.api_preview_local_inlet_model("case-test", body))
    assert response.body.startswith(b"solid test")
    assert response.headers["cache-control"] == "no-store"
    assert [name for name, _ in received] == ["preview", "save", "mesh"]
    assert all(args == (
        "case-test", "main_airway", body.left_inlet_plane, body.right_inlet_plane,
        body.posterior_plane, body.left_inlet_seed_lps_mm,
        body.right_inlet_seed_lps_mm, body.posterior_seed_lps_mm, 3,
    ) for _, args in received)


@pytest.mark.parametrize("changes", [
    {"right_inlet_seed_lps_mm": None},
    {"anterior_plane": {"point_lps_mm": [0, 0, 0], "normal_lps": [0, 1, 0]}},
    {"anterior_index": 2},
])
def test_bilateral_rejects_missing_or_legacy_fields(monkeypatch, changes):
    monkeypatch.setattr(server, "_require_case", lambda _case_id: None)
    body = _body(**changes)
    for route in (server.api_preview_oblique_endplanes, server.api_save_endplanes,
                  server.api_preview_local_inlet_model):
        with pytest.raises(HTTPException) as error:
            asyncio.run(route("case-test", body))
        assert error.value.status_code == 400


def test_other_format_cannot_silently_ignore_bilateral_coordinates(monkeypatch):
    monkeypatch.setattr(server, "_require_case", lambda _case_id: None)
    body = _body(format="oblique_planes_v1")
    for route in (server.api_preview_oblique_endplanes, server.api_save_endplanes):
        with pytest.raises(HTTPException) as error:
            asyncio.run(route("case-test", body))
        assert error.value.status_code == 400
