"""可选的 AI 报告解读适配器。

事实报告始终由 :mod:`pipeline.report` 本地生成。本模块只读取报告中的
白名单统计字段，并把脱敏后的结构化数据发送给用户配置的 OpenAI 兼容接口。
本机/共享模式的 API 密钥可从环境变量读取，也可由环境页暂存到进程内存。
个人账号模式只使用该账号提交的进程内密钥；绝不写入配置文件、日志或病例产物。
"""

from __future__ import annotations

import ipaddress
import http.client
import json
import os
import re
import socket
import threading
import urllib.error
import urllib.request
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from .config import APP_DIR

CONFIG_PATH = APP_DIR / "cases" / ".ai-report-config.json"
API_KEY_ENV = "WINDCATCHER_AI_API_KEY"
ALLOWED_HOSTS_ENV = "WINDCATCHER_AI_ALLOWED_HOSTS"
DEFAULT_MODEL = "gpt-4o-mini"
MAX_RESPONSE_BYTES = 2_000_000
MAX_MODELS = 500
MAX_API_KEY_LENGTH = 4096
_SESSION_KEY_LOCK = threading.Lock()
_SESSION_API_KEY = ""
_SESSION_API_BASE_URL = ""
CURRENT_USER_ID: ContextVar[str | None] = ContextVar("windcatcher_ai_user_id", default=None)
_USER_SESSION_KEYS: dict[str, tuple[str, str]] = {}
_USER_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)


class AIReportError(Exception):
    """可安全展示给用户的 AI 解读错误。"""

    def __init__(self, reason: str, status_code: int = 400, remediation: list[str] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code
        self.remediation = list(remediation or [])

    def to_dict(self) -> dict:
        return {"error": True, "status": "failed", "reason": self.reason, "remediation": self.remediation}


def _valid_url(value: str) -> str:
    value = str(value or "").strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise AIReportError("API URL 必须是 http 或 https 地址，且不能包含账号密码", 400, ["例如 https://api.openai.com/v1"])
    if parsed.query or parsed.fragment:
        raise AIReportError("API URL 不能包含查询参数或片段标识符，避免把密钥写入配置", 400)
    path = parsed.path.rstrip("/")
    for suffix in ("/chat/completions", "/models"):
        if path.endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
            break
    normalized = urlunparse(parsed._replace(path=path or "/", params="", query="", fragment="")).rstrip("/")
    return normalized


def _personal_provider_url(value: str, *, resolve: bool = False) -> str:
    """Restrict personal-account outbound requests to admin-approved public HTTPS hosts."""
    normalized = _valid_url(value)
    if CURRENT_USER_ID.get() is None:
        return normalized
    parsed = urlparse(normalized)
    host = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError as exc:
        raise AIReportError("AI 服务端口无效", 400) from exc
    if parsed.scheme != "https" or port not in (None, 443):
        raise AIReportError("个人账号的 AI 服务必须使用 HTTPS 标准端口", 400)
    if (not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host, re.ASCII)
            or "." not in host or ".." in host):
        raise AIReportError("AI 服务域名无效", 400)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise AIReportError("个人账号不能连接 IP 地址", 400)
    raw_hosts = os.environ.get(ALLOWED_HOSTS_ENV, "")
    allowed = {part.strip().lower() for part in raw_hosts.split(",") if part.strip()}
    if not allowed:
        raise AIReportError("管理员尚未配置 AI 服务允许域名", 403)
    if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", item, re.ASCII)
           or "." not in item or ".." in item for item in allowed):
        raise AIReportError("AI 服务允许域名配置无效", 503)
    if host not in allowed:
        raise AIReportError("该 AI 服务域名未获管理员允许", 403)
    if resolve:
        _resolve_public_addresses(host)
    return normalized


