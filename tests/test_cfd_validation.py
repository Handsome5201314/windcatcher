from __future__ import annotations

import pytest

from pipeline.cfd_validation import _inspect_ascii_field


MESH = {
    "cells": 2,
    "internal_faces": 1,
    "patch_faces": {"inlet": 2, "outlet": 1, "wall": 1},
}


def _write_velocity(tmp_path, inlet_value):
    path = tmp_path / "U"
    path.write_text(
        """FoamFile
{
    format ascii;
    class volVectorField;
    object U;
}
dimensions [0 1 -1 0 0 0 0];
internalField nonuniform List<vector> 2
(
    (0 0 1)
    (0 0 2)
);
boundaryField
{
    inlet
    {
        type flowRateInletVelocity;
        volumetricFlowRate
        {
            type constant;
            value 0.00025;
        }
        """ + inlet_value + """
    }
    outlet
    {
        type zeroGradient;
    }
    wall
    {
        type fixedValue;
        value uniform (0 0 0);
    }
}
""",
        encoding="ascii",
    )
    return path


@pytest.mark.parametrize(
    "inlet_value",
    [
        "value nonuniform List<vector> 2 ((0 0 1) (0 0 2));",
        "value uniform (0 0 1);",
    ],
)
def test_velocity_patch_after_flow_function_is_resolved_to_inlet(tmp_path, inlet_value):
    # OpenFOAM writes a closed Function1 subdictionary before the inlet's value.
    # Its opening brace must not make the field belong to volumetricFlowRate.
    result = _inspect_ascii_field(_write_velocity(tmp_path, inlet_value), "U", MESH)

    assert result["status"] == "checked"
    assert result["complete"] is True
    assert result["finite"] is True
    assert result["count"] == MESH["cells"]


def test_wrong_inlet_count_after_flow_function_still_fails(tmp_path):
    result = _inspect_ascii_field(
        _write_velocity(tmp_path, "value nonuniform List<vector> 1 ((0 0 1));"), "U", MESH
    )

    assert result["status"] == "invalid"
    assert result["complete"] is False
    assert result["reason"] == "inlet 边界场数量与网格不匹配"


def test_nonfinite_inlet_value_after_flow_function_still_fails(tmp_path):
    result = _inspect_ascii_field(
        _write_velocity(tmp_path, "value nonuniform List<vector> 2 ((0 0 1) (0 0 nan));"), "U", MESH
    )

    assert result["status"] == "invalid"
    assert result["complete"] is False
    assert "NaN" in result["reason"]
