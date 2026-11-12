"""The displayed draft, saved mesh, and CFD boundaries use one geometry."""

from collections import Counter
import json

import numpy as np

from pipeline import cases, cfd_prep, endplanes, formal_ports, geom_qc, surface
from pipeline.volume import Volume, save_volume


def _triangle_keys(triangles):
    return Counter(
        tuple(sorted(tuple(np.round(vertex, 6)) for vertex in triangle))
        for triangle in np.asarray(triangles, dtype=float)
    )


def _stl_patch_triangles(path):
    vertices = [
        [float(value) * 1000.0 for value in line.split()[1:]]
        for line in path.read_text(encoding="ascii").splitlines()
        if line.lstrip().startswith("vertex ")
    ]
    return np.asarray(vertices, dtype=float).reshape(-1, 3, 3)


def test_draft_saved_and_cfd_share_three_local_ports_without_cutting_medial_branch(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(cases, "CASES_DIR", tmp_path)
    mask = np.zeros((28, 38, 16), dtype=np.uint8)
    mask[4:11, 3:17, 5:12] = 1
    mask[17:24, 3:17, 5:12] = 1
    mask[4:24, 17:22, 5:12] = 1
    mask[9:19, 22:35, 5:12] = 1
    mask[12:15, 3:18, 5:12] = 1  # A third branch crossed by the inlet planes.
    volume = Volume(
        np.zeros(mask.shape, dtype=np.float32), (1.0, 1.0, 1.0),
        (0.0, 0.0, 0.0), tuple(map(tuple, np.eye(3))),
    )
    left = {"point_lps_mm": [20.0, 9.0, 8.0], "normal_lps": [0.0, -1.0, 0.0]}
    right = {"point_lps_mm": [7.0, 9.0, 8.0], "normal_lps": [0.0, -1.0, 0.0]}
    outlet = {"point_lps_mm": [14.0, 29.0, 8.0], "normal_lps": [0.0, 1.0, 0.0]}
    seeds = ([20.0, 5.0, 8.0], [7.0, 5.0, 8.0], [14.0, 32.0, 8.0])

    case_id = cases.create_case("local_path", "<synthetic port consistency>", synthetic=False)
    root = cases.case_dir(case_id)
    save_volume(case_id, volume)
    np.save(root / "segment" / "mask.npy", mask)
    (root / "segment" / "segmentation.json").write_text(json.dumps({
        "params": {"ap_axis": 1, "hu_high": -300.0},
        "mask_version": 1, "need_human_confirm": False,
        "review_flags": {"external_air_suspected": False},
        "external_air_validation": {
            "mask_version": 1, "body_hu_threshold": -300.0,
            "outside_voxels": 0, "candidate_outside_voxels": 0,
        },
    }), encoding="utf-8")

    draft_stl = endplanes.render_bilateral_local_preview_model(
        case_id, "main_airway", left, right, outlet, *seeds, 1,
    )
    assert not (root / "surface" / "airway.stl").exists()
    plan = endplanes.save_bilateral_local_endplanes(
        case_id, "main_airway", left, right, outlet, *seeds, 1,
    )
    assert plan["mesh_preflight"]["closed_caps"] is True
    surface.run_surface(case_id)
    saved_stl = (root / "surface" / "airway.stl").read_bytes()
    assert draft_stl[80:] == saved_stl[80:]  # Only the descriptive STL header differs.
    effective = np.load(root / "surface" / "effective_mask.npy")
    np.testing.assert_array_equal(effective[12:15, 3:18, 5:12], mask[12:15, 3:18, 5:12])

    qc = geom_qc.run_geom_qc(case_id)
    assert qc["verdict"] == "pass"
    assert qc["bilateral_local_caps"]["components_by_side"] == {
        "left": 1, "right": 1, "outlet": 1,
    }
    manifest = formal_ports.get_formal_port_state(case_id)
    assert manifest["status"] == "ready"
    assert len({port["port_id"] for port in manifest["ports"]}) == 3
    prepared = cfd_prep.prepare_case(case_id)
    assert prepared["inlet_patches"] == ["inlet_left", "inlet_right"]

    vertices = np.load(root / "surface" / "vertices.npy")
    faces = np.load(root / "surface" / "faces.npy")
    for side, filename in (("left", "inlet_left"), ("right", "inlet_right"),
                           ("posterior", "outlet")):
        port = next(item for item in manifest["ports"] if item["side"] == side)
        patch_faces = np.load(root / "surface" / f"{filename}_triangles.npy")
        assert port["cap_face_ids"] == patch_faces.tolist()
        expected = vertices[faces[patch_faces]]
        actual = _stl_patch_triangles(
            root / "cfd" / "constant" / "triSurface" / f"{filename}.stl"
        )
        assert _triangle_keys(actual) == _triangle_keys(expected)