def _resolve_public_addresses(host: str) -> tuple[tuple[int, int, int, tuple], ...]:
    """Resolve once and retain only vetted socket addresses for the connection."""
    try:
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise AIReportError("无法解析 AI 服务域名", 502) from exc
    if not answers:
        raise AIReportError("无法解析 AI 服务域名", 502)
    addresses = []
    for family, socktype, protocol, _, sockaddr in answers:
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except (IndexError, TypeError, ValueError) as exc:
            raise AIReportError("AI 服务域名解析结果无效", 502) from exc
        if not address.is_global:
            raise AIReportError("AI 服务域名解析到非公网地址", 403)
        if (socktype != socket.SOCK_STREAM or
                family not in (socket.AF_INET, socket.AF_INET6) or
                family != (socket.AF_INET if address.version == 4 else socket.AF_INET6)):
            raise AIReportError("AI 服务域名解析结果无效", 502)
        addresses.append((family, socktype, protocol, sockaddr))
    return tuple(addresses)


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise AIReportError("AI 服务重定向已被拒绝", 502)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, addresses: tuple[tuple[int, int, int, tuple], ...], **kwargs):
        super().__init__(host, **kwargs)
        self.addresses = addresses

    def connect(self) -> None:
        if self._tunnel_host:
            raise OSError("个人 AI 请求不得通过代理转发")
        last_error = None
        for family, socktype, protocol, sockaddr in self.addresses:
            sock = None
            try:
                sock = socket.socket(family, socktype, protocol)
                sock.settimeout(self.timeout)
                if self.source_address:
                    sock.bind(self.source_address)
                sock.connect(sockaddr)
                # TLS still verifies the certificate against the approved DNS name.
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                return
            except OSError as exc:
                if sock is not None:
                    sock.close()
                last_error = exc
        raise last_error or OSError("AI 服务没有可连接的公网地址")


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, addresses: tuple[tuple[int, int, int, tuple], ...]):
        super().__init__()
        self.addresses = addresses

    def https_open(self, request):
        return self.do_open(
            lambda host, **kwargs: _PinnedHTTPSConnection(host, self.addresses, **kwargs),
            request, context=self._context,
        )


def _open_provider_request(request: urllib.request.Request, timeout: int):
    if CURRENT_USER_ID.get() is None:
        return urllib.request.urlopen(request, timeout=timeout)
    normalized = _personal_provider_url(request.full_url)
    addresses = _resolve_public_addresses(urlparse(normalized).hostname or "")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _RejectRedirect(), _PinnedHTTPSHandler(addresses),
    )
    return opener.open(request, timeout=timeout)


def _config_path() -> Path:
    user_id = CURRENT_USER_ID.get()
    if user_id is None:
        return CONFIG_PATH
    if not _USER_ID_PATTERN.fullmatch(user_id):
        raise AIReportError("账号标识无效", 403)
    return CONFIG_PATH.parent / ".ai-report-users" / f"{user_id}.json"


