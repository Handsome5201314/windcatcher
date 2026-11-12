"""The relay diagnostic uses only the selected synthetic OpenAI route by default."""

import json
import sys

from scripts import probe_harness_relay as probe


def test_default_probe_uses_chat_stream_without_messages(monkeypatch, capsys):
    key = "synthetic-test-credential"
    monkeypatch.setattr(probe, "settings", lambda: {
        "DEEPSEEK_API_KEY": key,
        "WINDCATCHER_HARNESS_BASE_URL": probe.API_BASE,
        "WINDCATCHER_HARNESS_MODEL": "gpt-6.1-sol",
    })
    monkeypatch.setattr(sys, "argv", ["probe_harness_relay.py"])
    paths = []

    def fake_request(_opener, request, *, stream=False):
        paths.append((request.full_url, stream))
        assert request.get_header("Authorization") == "Bearer " + key
        if request.full_url.endswith("/models"):
            return 200, "application/json", json.dumps({
                "data": [{"id": "gpt-6.1-sol"}],
            }).encode()
        assert request.full_url.endswith("/chat/completions") and stream
        return 200, "text/event-stream", (
            b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\n'
            b'data: [DONE]\n'
        )

    monkeypatch.setattr(probe, "request", fake_request)
    assert probe.main() == 0
    assert [path.rsplit("/", 1)[-1] for path, _ in paths] == ["models", "completions"]
    assert key not in capsys.readouterr().out


def test_probe_rejects_wrong_model_before_network(monkeypatch):
    monkeypatch.setattr(probe, "settings", lambda: {
        "DEEPSEEK_API_KEY": "synthetic-test-credential",
        "WINDCATCHER_HARNESS_BASE_URL": probe.API_BASE,
        "WINDCATCHER_HARNESS_MODEL": "other-model",
    })
    monkeypatch.setattr(probe, "request", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("network must not be used")))
    assert probe.main() == 2
