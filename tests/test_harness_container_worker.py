"""Synthetic tests for the isolated worker's one-shot stdin contract."""

import io
import json
import sys

import pytest

from scripts import harness_container_worker as worker


def summary():
    return {
        "schema_version": 1,
        "case_id": "synthetic-harness-container",
        "state_sha256": "a" * 64,
        "stages": {name: "ok" for name in (
            "import", "anonymize", "qc", "segment", "surface",
            "cfd_prep", "cfd_run", "report",
        )},
        "baseline_ready": True,
        "numeric_ok": True,
        "eligible": True,
    }


def envelope(**changes):
    value = {
        "summary": summary(),
        "api_key": "synthetic-test-credential",
        "model": "gpt-6.1-sol",
        "base_url": "https://tongyimohe.aitoken.credit/v1",
    }
    value.update(changes)
    return json.dumps(value, separators=(",", ":")).encode()


def candidate_envelope(**changes):
    value = {
        "task": "synthetic_endplane_candidate",
        "geometry": {"fixture": "bent_tube_v1"},
        "api_key": "synthetic-test-credential",
        "model": "gpt-6.1-sol",
        "base_url": "https://tongyimohe.aitoken.credit/v1",
    }
    value.update(changes)
    return json.dumps(value, separators=(",", ":")).encode()


def action_request():
    return {
        "schema_version": 1,
        "stages": {name: "pending" for name in (
            "import", "anonymize", "qc", "segment", "surface",
            "cfd_prep", "cfd_run", "report",
        )},
        "baseline_ready": False, "numeric_ok": None, "eligible": None,
        "allowed_actions": ["wait_for_review"],
    }


def action_envelope(**changes):
    value = {
        "task": "select_next_action",
        "request": action_request(),
        "api_key": "synthetic-test-credential",
        "model": "gpt-6.1-sol",
        "base_url": "https://tongyimohe.aitoken.credit/v1",
    }
    value.update(changes)
    return json.dumps(value, separators=(",", ":")).encode()


def test_synthetic_success_passes_only_validated_summary_and_scoped_settings(monkeypatch):
    captured = {}

    def fake_run_model(value):
        captured["summary"] = value
        captured["key"] = worker.os.environ["DEEPSEEK_API_KEY"]
        captured["model"] = worker.os.environ["WINDCATCHER_HARNESS_MODEL"]
        return {"advice": {"status": "advisory_only"},
                "metering": {"status": "unavailable", "cost_usd": None}}

    monkeypatch.setattr(worker.harness_sdk_worker, "run_model", fake_run_model)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("WINDCATCHER_HARNESS_MODEL", raising=False)
    monkeypatch.setenv("WINDCATCHER_HARNESS_BASE_URL", "https://unapproved.example")
    assert worker.run(envelope()) == {
        "advice": {"status": "advisory_only"},
        "metering": {"status": "unavailable", "cost_usd": None},
    }
    assert captured == {
        "summary": summary(),
        "key": "synthetic-test-credential",
        "model": "gpt-6.1-sol",
    }
    assert worker.os.environ["WINDCATCHER_HARNESS_BASE_URL"] == \
        "https://tongyimohe.aitoken.credit/v1"


def test_approved_relay_url_reaches_readonly_worker(monkeypatch):
    seen = {}

    def fake_run_model(_value):
        seen["base_url"] = worker.os.environ.get("WINDCATCHER_HARNESS_BASE_URL")
        return {"advice": {}, "metering": {}}

    monkeypatch.setattr(worker.harness_sdk_worker, "run_model", fake_run_model)
    worker.run(envelope(base_url="https://tongyimohe.aitoken.credit/v1"))
    assert seen["base_url"] == "https://tongyimohe.aitoken.credit/v1"


def test_candidate_envelope_uses_only_allowlisted_synthetic_geometry(monkeypatch):
    captured = {}

    def fake_run_model(geometry):
        captured["geometry"] = geometry
        captured["model"] = worker.os.environ["WINDCATCHER_HARNESS_MODEL"]
        captured["base_url"] = worker.os.environ.get("WINDCATCHER_HARNESS_BASE_URL")
        return {"candidate": {"scope": "main_airway"}, "metering": {"status": "unavailable"}}

    monkeypatch.setattr(worker.harness_endplane_worker, "run_model", fake_run_model)
    result = worker.run(candidate_envelope(base_url="https://tongyimohe.aitoken.credit/v1"))
    assert result["candidate"]["scope"] == "main_airway"
    assert captured["geometry"]["fixture"] == "bent_tube_v1"
    assert captured["geometry"]["centerline_lps_mm"][0] == [0, 0, 0]
    assert captured["model"] == "gpt-6.1-sol"
    assert captured["base_url"] == "https://tongyimohe.aitoken.credit/v1"