def _read_saved_config() -> dict:
    try:
        value = json.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def get_config() -> dict:
    """返回不含密钥的安全配置状态。"""
    saved = _read_saved_config()
    user_id = CURRENT_USER_ID.get()
    raw_url = (os.environ.get("WINDCATCHER_AI_API_URL") if user_id is None else None) or saved.get("base_url") or ""
    try:
        base_url = _valid_url(raw_url) if raw_url else ""
    except AIReportError:
        base_url = ""
    model = str((os.environ.get("WINDCATCHER_AI_MODEL") if user_id is None else None) or saved.get("model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    key_configured = bool(_active_api_key(base_url))
    provider_error = None
    if user_id is not None and base_url:
        try:
            _personal_provider_url(base_url)
        except AIReportError as exc:
            provider_error = exc.reason
    configured = bool(base_url and key_configured and not provider_error)
    return {
        "status": "available" if configured else "unavailable",
        "configured": configured,
        "provider": "OpenAI 兼容接口",
        "base_url": base_url,
        "model": model,
        "api_key_configured": key_configured,
        "reason": "已配置，可生成 AI 解读" if configured else provider_error or "请填写 API URL 和 API 密钥",
        "key_env": API_KEY_ENV if user_id is None else "",
    }


def set_session_api_key(api_key: str | None, base_url: str) -> None:
    """Keep a submitted key bound to one user and URL in this process only."""
    normalized_base = _valid_url(base_url)
    normalized = str(api_key or "").strip()
    if len(normalized) > MAX_API_KEY_LENGTH:
        raise AIReportError("API 密钥长度不正确", 400)
    if not normalized:
        return
    with _SESSION_KEY_LOCK:
        user_id = CURRENT_USER_ID.get()
        if user_id is not None:
            if not _USER_ID_PATTERN.fullmatch(user_id):
                raise AIReportError("账号标识无效", 403)
            _USER_SESSION_KEYS[user_id] = (normalized_base, normalized)
            return
        global _SESSION_API_BASE_URL, _SESSION_API_KEY
        _SESSION_API_KEY = normalized
        _SESSION_API_BASE_URL = normalized_base


def _active_api_key(base_url: str | None = None) -> str:
    """Use only the current user's key in personal mode; retain legacy fallback."""
    normalized_base = _valid_url(base_url) if base_url else None
    with _SESSION_KEY_LOCK:
        user_id = CURRENT_USER_ID.get()
        if user_id is not None:
            if not _USER_ID_PATTERN.fullmatch(user_id):
                raise AIReportError("账号标识无效", 403)
            saved = _USER_SESSION_KEYS.get(user_id)
            return saved[1] if saved and (normalized_base is None or saved[0] == normalized_base) else ""
        if _SESSION_API_KEY and (normalized_base is None or _SESSION_API_BASE_URL == normalized_base):
            return _SESSION_API_KEY
    return os.environ.get(API_KEY_ENV, "").strip()


def forget_user_key(user_id: str) -> None:
    """Discard a disabled account's transient key without touching its config."""
    with _SESSION_KEY_LOCK:
        _USER_SESSION_KEYS.pop(user_id, None)


def save_config(base_url: str, model: str | None = None, api_key: str | None = None) -> dict:
    """保存非敏感连接配置；api_key 仅保存在该账号的进程内存。"""
    normalized = _personal_provider_url(base_url)
    if api_key is not None and len(str(api_key or "").strip()) > MAX_API_KEY_LENGTH:
        raise AIReportError("API 密钥长度不正确", 400)
    model = str(model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    if len(model) > 160 or not re.fullmatch(r"[A-Za-z0-9_.:/-]+", model):
        raise AIReportError("模型名称格式不正确", 400, ["模型名称只允许字母、数字、点、下划线、冒号、斜线和短横线"])
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"base_url": normalized, "model": model}, ensure_ascii=False, indent=2), encoding="utf-8")
    if api_key is not None:
        set_session_api_key(api_key, normalized)
    return get_config()


def _models_endpoint(base_url: str) -> str:
    """Return the OpenAI-compatible models endpoint for a configured base URL."""
    return _valid_url(base_url) + "/models"


def _redact_api_key(text: str, api_key: str) -> str:
    """Keep upstream errors useful without echoing a submitted secret."""
    if not text:
        return ""
    return text.replace(api_key, "[已隐藏密钥]")[:800]


