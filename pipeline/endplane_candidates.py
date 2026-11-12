"""Model-neutral, read-only validation of explicitly supplied current-case plans.

Phase 1 has no registered automatic candidate provider.  This module neither
reads a reference cohort nor transfers coordinates between cases.  An
authenticated service may validate an explicit plan here and separately record
it in the proposal ledger; saving the case plan remains a separate human action.
"""

from __future__ import annotations

from copy import deepcopy
import math

from . import endplane_learning


SUPPORTED_FORMATS = frozenset({"oblique_planes_v1", "oblique_local_inlet_v2", "oblique_bilateral_local_v3"})
SUPPORTED_SCOPES = frozenset({"main_airway", "full_candidate"})


class CandidateInputError(ValueError):
    """The supplied current-case context or structured plan is invalid."""


def _validate_context(scope: object, mask_version: object) -> None:
    if type(scope) is not str or scope not in SUPPORTED_SCOPES:
        raise CandidateInputError("invalid endplane scope")
    if type(mask_version) is not int or not 0 <= mask_version <= 2**31 - 1:
        raise CandidateInputError("invalid mask_version")


def _vector(value: object, label: str, *, normal: bool = False) -> None:
    if type(value) is not list or len(value) != 3:
        raise CandidateInputError(f"{label} must contain three finite numbers")
    if any(type(part) not in (int, float)
           or not -1_000_000_000_000 <= part <= 1_000_000_000_000
           or not math.isfinite(part) for part in value):
        raise CandidateInputError(f"{label} must contain three finite numbers")
    if normal and not any(part != 0 for part in value):
        raise CandidateInputError(f"{label} must be nonzero")


def validate_candidate_plan(scope: str, mask_version: int, plan: object) -> dict:
    """Validate an explicit plan for the current scope/version without I/O.

    Both envelope fields must be present in the plan and match exactly; this
    function never fills or rewrites them.  Ledger validation rejects unknown
    fields and bounds numeric evidence.  Complete geometric checks here cover
    the stricter proposal input contract without asserting anatomical validity.
    """
    _validate_context(scope, mask_version)
    if type(plan) is not dict:
        raise CandidateInputError("candidate plan must be an object")
    if type(plan.get("scope")) is not str or plan["scope"] != scope:
        raise CandidateInputError("plan scope must explicitly match current scope")
    if type(plan.get("mask_version")) is not int or plan["mask_version"] != mask_version:
        raise CandidateInputError("plan mask_version must explicitly match current mask_version")
    fmt = plan.get("format")
    if type(fmt) is not str or fmt not in SUPPORTED_FORMATS:
        raise CandidateInputError("candidate format must be a supported oblique format")
    if {"anterior_index", "posterior_index"} & set(plan):
        raise CandidateInputError("oblique candidate plans cannot contain CT indices")
    sides = ("left_inlet", "right_inlet", "posterior") if fmt == "oblique_bilateral_local_v3" else ("anterior", "posterior")
    for side in sides:
        plane = plan.get(f"{side}_plane")
        if type(plane) is not dict or set(plane) != {"point_lps_mm", "normal_lps"}:
            raise CandidateInputError(f"{side}_plane must contain exactly point_lps_mm and normal_lps")
        _vector(plane["point_lps_mm"], f"{side}_plane.point_lps_mm")
        _vector(plane["normal_lps"], f"{side}_plane.normal_lps", normal=True)
    if fmt == "oblique_bilateral_local_v3":
        if scope != "main_airway" or "anterior_plane" in plan or "anterior_seed_lps_mm" in plan:
            raise CandidateInputError("bilateral candidates require main_airway and no legacy anterior fields")
        for name in ("left_inlet_seed_lps_mm", "right_inlet_seed_lps_mm", "posterior_seed_lps_mm"):
            _vector(plan.get(name), name)
    elif fmt == "oblique_local_inlet_v2":
        if scope != "main_airway":
            raise CandidateInputError("local inlet candidates require main_airway scope")
        _vector(plan.get("anterior_seed_lps_mm"), "anterior_seed_lps_mm")
    elif "anterior_seed_lps_mm" in plan:
        raise CandidateInputError("anterior_seed_lps_mm requires local inlet format")
    try:
        canonical = endplane_learning.validate_plan(plan, scope=scope, mask_version=mask_version)
    except endplane_learning.LearningError as exc:
        raise CandidateInputError(str(exc)) from exc
    return deepcopy(canonical)


def propose_candidate(scope: str, mask_version: int) -> dict:
    """Report absence of a registered provider; never generate or persist a plan.

    No cohort or provider object is accepted from request data.  Authorization
    and current-case version checks belong to the authenticated service before
    calling this read-only function.
    """
    _validate_context(scope, mask_version)
    return {
        "status": "unavailable",
        "reason": "no_registered_candidate_provider",
        "scope": scope,
        "mask_version": mask_version,
        "proposal_id": None,
    }


__all__ = [
    "CandidateInputError", "SUPPORTED_FORMATS", "SUPPORTED_SCOPES",
    "propose_candidate", "validate_candidate_plan",
]