def test_action_envelope_passes_only_anonymous_allowlisted_status(monkeypatch):
    captured = {}

    def fake_run_model(request):
        captured["request"] = request
        captured["base_url"] = worker.os.environ.get("WINDCATCHER_HARNESS_BASE_URL")
        return {"selection": {"action": "wait_for_review"},
                "metering": {"status": "unavailable"}}

    monkeypatch.setattr(worker.harness_action_selector, "run_model", fake_run_model)
    result = worker.run(action_envelope())
    assert result["selection"]["action"] == "wait_for_review"
    assert captured["request"] == action_request()
    assert "case_id" not in captured["request"]
    assert captured["base_url"] == "https://tongyimohe.aitoken.credit/v1"


@pytest.mark.parametrize("raw", [
    action_envelope(request={**action_request(), "patient_name": "private"}),
    action_envelope(request={**action_request(), "allowed_actions": ["confirm_mask"]}),
    action_envelope(request={**action_request(), "allowed_actions": []}),
    action_envelope(extra="private"),
    action_envelope(base_url="https://unapproved.example"),
    action_envelope(task="run_case_shell"),
    b'{"task":"select_next_action","request":{"allowed_actions":[],"allowed_actions":["wait_for_review"]},"api_key":"secret","model":"gpt-6.1-sol"}',
])
def test_invalid_action_envelope_never_invokes_model(monkeypatch, raw):
    monkeypatch.setattr(worker.harness_action_selector, "run_model",
                        lambda _value: pytest.fail("model must not start"))
    with pytest.raises(worker.EnvelopeError):
        worker.run(raw)


@pytest.mark.parametrize("raw", [
    candidate_envelope(geometry={"fixture": "other"}),
    candidate_envelope(geometry={"fixture": "bent_tube_v1", "case_id": "private"}),
    candidate_envelope(geometry={"fixture": "bent_tube_v1", "coordinates": [1, 2, 3]}),
    candidate_envelope(extra="private"),
    candidate_envelope(base_url="https://unapproved.example"),
    candidate_envelope(base_url=""),
    candidate_envelope(task="patient_endplane_candidate"),
    b'{"task":"synthetic_endplane_candidate","geometry":{"fixture":"bent_tube_v1","fixture":"other"},"api_key":"secret","model":"gpt-6.1-sol"}',
])
def test_invalid_candidate_envelope_never_invokes_model(monkeypatch, raw):
    monkeypatch.setattr(worker.harness_endplane_worker, "run_model",
                        lambda _value: pytest.fail("model must not start"))
    with pytest.raises(worker.EnvelopeError):
        worker.run(raw)


@pytest.mark.parametrize("raw", [
    envelope(api_key=""),
    envelope(api_key="bad key with whitespace"),
    envelope(model="../other-model"),
    envelope(summary={**summary(), "patient_name": "PRIVATE_NAME"}),
    envelope(extra="PRIVATE_EXTRA_FIELD"),
    envelope(base_url="https://unapproved.example"),
    envelope(base_url=""),
    b'{"summary":{},"summary":{},"api_key":"secret","model":"gpt-6.1-sol"}',
    b'{"summary":{"a":1,"a":2},"api_key":"secret","model":"gpt-6.1-sol"}',
    b"x" * (worker.MAX_ENVELOPE_BYTES + 1),
])
def test_invalid_envelope_never_invokes_model(monkeypatch, raw):
    monkeypatch.setattr(worker.harness_sdk_worker, "run_model",
                        lambda _value: pytest.fail("model must not start"))
    with pytest.raises(worker.EnvelopeError):
        worker.run(raw)


def test_main_does_not_echo_credential_or_sdk_exception(monkeypatch, capsys):
    secret = "synthetic-private-credential"
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(envelope(api_key=secret))))

    def fail(_value):
        raise RuntimeError(f"SDK error containing {secret}")

    monkeypatch.setattr(worker.harness_sdk_worker, "run_model", fail)
    assert worker.main() == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "RuntimeError" in output.err
    assert secret not in output.err
    assert "SDK error" not in output.err