def list_models(base_url: str, api_key: str) -> dict:
    """Fetch model IDs from an OpenAI-compatible endpoint using a transient key.

    The key is used only while this request is active. It is never passed to
    ``save_config`` or written to disk. The response deliberately contains a
    small public subset of each model record so provider metadata cannot leak
    into the UI or logs.
    """
    normalized = _personal_provider_url(base_url)
    normalized_key = str(api_key or "").strip() or _active_api_key(normalized)
    if not normalized_key:
        raise AIReportError("请填写 API 密钥", 400, ["密钥仅用于本次获取模型，不会保存到病例或配置文件"])
    if len(normalized_key) > MAX_API_KEY_LENGTH:
        raise AIReportError("API 密钥长度不正确", 400)
    endpoint = _models_endpoint(normalized)
    request = urllib.request.Request(
        endpoint,
        method="GET",
        headers={"Authorization": f"Bearer {normalized_key}", "Accept": "application/json"},
    )
    try:
        with _open_provider_request(request, timeout=30) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = _redact_api_key(exc.read(800).decode("utf-8", errors="replace"), normalized_key)
        raise AIReportError(f"获取模型失败，AI 服务返回 HTTP {exc.code}：{detail}", 502)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AIReportError(f"无法连接 AI 服务：{exc}", 502)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise AIReportError("模型列表响应过大，未读取", 502)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise AIReportError("AI 服务返回的模型列表不是 JSON", 502)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise AIReportError("AI 服务返回的模型列表格式无效（需要 data 数组）", 502)
    models = []
    seen = set()
    for item in payload["data"][:MAX_MODELS]:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not isinstance(model_id, str):
            continue
        model_id = model_id.strip()
        if not model_id or model_id in seen or len(model_id) > 160:
            continue
        seen.add(model_id)
        record = {"id": model_id}
        if isinstance(item.get("owned_by"), str):
            record["owned_by"] = item["owned_by"][:120]
        models.append(record)
    models.sort(key=lambda item: item["id"].lower())
    return {"status": "ok", "base_url": normalized, "models": models, "count": len(models)}


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _metric(source: dict, fields: tuple[str, ...]) -> dict:
    return {field: source.get(field) for field in fields if field in source and (_number(source.get(field)) is not None or isinstance(source.get(field), str))}


