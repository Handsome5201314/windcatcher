"""Suggest inert personal workflow drafts from bounded operation metadata.

Browser activity is a usage hint, not a quality label. Suggestions never grant
tool permissions, execute steps, or become saved recipes without user action.
"""

from __future__ import annotations

from collections import defaultdict

from .operator_recipes import SUGGESTED_STEPS


MINIMUM_CASES = 3
MAX_CANDIDATES = 3
MAX_PATTERN_STEPS = 4
# These names occur as successful browser stage actions. Do not infer a review,
# end-plane quality decision, or CFD result from adjacent stage names.
OBSERVED_STEPS = SUGGESTED_STEPS & frozenset({
    "qc", "segment", "surface", "cfd_prep", "report",
})


def suggest_candidates(events: list[dict], owner_id: str,
                       allowed_case_ids: set[str]) -> list[dict]:
    """Count a repeated ordered sequence at most once per authorized case."""
    tracks: dict[str, list[str]] = {}
    for event in sorted(events, key=lambda row: row["id"]):
        case_id = event.get("case_id")
        details = event.get("details")
        if (event.get("session_id") != owner_id or case_id not in allowed_case_ids
                or event.get("kind") != "stage" or event.get("action") != "action_finished"
                or not isinstance(details, dict) or details.get("status") != "ok"):
            continue
        stage = details.get("stage")
        if stage not in OBSERVED_STEPS:
            continue
        sequence = tracks.setdefault(case_id, [])
        if stage not in sequence:
            sequence.append(stage)

    support: dict[tuple[str, ...], set[str]] = defaultdict(set)
    for case_id, sequence in tracks.items():
        for length in range(2, min(MAX_PATTERN_STEPS, len(sequence)) + 1):
            for start in range(len(sequence) - length + 1):
                support[tuple(sequence[start:start + length])].add(case_id)

    ranked = sorted(
        ((steps, len(cases)) for steps, cases in support.items()
         if len(cases) >= MINIMUM_CASES),
        key=lambda item: (-item[1], -len(item[0]), item[0]),
    )
    chosen: list[tuple[tuple[str, ...], int]] = []
    for steps, case_count in ranked:
        if any(case_count <= chosen_count and any(
            longer[start:start + len(steps)] == steps
            for start in range(len(longer) - len(steps) + 1)
        ) for longer, chosen_count in chosen):
            continue
        chosen.append((steps, case_count))
        if len(chosen) == MAX_CANDIDATES:
            break
    return [{"suggested_steps": list(steps), "case_count": count}
            for steps, count in chosen]
