"""Probe a configured relay with synthetic text; never print credentials or bodies.

This is an operator diagnostic. It checks the configured model and one minimal
OpenAI Chat Completions streaming request, without following redirects. Optional
flags add non-streaming Chat Completions, Anthropic Messages, or Responses.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


RELAY = "https://tongyimohe.aitoken.credit"
API_BASE = RELAY + "/v1"
MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def settings() -> dict[str, str]:
    path = Path(__file__).resolve().parents[1] / ".env"
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if match:
            result[match.group(1)] = match.group(2).strip().strip('"').strip("'")
    return result


def request(opener, req: Request, *, stream: bool = False) -> tuple[int, str, bytes]:
    try:
        with opener.open(req, timeout=20) as response:
            content_type = response.headers.get("content-type", "").split(";", 1)[0]
            if stream:
                lines = []
                for _ in range(100):
                    line = response.readline(4096)
                    if not line:
                        break
                    lines.append(line)
                    if (line.startswith(b"event: message_stop") or
                            line.strip() == b"data: [DONE]"):
                        break
                body = b"".join(lines)[:65536]
            else:
                body = response.read(65536)
            return response.status, content_type, body
    except HTTPError as exc:
        return exc.code, exc.headers.get("content-type", "").split(";", 1)[0], exc.read(8192)


def category(status: int) -> str:
    if 300 <= status < 400:
        return "redirect_rejected"
    if status == 401:
        return "auth_rejected"
    if status == 403:
        return "permission_denied"
    if status == 404:
        return "route_or_model_missing"
    if status == 400:
        return "request_or_model_rejected"
    if status == 429:
        return "quota_or_rate_limited"
    if 500 <= status:
        return "upstream_failure"
    return "unexpected_status"


def error_shape(body: bytes, content_type: str) -> str:
    if content_type != "application/json":
        return "non_json"
    try:
        parsed = json.loads(body)
        error = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(error, dict):
            raw_type = error.get("type")
            known_types = {"authentication_error", "permission_error",
                           "invalid_request_error", "not_found_error",
                           "rate_limit_error", "api_error"}
            return "json_error_" + (raw_type if raw_type in known_types else "other")
        return "json_other"
    except (ValueError, TypeError):
        return "invalid_json"


def main() -> int:
    config = settings()
    key = config.get("DEEPSEEK_API_KEY", "")
    base = config.get("WINDCATCHER_HARNESS_BASE_URL", "")
    if base != API_BASE or not key:
        print("configuration=missing_or_unexpected")
        return 2
    model = config.get("WINDCATCHER_HARNESS_MODEL", "")
    if model != "gpt-6.1-sol":
        print("configuration=invalid_model")
        return 2
    opener = build_opener(NoRedirect)
    models_req = Request(API_BASE + "/models", headers={
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
    })
    try:
        status, content_type, body = request(opener, models_req)
    except (URLError, OSError, TimeoutError) as exc:
        print("models=network_error:" + type(exc).__name__)
        return 2
    ids: list[str] = []
    if status == 200 and content_type == "application/json":
        try:
            data = json.loads(body)
            ids = [row.get("id") for row in data.get("data", []) if isinstance(row, dict)]
            ids = [value for value in ids if isinstance(value, str) and MODEL_PATTERN.fullmatch(value)]
            print("models=status_200 count=" + str(len(ids)) + " configured_model_listed=" + str(model in ids).lower())
            print("model_ids=" + ",".join(ids[:30]))
        except (ValueError, TypeError, AttributeError):
            print("models=status_200 invalid_json_schema")
    else:
        print("models=status_" + str(status) + " " + category(status))

    messages_ok = False
    if "--check-messages" in sys.argv:
        payload = json.dumps({
            "model": model,
            "max_tokens": 16,
            "stream": True,
            "messages": [{"role": "user", "content": "Synthetic connectivity test. Reply OK."}],
        }).encode("utf-8")
        messages_req = Request(API_BASE + "/messages", data=payload, headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "accept": "text/event-stream",
        }, method="POST")
        try:
            status, content_type, body = request(opener, messages_req, stream=True)
        except (URLError, OSError, TimeoutError) as exc:
            print("messages=network_error:" + type(exc).__name__)
            return 2
        if status == 200 and content_type == "text/event-stream":
            events = re.findall(rb"(?m)^event: ([a-z_]+)\s*$", body)
            names = [event.decode("ascii") for event in events]
            print("messages=status_200 sse=" + ",".join(names[:20]))
            messages_ok = "message_start" in names and "message_stop" in names
        else:
            print("messages=status_" + str(status) + " " +
                  ("unexpected_content_type" if status == 200 else category(status)) +
                  " body=" + error_shape(body, content_type))

    chat_ok = False
    if "--check-openai" in sys.argv and model in ids:
        chat_req = Request(API_BASE + "/chat/completions", data=json.dumps({
            "model": model, "max_completion_tokens": 128,
            "messages": [{"role": "user", "content": "Synthetic connectivity test. Reply OK."}],
        }).encode("utf-8"), headers={
            "Authorization": "Bearer " + key,
            "content-type": "application/json",
        }, method="POST")
        try:
            chat_status, chat_type, chat_body = request(opener, chat_req)
        except (URLError, OSError, TimeoutError) as exc:
            print("chat=network_error:" + type(exc).__name__)
        else:
            if chat_status == 200 and chat_type == "application/json":
                try:
                    parsed = json.loads(chat_body)
                    choices = parsed.get("choices")
                    choice = choices[0] if isinstance(choices, list) and choices else {}
                    complete = (isinstance(choice, dict) and
                                choice.get("finish_reason") == "stop" and
                                bool(choice.get("message", {}).get("content")))
                except (ValueError, TypeError, AttributeError):
                    complete = False
                chat_ok = complete
                print("chat=status_200 completed=" + str(complete).lower())
            else:
                print("chat=status_" + str(chat_status) + " " + category(chat_status) +
                      " body=" + error_shape(chat_body, chat_type))

    stream_ok = False
    if model in ids:
        stream_req = Request(API_BASE + "/chat/completions", data=json.dumps({
            "model": model, "max_completion_tokens": 128, "stream": True,
            "messages": [{"role": "user", "content": "Synthetic connectivity test. Reply OK."}],
        }).encode("utf-8"), headers={
            "Authorization": "Bearer " + key,
            "content-type": "application/json",
            "accept": "text/event-stream",
        }, method="POST")
        try:
            stream_status, stream_type, stream_body = request(opener, stream_req, stream=True)
        except (URLError, OSError, TimeoutError) as exc:
            print("chat_stream=network_error:" + type(exc).__name__)
        else:
            if stream_status == 200 and stream_type == "text/event-stream":
                chunks = [line for line in stream_body.splitlines()
                          if line.startswith(b"data: ")]
                done = any(line == b"data: [DONE]" for line in chunks)
                content = False
                for line in chunks:
                    if line == b"data: [DONE]":
                        continue
                    try:
                        event = json.loads(line[6:])
                        choices = event.get("choices")
                        delta = choices[0].get("delta", {}) if choices else {}
                        content = content or bool(delta.get("content"))
                    except (ValueError, TypeError, AttributeError, IndexError):
                        pass
                print("chat_stream=status_200 done=" + str(done).lower() +
                      " content_present=" + str(content).lower())
                stream_ok = done and content
            else:
                print("chat_stream=status_" + str(stream_status) + " " +
                      category(stream_status) + " body=" +
                      error_shape(stream_body, stream_type))

    if "--check-responses" in sys.argv and model in ids:
        responses_req = Request(API_BASE + "/responses", data=json.dumps({
            "model": model, "max_output_tokens": 128,
            "input": "Synthetic connectivity test. Reply OK.",
        }).encode("utf-8"), headers={
            "Authorization": "Bearer " + key,
            "content-type": "application/json",
        }, method="POST")
        try:
            responses_status, responses_type, responses_body = request(opener, responses_req)
        except (URLError, OSError, TimeoutError) as exc:
            print("responses=network_error:" + type(exc).__name__)
        else:
            if responses_status == 200 and responses_type == "application/json":
                try:
                    parsed = json.loads(responses_body)
                    complete = parsed.get("status") == "completed"
                    incomplete = parsed.get("status") == "incomplete"
                except (ValueError, TypeError, AttributeError):
                    complete = False
                    incomplete = False
                print("responses=status_200 completed=" + str(complete).lower() +
                      " incomplete=" + str(incomplete).lower())
            else:
                print("responses=status_" + str(responses_status) + " " +
                      category(responses_status) + " body=" +
                      error_shape(responses_body, responses_type))
    if "--check-messages" in sys.argv:
        return 0 if messages_ok else 1
    if "--check-openai" in sys.argv:
        return 0 if chat_ok else 1
    return 0 if stream_ok else 1


if __name__ == "__main__":
    sys.exit(main())