def build_safe_payload(report: dict) -> dict:
    """从 report.json 提取固定白名单，禁止路径、患者标识和原始文件进入请求。"""
    if not isinstance(report, dict):
        raise AIReportError("报告数据格式无效", 400)
    gate = report.get("gate") if isinstance(report.get("gate"), dict) else {}
    baseline = report.get("baseline_flow") if isinstance(report.get("baseline_flow"), dict) else {}
    pressure = baseline.get("pressure_drop") if isinstance(baseline.get("pressure_drop"), dict) else {}
    distribution = baseline.get("velocity_distribution") if isinstance(baseline.get("velocity_distribution"), dict) else {}
    velocity = distribution.get("velocity") if isinstance(distribution.get("velocity"), dict) else {}
    pressure_distribution = distribution.get("pressure") if isinstance(distribution.get("pressure"), dict) else {}
    split = baseline.get("left_right_flow") if isinstance(baseline.get("left_right_flow"), dict) else {}
    narrow = baseline.get("narrow_regions") if isinstance(baseline.get("narrow_regions"), dict) else {}
    comparison = report.get("obstruction_comparison") if isinstance(report.get("obstruction_comparison"), dict) else {}
    items = []
    for item in comparison.get("items", []):
        if not isinstance(item, dict):
            continue
        flow = item.get("flow_report") if isinstance(item.get("flow_report"), dict) else {}
        scenario_pressure = flow.get("pressure_drop") if isinstance(flow.get("pressure_drop"), dict) else {}
        scenario_dist = flow.get("velocity_distribution") if isinstance(flow.get("velocity_distribution"), dict) else {}
        scenario_velocity = scenario_dist.get("velocity") if isinstance(scenario_dist.get("velocity"), dict) else {}
        comparison_delta = item.get("comparison") if isinstance(item.get("comparison"), dict) else {}
        items.append({
            "label": str(item.get("label", "方案"))[:120],
            "position": str(item.get("position", "未记录"))[:120],
            "size_mm": item.get("size_mm") if _number(item.get("size_mm")) is not None else None,
            "occlusion_percent": item.get("occlusion_percent") if _number(item.get("occlusion_percent")) is not None else None,
            "flow_lpm": flow.get("flow_lpm") if _number(flow.get("flow_lpm")) is not None else None,
            "pressure_drop": _metric(scenario_pressure, ("value", "unit")),
            "velocity_p95": scenario_velocity.get("p95") if _number(scenario_velocity.get("p95")) is not None else None,
            "delta_pressure_percent": comparison_delta.get("delta_pressure_percent") if _number(comparison_delta.get("delta_pressure_percent")) is not None else None,
            "delta_flow_percent": comparison_delta.get("delta_flow_percent") if _number(comparison_delta.get("delta_flow_percent")) is not None else None,
        })
    design = report.get("design_reference") if isinstance(report.get("design_reference"), dict) else {}
    return {
        "report_generated_at": str(report.get("generated_at", ""))[:40],
        "gate": {
            "eligible": bool(gate.get("eligible")),
            "status": str(gate.get("status", ""))[:80],
            "reasons": [str(item)[:240] for item in gate.get("reasons", []) if isinstance(item, str)][:20],
        },
        "baseline_flow": {
            "status": str(baseline.get("status", "missing"))[:60],
            "data_status": str(baseline.get("data_status", "missing"))[:60],
            "validation_status": str(baseline.get("validation_status", "not_run"))[:60],
            "flow_lpm": baseline.get("flow_lpm") if _number(baseline.get("flow_lpm")) is not None else None,
            "pressure_drop": _metric(pressure, ("value", "unit")),
            "velocity": _metric(velocity, ("min", "mean", "p95", "max", "sample_count", "unit")),
            "pressure_distribution": _metric(pressure_distribution, ("min", "mean", "p95", "max", "sample_count", "unit")),
            "left_right_flow": _metric(split, ("left_lpm", "right_lpm", "left_fraction", "right_fraction", "status")),
            "narrow_regions": {"status": str(narrow.get("status", "not_available"))[:60], "reason": str(narrow.get("reason", "未生成"))[:240]},
        },
        "obstruction_comparison": {
            "status": str(comparison.get("status", "not_available"))[:60],
            "items": items[:50],
            "reason": str(comparison.get("reason", "尚未生成独立鼻塞方案"))[:240],
        },
        "design_reference": {
            "status": str(design.get("status", "pending"))[:60],
            "uncertainty": [str(item)[:240] for item in design.get("uncertainty", []) if isinstance(item, str)][:20],
        },
    }


def _prompt(payload: dict, mode: str) -> list[dict]:
    pending = not bool(payload.get("gate", {}).get("eligible"))
    system = (
        "你是鼻腔 CFD 研究原型的报告解读助手。只能解释用户提供的结构化真实观测值，不能编造、估算或补全任何数字。"
        "缺失字段必须写‘未计算/未生成’。不要诊断、不要给医疗建议。"
        + ("当前结果闸门未通过：不得给出确定性的鼻塞设计推荐，只能列出待复核项。" if pending else "")
        + "只返回 JSON，不要 Markdown 代码围栏。JSON 必须含 summary（字符串）、observations（字符串数组）、limitations（字符串数组）、"
        "design_reference（对象，含 status、text、recommendation_allowed 布尔值）、source_fields（字符串数组）。"
    )
    user = {"mode": mode, "facts": payload}
    return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(user, ensure_ascii=False)}]


def _extract_content(response: dict) -> str:
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise AIReportError("模型响应缺少 choices[0].message.content", 502)
    if isinstance(content, list):
        content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    if not isinstance(content, str) or not content.strip():
        raise AIReportError("模型返回了空的解读内容", 502)
    return content.strip()


