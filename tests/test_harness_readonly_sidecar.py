"""The advisory sidecar rejects case bytes and never grants write actions."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.harness_readonly_sidecar import READ_ONLY_TOOLS, advise

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "harness_readonly_sidecar.py"


def summary(**overrides):
    value = {
        "schema_version": 1, "case_id": "synthetic-harness-001", "state_sha256": "a" * 64,
        "stages": {name: "ok" for name in ("import", "anonymize", "qc", "segment",
                                                   "surface", "cfd_prep", "cfd_run", "report")},
        "baseline_ready": True, "numeric_ok": True, "eligible": True,
    }
    value.update(overrides)
    return value


def encoded(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def test_process_returns_bounded_structured_advice_without_write_capabilities():
    value = summary(numeric_ok=False, eligible=False)
    run = subprocess.run([sys.executable, str(SCRIPT)], input=encoded(value), capture_output=True,
                         timeout=5, check=False)
    assert run.returncode == 0 and not run.stderr
    result = json.loads(run.stdout)
    assert result["next_step"] == {"code": "review_numeric_gate", "stage": "cfd_run"}
    assert result["failure_recap"] == ["numeric_gate_failed", "eligibility_not_met"]
    assert result["human_review_required"] is True and result["automated_action"] is False
    assert set(result["skill_draft"]["allowed_tools"]) <= set(READ_ONLY_TOOLS)
    assert "submit_cfd_batch" not in run.stdout.decode()
    assert "cancel_queued_cfd" not in run.stdout.decode()


@pytest.mark.parametrize("extra", ["patient_name", "ct_path", "free_text", "api_key", "url"])
def test_process_rejects_unexpected_sensitive_or_free_text_fields(extra):
    value = summary(**{extra: "SECRET_SHOULD_NOT_BE_ECHOED"})
    run = subprocess.run([sys.executable, str(SCRIPT)], input=encoded(value), capture_output=True,
                         timeout=5, check=False)
    assert run.returncode == 2 and not run.stdout
    assert b"SECRET_SHOULD_NOT_BE_ECHOED" not in run.stderr


def test_rejects_duplicate_keys_and_large_input():
    payload = encoded(summary())
    with pytest.raises(ValueError, match="重复"):
        advise(payload[:-1] + b',"case_id":"synthetic-harness-002"}')
    with pytest.raises(ValueError, match="长度"):
        advise(payload + b" " * 4096)


def test_sha_is_stable_across_json_key_order_and_no_quality_label_is_inferred():
    value = summary(stages={**summary()["stages"], "surface": "blocked"}, numeric_ok=None, eligible=None)
    a = advise(json.dumps(value, sort_keys=True).encode())
    b = advise(json.dumps(value, sort_keys=False).encode())
    assert a["input_sha256"] == b["input_sha256"]
    assert a["next_step"] == {"code": "review_failed_stage", "stage": "surface"}
    assert a["failure_recap"] == ["stage_failure"]


def test_invalid_state_fingerprint_is_rejected():
    with pytest.raises(ValueError, match="指纹"):
        advise(encoded(summary(state_sha256=True)))


def test_eligible_state_requires_both_preparation_and_numeric_gate():
    with pytest.raises(ValueError, match="矛盾"):
        advise(encoded(summary(numeric_ok=False)))
    with pytest.raises(ValueError, match="矛盾"):
        advise(encoded(summary(baseline_ready=False)))


def test_warning_stage_requires_manual_review():
    value = summary(stages={**summary()["stages"], "qc": "warn"})
    result = advise(encoded(value))
    assert result["next_step"] == {"code": "review_warning_stage", "stage": "qc"}
    assert result["failure_recap"] == ["stage_warning"]
