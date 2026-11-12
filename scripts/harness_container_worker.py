"""One-shot stdin boundary for the isolated DeepSeek Harness container.

The legacy route accepts a validated state summary. The synthetic candidate
route accepts only an allowlisted fixture selector. Neither route accepts CT
or case files, and credentials are never written to stdout or stderr.
"""

from __future__ import annotations

import json
import os
import re
import sys

if __package__:
    from . import harness_action_selector, harness_endplane_worker, harness_sdk_worker
    from .harness_readonly_sidecar import SummaryError, _unique_pairs, _validate
else:
    import harness_action_selector
    import harness_endplane_worker
    import harness_sdk_worker
    from harness_readonly_sidecar import SummaryError, _unique_pairs, _validate


MAX_ENVELOPE_BYTES = 8192
MAX_KEY_LENGTH = 512
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z", re.ASCII)
RELAY_BASE_URL = "https://tongyimohe.aitoken.credit/v1"
RELAY_MODEL = "gpt-6.1-sol"


class EnvelopeError(ValueError):
    """Invalid container input; its text must never contain input values."""


def _load_envelope(raw: bytes) -> dict:
    if not raw or len(raw) > MAX_ENVELOPE_BYTES:
        raise EnvelopeError("Envelope length invalid")
    try:
        envelope = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, SummaryError) as exc:
        raise EnvelopeError("Envelope JSON invalid") from exc
    if type(envelope) is not dict:
        raise EnvelopeError("Envelope fields invalid")
    return envelope


def _credentials(envelope: dict) -> tuple[str, str]:
    key = envelope["api_key"]
    if (type(key) is not str or not 1 <= len(key) <= MAX_KEY_LENGTH
            or any(not 33 <= ord(char) <= 126 for char in key)):
        raise EnvelopeError("Credential invalid")
    model = envelope["model"]
    if type(model) is not str or MODEL.fullmatch(model) is None or model != RELAY_MODEL:
        raise EnvelopeError("Model invalid")
    return key, model


def _base_url(envelope: dict) -> str | None:
    base_url = envelope.get("base_url")
    if type(base_url) is not str or base_url != RELAY_BASE_URL:
        raise EnvelopeError("Base URL invalid")
    return base_url


def _parse_legacy(envelope: dict) -> tuple[dict, str, str]:
    if set(envelope) not in ({"summary", "api_key", "model"},
                             {"summary", "api_key", "model", "base_url"}):
        raise EnvelopeError("Envelope fields invalid")
    _base_url(envelope)
    key, model = _credentials(envelope)
    try:
        summary_raw = json.dumps(envelope["summary"], sort_keys=True,
                                 separators=(",", ":"), ensure_ascii=True,
                                 allow_nan=False).encode("ascii")
        summary = _validate(summary_raw)
    except (TypeError, ValueError, SummaryError) as exc:
        raise EnvelopeError("Summary invalid") from exc
    return summary, key, model


def parse_envelope(raw: bytes) -> tuple[dict, str, str]:
    """Preserve the original read-only advice envelope contract."""
    return _parse_legacy(_load_envelope(raw))


def _parse_synthetic_candidate(envelope: dict) -> tuple[dict, str, str]:
    if (set(envelope) not in ({"task", "geometry", "api_key", "model"},
                             {"task", "geometry", "api_key", "model", "base_url"})
            or envelope["task"] != harness_endplane_worker.TASK):
        raise EnvelopeError("Envelope fields invalid")
    _base_url(envelope)
    key, model = _credentials(envelope)
    try:
        geometry = harness_endplane_worker.validate_geometry(envelope["geometry"])
    except harness_endplane_worker.CandidateError as exc:
        raise EnvelopeError("Synthetic geometry invalid") from exc
    return geometry, key, model


def _parse_action(envelope: dict) -> tuple[dict, str, str]:
    if (set(envelope) not in ({"task", "request", "api_key", "model"},
                             {"task", "request", "api_key", "model", "base_url"})
            or envelope["task"] != harness_action_selector.TASK):
        raise EnvelopeError("Envelope fields invalid")
    _base_url(envelope)
    key, model = _credentials(envelope)
    try:
        request = harness_action_selector.validate_request(envelope["request"])
    except harness_action_selector.ActionError as exc:
        raise EnvelopeError("Action request invalid") from exc
    return request, key, model


def run(raw: bytes) -> dict:
    envelope = _load_envelope(raw)
    task = envelope.get("task")
    if task == harness_endplane_worker.TASK:
        payload, key, model = _parse_synthetic_candidate(envelope)
    elif task == harness_action_selector.TASK:
        payload, key, model = _parse_action(envelope)
    else:
        payload, key, model = _parse_legacy(envelope)
    base_url = _base_url(envelope)
    os.environ["DEEPSEEK_API_KEY"] = key
    os.environ["WINDCATCHER_HARNESS_MODEL"] = model
    os.environ.pop("DEEPSEEK_BASE_URL", None)
    if base_url is None:
        os.environ.pop("WINDCATCHER_HARNESS_BASE_URL", None)
    else:
        os.environ["WINDCATCHER_HARNESS_BASE_URL"] = base_url
    if task == harness_endplane_worker.TASK:
        return harness_endplane_worker.run_model(payload)
    if task == harness_action_selector.TASK:
        return harness_action_selector.run_model(payload)
    return harness_sdk_worker.run_model(payload)


def main() -> int:
    try:
        result = run(sys.stdin.buffer.read(MAX_ENVELOPE_BYTES + 1))
        output = json.dumps(result, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=True, allow_nan=False)
    except Exception as exc:
        # SDK exceptions may include the credential or host details. Never echo them.
        print(f"Harness container worker unavailable ({type(exc).__name__})",
              file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