def _parse_interpretation(content: str, pending: bool) -> dict:
    candidate = content.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        result = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise AIReportError(f"模型没有返回合法 JSON：{exc.msg}", 502)
    required = ("summary", "observations", "limitations", "design_reference", "source_fields")
    if not isinstance(result, dict) or any(field not in result for field in required):
        raise AIReportError("模型响应缺少必需字段，未保存解读", 502)
    if not isinstance(result["summary"], str) or not isinstance(result["observations"], list) or not isinstance(result["limitations"], list) or not isinstance(result["source_fields"], list) or not isinstance(result["design_reference"], dict):
        raise AIReportError("模型响应字段类型不符合约定，未保存解读", 502)
    design = result["design_reference"]
    if not isinstance(design.get("status"), str) or not isinstance(design.get("text"), str) or not isinstance(design.get("recommendation_allowed"), bool):
        raise AIReportError("模型响应的 design_reference 字段不完整，未保存解读", 502)
    if pending and design["recommendation_allowed"]:
        raise AIReportError("结果闸门未通过，模型返回了不允许的确定性设计推荐", 502)
    return result


def _call_model(config: dict, messages: list[dict]) -> dict:
    api_key = _active_api_key(config["base_url"])
    if not api_key:
        raise AIReportError("未配置 AI API 密钥", 409, ["到环境页填写 API 密钥并点击获取模型或保存连接配置"])
    endpoint = _personal_provider_url(config["base_url"]) + "/chat/completions"
    body = json.dumps({"model": config["model"], "temperature": 0, "messages": messages}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(endpoint, data=body, method="POST", headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    try:
        with _open_provider_request(request, timeout=90) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        detail = _redact_api_key(exc.read(800).decode("utf-8", errors="replace"), api_key)
        raise AIReportError(f"AI 服务返回 HTTP {exc.code}：{detail}", 502)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AIReportError(f"无法连接 AI 服务：{exc}", 502)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise AIReportError("AI 服务响应过大，未保存解读", 502)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise AIReportError("AI 服务返回的不是 JSON", 502)


def _provider_usage(response: dict) -> dict:
    """Only retain reported token counters; never estimate a missing bill."""
    raw = response.get("usage")
    if not isinstance(raw, dict):
        return {"available": False, "source": "provider_response"}
    result = {key: value for key in ("prompt_tokens", "completion_tokens", "total_tokens")
              if type(value := raw.get(key)) is int and value >= 0}
    for group, allowed in (("prompt_tokens_details", ("cached_tokens",)),
                           ("completion_tokens_details", ("reasoning_tokens",))):
        detail = raw.get(group)
        if isinstance(detail, dict):
            counters = {key: value for key in allowed if type(value := detail.get(key)) is int and value >= 0}
            if counters:
                result[group] = counters
    return {"available": bool(result), "source": "provider_response", **result}


def generate(case_id: str, report: dict, mode: str = "observations") -> dict:
    config = get_config()
    if not config["base_url"]:
        raise AIReportError("尚未配置 AI API URL", 409, ["到环境页填写 OpenAI 兼容 API URL 并保存"])
    if not config["api_key_configured"]:
        raise AIReportError("未配置 AI API 密钥", 409, ["到环境页填写 API 密钥并点击获取模型或保存连接配置"])
    safe_payload = build_safe_payload(report)
    response = _call_model(config, _prompt(safe_payload, mode))
    interpretation = _parse_interpretation(_extract_content(response), not bool(safe_payload["gate"]["eligible"]))
    result = {
        "case_id": case_id,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "provider": config["provider"],
        "model": config["model"],
        "mode": mode,
        "usage": _provider_usage(response),
        "source": {"report_generated_at": safe_payload["report_generated_at"], "fields": safe_payload},
        "interpretation": interpretation,
        "disclaimer": "AI 解读仅基于本页脱敏统计，不能替代 CFD 工程复核或医学判断。",
    }
    from .cases import case_dir
    output = case_dir(case_id) / "report" / "ai-interpretation.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def load(case_id: str) -> dict | None:
    from .cases import case_dir
    path = case_dir(case_id) / "report" / "ai-interpretation.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None
