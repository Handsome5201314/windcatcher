"""Hosted entry point: public introduction, password protected research app.

The local server remains unchanged. Secrets must be injected at runtime; importing
this module neither loads local .env files nor opens any patient data.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

COOKIE = "__Host-windcatcher"
PATIENT_COOKIE = "windcatcher_patient_session"
SESSION_SECONDS = 8 * 60 * 60
ROOT = Path(__file__).resolve().parent


class AccessControl:
    def __init__(self):
        mode = os.environ.get("WINDCATCHER_MULTIUSER", "0")
        if mode not in {"0", "1"}:
            raise RuntimeError("WINDCATCHER_MULTIUSER 仅支持 0 或 1")
        self.multiuser = mode == "1"
        self.identity = None
        from pipeline.config import CASES_DIR
        identity_dir = Path(os.environ.get("WINDCATCHER_IDENTITY_DIR") or ROOT / ".local-identity")
        self.identity_path = identity_dir / "identity.sqlite3"
        self.identity_marker = CASES_DIR / ".multiuser-identity-enabled"
        if not self.multiuser and (self.identity_path.exists() or self.identity_marker.exists()):
            raise RuntimeError("已启用个人账号，拒绝回退到共享账号模式；请先恢复多用户身份库")
        self.deployment = os.environ.get("WINDCATCHER_DEPLOYMENT", "cloud").strip().lower()
        if self.deployment not in {"cloud", "public"}:
            raise RuntimeError("WINDCATCHER_DEPLOYMENT 仅支持 cloud 或 public")
        prefix = "WINDCATCHER_PUBLIC" if self.deployment == "public" else "WINDCATCHER_CLOUD"
        self.username = os.environ.get(f"{prefix}_USERNAME", "").strip()
        self.password = os.environ.get(f"{prefix}_PASSWORD", "")
        secret = os.environ.get(f"{prefix}_SESSION_SECRET", "")
        if not self.username or len(self.password) < 16 or len(secret) < 32:
            raise RuntimeError(f"{prefix} 访问配置缺失：需设置账号、至少 16 位密码和至少 32 位会话密钥")
        self.key = hmac.digest(secret.encode(), (self.username + "\0" + self.password).encode(), "sha256")
        self.failures: dict[str, list[float]] = {}
        self.share_failures: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def session(self, expires: int | None = None) -> str:
        payload = f"{expires if expires is not None else int(time.time()) + SESSION_SECONDS}.{secrets.token_hex(12)}"
        return payload + "." + hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()

    def authenticated(self, request: Request) -> bool:
        if self.multiuser:
            return self.principal(request) is not None
        value = request.cookies.get(COOKIE, "")
        if len(value) > 160:
            return False
        try:
            expiry, nonce, signature = value.split(".")
            valid_time = time.time() < int(expiry) <= time.time() + SESSION_SECONDS + 5
        except (ValueError, TypeError):
            return False
        expected = hmac.new(self.key, f"{expiry}.{nonce}".encode(), hashlib.sha256).hexdigest()
        return valid_time and hmac.compare_digest(signature.encode(), expected.encode())

    def principal(self, request: Request) -> dict | None:
        if not self.multiuser:
            return {"id": self.username, "username": self.username,
                    "global_role": "admin", "kind": "human"} if self.authenticated(request) else None
        return self.identity.resolve_session(request.cookies.get(COOKIE))

    def actor_name(self, request: Request) -> str:
        principal = getattr(request.state, "principal", None)
        return principal["id"] if self.multiuser and principal else self.username

    def login(self, address: str, username: str, password: str) -> str:
        # Bound memory and serialize check/update so concurrent attempts share the limit.
        now = time.monotonic()
        with self.lock:
            self.failures = {key: [t for t in times if now - t < 900]
                             for key, times in self.failures.items() if times and now - times[-1] < 900}
            attempts = self.failures.get(address, [])
            if len(attempts) >= 5 or sum(map(len, self.failures.values())) >= 1000:
                return "limited"
            if self.multiuser:
                user_ok = self.identity.authenticate(username, password) is not None
            else:
                user_ok = (hmac.compare_digest(username.encode(), self.username.encode())
                           and hmac.compare_digest(password.encode(), self.password.encode()))
            if user_ok:
                self.failures.pop(address, None)
                return "ok"
            self.failures[address] = attempts + [now]
            return "invalid"

    def patient_session(self, share_id: str, expires_at: int) -> str:
        expiry = min(int(time.time()) + SESSION_SECONDS, expires_at)
        payload = f"{share_id}.{expiry}.{secrets.token_hex(12)}"
        return payload + "." + hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()

    def patient_authenticated(self, request: Request, share_id: str, expires_at: int) -> bool:
        value = request.cookies.get(PATIENT_COOKIE, "")
        if len(value) > 160:
            return False
        try:
            identity, expiry, nonce, signature = value.split(".")
            valid = identity == share_id and time.time() < int(expiry) <= min(
                expires_at, time.time() + SESSION_SECONDS + 5)
        except (ValueError, TypeError):
            return False
        expected = hmac.new(self.key, f"{identity}.{expiry}.{nonce}".encode(), hashlib.sha256).hexdigest()
        return valid and hmac.compare_digest(signature, expected)

    def check_share_limit(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            if len(self.share_failures) > 1000:
                self.share_failures = {name: [t for t in times if now - t < 900]
                                       for name, times in self.share_failures.items()
                                       if times and now - times[-1] < 900}
                if len(self.share_failures) > 1000 and key not in self.share_failures:
                    return False
            attempts = [t for t in self.share_failures.get(key, []) if now - t < 900]
            self.share_failures[key] = attempts
            return len(attempts) < 5

    def mark_share_failure(self, key: str) -> None:
        with self.lock:
            self.share_failures.setdefault(key, []).append(time.monotonic())

    def clear_share_failure(self, key: str) -> None:
        with self.lock:
            self.share_failures.pop(key, None)


class ReviewBody(BaseModel):
    operator: str = Field(min_length=2, max_length=80)
    stage: str
    action: str
    reason: str = Field(min_length=4, max_length=1000)


class CfdReviewBody(BaseModel):
    expected_signature: str = Field(min_length=64, max_length=64)
    kind: str
    reviewer: str = Field(min_length=2, max_length=80)
    note: str = Field(min_length=10, max_length=2000)
    evidence_ref: str = Field(min_length=8, max_length=200)
    decision: str | None = None
    grids: list[dict] = Field(default_factory=list)


class PublishBody(BaseModel):
    reviewer: str = Field(min_length=2, max_length=80)
    review_note: str = Field(min_length=10, max_length=1000)
    days: int = Field(default=7, ge=1, le=30)


class TestPublishBody(BaseModel):
    operator: str = Field(min_length=2, max_length=80)
    reason: str = Field(min_length=10, max_length=1000)
    recipient_confirmed: bool


class UnlockBody(BaseModel):
    access_code: str = Field(min_length=1, max_length=64)


class CreateUserBody(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=16, max_length=1024)
    global_role: Literal["admin", "member"] = "member"


class GrantBody(BaseModel):
    role: Literal["viewer", "editor", "reviewer", "owner"]


class OperatorPreferencesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0, strict=True)
    preferences: dict


class OperatorRecipeCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec: dict


class OperatorRecipeUpdateBody(OperatorRecipeCreateBody):
    expected_version: int = Field(ge=1, strict=True)


class OperatorRecipeRevokeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, strict=True)


def _same_origin(request: Request, expected_origin: str) -> bool:
    origin = request.headers.get("origin")
    # Trust the configured deployment origin, never a client-supplied forwarded host.
    expected = expected_origin or str(request.base_url)
    return origin.rstrip("/") == expected.rstrip("/") if origin else request.headers.get("sec-fetch-site") != "cross-site"


def _local_route(path: str, method: str) -> bool:
    path = path.rstrip("/")
    return (any(path == prefix or path.startswith(prefix + "/") for prefix in (
        "/api/monitor", "/api/environment", "/api/automation", "/api/pick-directory", "/api/fetch",
    )) or (path == "/api/cases" and method == "POST") or path.endswith("/open-slicer"))


def _supported_delete_override(path: str) -> bool:
    """Only these exact authenticated DELETE routes cross the public gateway."""
    return bool(re.fullmatch(
        r"/api/cases/case-\d{8}-\d{6}-[0-9a-f]{4}(?:/grants/[0-9a-f]{32})?", path
    ))


def create_app() -> FastAPI:
    access = AccessControl()  # Fail closed before starting the underlying research server.
    mcp_token = os.environ.get("WINDCATCHER_MCP_TOKEN", "")
    if mcp_token and not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", mcp_token):
        raise RuntimeError("WINDCATCHER_MCP_TOKEN 格式无效")
    origin_key = "WINDCATCHER_PUBLIC_URL" if access.deployment == "public" else "WINDCATCHER_CLOUD_PUBLIC_ORIGIN"
    origin = os.environ.get(origin_key, "").rstrip("/")
    if access.deployment == "public" and not origin:
        raise RuntimeError("WINDCATCHER_PUBLIC_URL 必须是可信的 HTTPS 站点源")
    if origin:
        parsed = urlsplit(origin)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise RuntimeError(f"{origin_key} 必须是可信的 HTTPS 站点源")
    import server
    from pipeline import activity_monitor, ai_report, cases, cfd_reviews, endplane_history, harness_advice, harness_projection, operator_recipe_candidates, patient_shares, reference_cases, reference_routes
    from pipeline.access_identity import CASE_ID, IdentityError, IdentityStore
    from pipeline.access_policy import classify
    from pipeline.cases import list_cases
    from pipeline.errors import PipelineError
    from pipeline.operator_preferences import OperatorPreferencesStore, PreferenceConflict, PreferenceError
    from pipeline.operator_recipes import (
        OperatorRecipesStore, RecipeConflict, RecipeError, RecipeNotFound, RecipeRevoked,
    )

    def share_error(exc: patient_shares.ShareError):
        raise HTTPException(exc.status, detail={"reason": exc.reason}) from exc

    def cfd_review_error(exc: cfd_reviews.CfdReviewError):
        raise HTTPException(exc.status, detail={"reason": exc.reason}) from exc

    @asynccontextmanager
    async def lifespan(_application):
        async with server.app.router.lifespan_context(server.app):
            yield

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.access = access
    if access.multiuser:
        if access.identity_marker.exists() and not access.identity_path.is_file():
            raise RuntimeError("病例数据仍在，但个人账号库缺失；请从备份恢复身份库")
        access.identity = IdentityStore()
        access.identity.bootstrap_admin(access.username, access.password,
                                        [row["case_id"] for row in list_cases()])
        app.state.operator_preferences = OperatorPreferencesStore()
        app.state.operator_recipes = OperatorRecipesStore()
        # Persist the enablement marker only after the identity store is ready.
        # If bootstrap fails, startup still fails closed because the DB exists,
        # without leaving a marker that demands restoring a never-ready store.
        access.identity_marker.parent.mkdir(parents=True, exist_ok=True)
        try:
            with access.identity_marker.open("x", encoding="utf-8") as marker:
                marker.write("windcatcher-multiuser-v1\n")
        except FileExistsError:
            pass
        app.state.identity = access.identity
        # The background worker must recheck the same case grant after a
        # queued request starts and again after every Harness model call.
        server.app.state.orchestration_authorize = lambda actor_id, case_id: (
            access.identity.authorize_case(actor_id, case_id, "edit"))

    @app.exception_handler(patient_shares.ShareError)
    async def display_error(_request, exc):
        return JSONResponse({'detail': {'reason': exc.reason}}, status_code=exc.status)

    reference_routes.install(app, server, access, ROOT)

    @app.exception_handler(PipelineError)
    async def pipeline_error(_request: Request, exc: PipelineError):
        return JSONResponse({"detail": {"reason": exc.reason}}, status_code=409)

    def denied(status: int, reason: str) -> JSONResponse:
        return JSONResponse({"detail": {"reason": reason}}, status_code=status)

    async def authorize_multi(request: Request, policy, principal: dict | None,
                              mcp_access: bool) -> Response | None:
        kind = policy.kind
        if kind == "public":
            return None
        if principal is None:
            return denied(401, "请先登录研究工作台")
        if kind == "deny":
            return denied(403, "此入口未开放")
        actor_id = principal["id"]
        human = principal["kind"] == "human"
        if kind == "admin":
            return None if human and principal["global_role"] == "admin" else denied(403, "需要账号管理员权限")
        if kind == "session":
            return None if human else denied(403, "服务身份不能打开工作台")
        if kind in {"operator_preferences", "operator_recipes"}:
            return None if human else denied(403, "服务身份不能访问个人设置")
        if kind == "provider":
            return None if human else denied(403, "AI 配置仅供操作者使用")
        if kind == "monitor_read":
            if request.url.path == "/api/automation/status":
                return None if mcp_access else denied(403, "自动化状态仅供本机 MCP 使用")
            return None if human and principal["global_role"] == "admin" else denied(403, "操作日志仅供管理员查看")
        if kind == "monitor_ingest":
            return None if human else denied(403, "服务身份不能写入浏览器事件")
        if kind in {"case_list", "case_create", "upload_create", "reference_candidates"}:
            return None if human else denied(403, "此入口仅供操作者使用")
        if kind in {"orchestration_submit", "orchestration_batch"}:
            return None if human else denied(403, "逐例编排仅供操作者使用")
        if kind == "upload":
            return None if human and access.identity.authorize_upload(actor_id, policy.resource_id) else denied(404, "上传任务不存在")
        if kind == "case":
            capability = "manage" if policy.capability == "owner" else policy.capability
            if (not human and not mcp_access) or not access.identity.authorize_case(actor_id, policy.case_id, capability):
                return denied(404, "病例不存在或无权访问")
            if cases.deletion_pending(policy.case_id):
                if not (request.method == "DELETE" and policy.capability == "owner"
                        and cases.deletion_owner(policy.case_id) == actor_id):
                    return denied(404, "病例正在删除，仅原所有者可以重试删除")
            if request.url.path.endswith("/ai-report") and request.method == "POST":
                if not human:
                    return denied(403, "AI 解读仅供操作者使用")
            return None
        if kind == "reference_revoke":
            try:
                source = await run_in_threadpool(reference_cases.get_entry, policy.resource_id)
            except reference_cases.ReferenceError:
                return denied(404, "参考病例不存在或无权访问")
            return None if human and access.identity.authorize_case(actor_id, source["case_id"], "review") else denied(404, "参考病例不存在或无权访问")
        if kind in {"automation_cases", "automation_submit", "automation_batch"}:
            if not mcp_access:
                return denied(403, "自动化接口仅供本机 MCP 使用")
            if kind == "automation_batch":
                queue = getattr(server.app.state, "batch_queue", None)
                if queue is None:
                    return denied(503, "批量队列未运行")
                try:
                    batch = await run_in_threadpool(queue.get, policy.resource_id)
                except PipelineError:
                    return denied(404, "批量任务不存在或无权访问")
                case_ids = [item["input"]["case_id"] for item in batch["items"]]
                if not case_ids or len(access.identity.allowed_case_ids(actor_id, case_ids, policy.capability)) != len(case_ids):
                    return denied(404, "批量任务不存在或无权访问")
            return None
        return denied(403, "此入口未开放")

    @app.middleware("http")
    async def protect(request: Request, call_next):
        path = request.url.path
        automation_path = path == "/api/automation" or path.startswith("/api/automation/")
        # The local service identity may inspect/propose anonymous learning
        # drafts, but cannot supply the human feedback that labels them.
        learning_match = re.fullmatch(
            r"/api/cases/([^/]+)/segment/endplanes/learning(/(?:propose|auto-propose|auto-propose-bilateral))?", path, re.ASCII)
        learning_mcp_path = bool(learning_match and CASE_ID.fullmatch(learning_match.group(1)) and (
            request.method in {"GET", "HEAD"} and learning_match.group(2) is None
            or request.method == "POST" and learning_match.group(2) in {"/propose", "/auto-propose", "/auto-propose-bilateral"}))
        segmentation_match = re.fullmatch(
            r"/api/cases/([^/]+)/segment/learning(/candidate)?", path, re.ASCII)
        learning_mcp_path = learning_mcp_path or bool(
            segmentation_match and CASE_ID.fullmatch(segmentation_match.group(1)) and (
                request.method in {"GET", "HEAD"} and segmentation_match.group(2) is None
                or request.method == "POST" and segmentation_match.group(2) == "/candidate"))
        model_match = re.fullmatch(
            r'/api/cases/([^/]+)/segment/models(?:/(model-[0-9a-f]{32})(/cancel)?)?', path, re.ASCII)
        learning_mcp_path = learning_mcp_path or bool(
            model_match and CASE_ID.fullmatch(model_match.group(1)) and (
                request.method in {'GET', 'HEAD'} and model_match.group(3) is None
                or request.method == 'POST' and (model_match.group(2) is None or model_match.group(3) == '/cancel')))
        mcp_access = (
            access.deployment == "public" and (automation_path or learning_mcp_path) and bool(mcp_token)
            and request.client is not None and request.client.host in {"127.0.0.1", "::1"}
            and request.url.hostname in {"127.0.0.1", "localhost", "::1"}
            and not any(name in request.headers for name in (
                "forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
                "x-real-ip", "cf-connecting-ip", "origin", "sec-fetch-site",
            ))
            and hmac.compare_digest(request.headers.get("x-windcatcher-mcp-token", ""), mcp_token)
        )
        activity_path = path == '/api/monitor' or path.startswith('/api/monitor/')
        activity_allowed = access.deployment == 'public' and (
            path == '/api/monitor/status' and request.method == 'GET'
            or path == '/api/monitor/events' and request.method in {'GET', 'POST'}
        )
        share_page = re.fullmatch(r"/share/[A-Za-z0-9_-]{43}", path)
        share_model = re.fullmatch(r"/share/[A-Za-z0-9_-]{43}/(?:model|metadata)", path)
        share_unlock = re.fullmatch(r"/share/[A-Za-z0-9_-]{43}/unlock", path)
        patient_assets = {"/static/patient-view.css", "/static/patient-view.js",
                          "/static/vendor/three.min.js", "/static/vendor/OrbitControls.js",
                          "/static/cfd-viewer.js", "/static/reference-view.js"}
        reference_public = path in {'/references', '/api/references'} or bool(
            re.fullmatch(r'/api/references/[0-9a-f]{32}/model', path))
        public = (path in {"/", "/login", "/healthz", "/static/app.css", "/static/assets/logo-badge.svg", *patient_assets}
                  and request.method in {"GET", "HEAD"}) or bool(
                      (share_page or share_model or reference_public) and request.method == "GET"
                      or share_unlock and request.method == "POST")
        if access.multiuser:
            override = request.headers.get("x-windcatcher-method")
            if (activity_path or request.method not in {"GET", "HEAD", "OPTIONS"}) and not _same_origin(request, origin):
                response = denied(403, "不接受其他站点的请求")
            elif override and not (request.method == "POST" and override == "DELETE"
                                   and _supported_delete_override(path)):
                response = denied(400, "不支持的云端请求方法")
            else:
                if override:
                    request.scope["method"] = "DELETE"
                policy = classify(path, request.method)
                principal = access.identity.mcp_principal() if mcp_access else access.principal(request)
                request.state.principal = principal
                rejected = await authorize_multi(request, policy, principal, mcp_access)
                if rejected:
                    response = rejected
                elif _local_route(path, request.method) and not (mcp_access or policy.kind in {"monitor_read", "monitor_ingest"}):
                    response = denied(403, "云端不提供本机目录、安装或桌面操作，请使用浏览器上传")
                else:
                    headers = [(name, value) for name, value in request.scope["headers"]
                               if name.lower() != b"x-windcatcher-session"]
                    if principal:
                        headers.append((b"x-windcatcher-session", principal["id"].encode("ascii")))
                    request.scope["headers"] = headers
                    actor_token = endplane_history.CURRENT_ACTOR_ID.set(principal["id"] if principal else None)
                    ai_user_token = ai_report.CURRENT_USER_ID.set(
                        principal["id"] if principal and principal["kind"] == "human" else None)
                    try:
                        response = await call_next(request)
                        if policy.kind == "upload" and (response.status_code in {404, 410}
                                or (path.endswith("/cancel") and response.status_code == 200)):
                            try:
                                access.identity.forget_unfinished_upload(policy.resource_id, principal["id"])
                            except Exception as exc:
                                server._MONITOR_LOGGER.error("Upload ownership cleanup failed: %s", type(exc).__name__)
                                response = denied(503, "上传任务清理未完成，请再次重试")
                    finally:
                        ai_report.CURRENT_USER_ID.reset(ai_user_token)
                        endplane_history.CURRENT_ACTOR_ID.reset(actor_token)
        elif (activity_path or request.method not in {"GET", "HEAD", "OPTIONS"}) and not _same_origin(request, origin):
            response = JSONResponse({"detail": {"reason": "不接受其他站点的请求"}}, status_code=403)
        elif path != "/login" and not public and not mcp_access and not access.authenticated(request):
            response = JSONResponse({"detail": {"reason": "请先登录研究工作台"}}, status_code=401)
        elif _local_route(path, request.method) and not activity_allowed and not mcp_access:
            response = JSONResponse({"detail": {"reason": "云端不提供本机目录、安装或桌面操作，请使用浏览器上传"}}, status_code=403)
        else:
            override = request.headers.get("x-windcatcher-method")
            if override and not (request.method == "POST" and override == "DELETE"
                                 and _supported_delete_override(path)):
                response = JSONResponse({"detail": {"reason": "不支持的云端请求方法"}}, status_code=400)
            else:
                # ModelScope's public gateway rejects DELETE. Restore only this
                # authenticated case deletion so the original locks still run.
                if override:
                    request.scope["method"] = "DELETE"
                case_path = re.fullmatch(r"/api/cases/(case-\d{8}-\d{6}-[0-9a-f]{4})(?:/.*)?", path)
                delete_path = case_path and path == f"/api/cases/{case_path.group(1)}"
                if (case_path and not (delete_path and request.scope["method"] == "DELETE")
                        and cases.deletion_pending(case_path.group(1))):
                    response = denied(404, "病例正在删除，仅可重试删除")
                else:
                    response = await call_next(request)
        # no-referrer makes native form POSTs send Origin:null, breaking CSRF
        # validation. same-origin preserves it without disclosing external paths.
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer" if path.startswith("/share/") else "same-origin"})
        if path.startswith("/share/") or path == '/references' or path.startswith('/api/references'):
            response.headers.update({
                "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
                "X-Frame-Options": "DENY",
            })
        return response

    # Personal mode must bind browser activity to the authenticated account in
    # both deployment variants. Otherwise the mounted local route would accept
    # a browser-supplied session_id in cloud mode.
    if access.deployment == 'public' or access.multiuser:
        @app.post('/api/monitor/events')
        async def operator_activity_ingest(request: Request):
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 65536:
                    raise HTTPException(413, detail={'reason': '操作记录请求过大'})
            try:
                batch = server.ActivityBatchBody.model_validate_json(bytes(body))
                events = activity_monitor.validate_browser_events(batch.events)
            except ValueError as exc:
                raise HTTPException(422, detail={'reason': '操作事件格式无效'}) from exc
            if access.multiuser:
                actor_id = request.state.principal['id']
                if any(event.get('case_id') is not None and
                       (not isinstance(event['case_id'], str) or not CASE_ID.fullmatch(event['case_id']))
                       for event in events):
                    raise HTTPException(422, detail={'reason': '操作事件病例编号无效'})
                case_ids = [event['case_id'] for event in events if event.get('case_id')]
                if len(access.identity.allowed_case_ids(actor_id, case_ids)) != len(case_ids):
                    raise HTTPException(404, detail={'reason': '病例不存在或无权访问'})
                events = [{**event, 'session_id': actor_id} for event in events]
            try:
                ids = await run_in_threadpool(activity_monitor.record_events, events)
            except Exception as exc:
                server._MONITOR_FAILURE = type(exc).__name__
                server._MONITOR_LOGGER.error('Activity monitor write failed: %s', type(exc).__name__)
                raise HTTPException(503, detail={'reason': '操作记录暂时无法写入，请稍后重试'}) from exc
            server._MONITOR_FAILURE = None
            return {'accepted': len(ids), 'last_event_id': ids[-1]}

        @app.get('/api/monitor/status')
        def operator_activity_status():
            try:
                result = activity_monitor.get_status()
            except Exception as exc:
                server._MONITOR_LOGGER.error('Activity monitor query failed: %s', type(exc).__name__)
                raise HTTPException(503, detail={'reason': '操作记录暂时不可读取，请稍后重试'}) from exc
            if server._MONITOR_FAILURE:
                result.update(status='unavailable', error_type=server._MONITOR_FAILURE)
            return result

        @app.get('/api/monitor/events')
        def operator_activity_events(
            after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500),
            case_id: str | None = None, kind: str | None = None, session_id: str | None = None,
            errors_only: bool = False, latest: bool = False,
        ):
            try:
                return activity_monitor.query_events(after, limit, case_id, kind, session_id, errors_only, latest)
            except ValueError as exc:
                raise HTTPException(422, detail={'reason': '操作记录查询条件无效'}) from exc
            except Exception as exc:
                server._MONITOR_LOGGER.error('Activity monitor query failed: %s', type(exc).__name__)
                raise HTTPException(503, detail={'reason': '操作记录暂时不可读取，请稍后重试'}) from exc

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    def landing(error: str | None = None, status_code: int = 200):
        html = (ROOT / "deploy" / "login.html").read_text(encoding="utf-8")
        error_text = {"invalid": "账号或密码错误，请重试。", "limited": "登录尝试过多，请在 15 分钟后重试。"}.get(error, "")
        error_html = f'<p class="error" role="alert">{error_text}</p>' if error_text else ""
        deployment_note = (
            "本机公网研究空间：仅上传已授权、已去标识的影像；上传后由本机服务处理。"
            if access.deployment == "public" else
            "魔搭研究空间：仅上传已授权、已去标识的影像；处理与存储依赖创空间部署配置。"
        )
        html = html.replace("<!--LOGIN_ERROR-->", error_html).replace("<!--DEPLOYMENT_NOTE-->", deployment_note)
        return HTMLResponse(html, status_code=status_code,
                            headers={"Retry-After": "900"} if error == "limited" else None)

    @app.get("/login")
    async def login_page(request: Request):
        return landing("invalid" if request.query_params.get("error") == "1" else None)

    @app.post("/login")
    async def login(request: Request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                return JSONResponse({"detail": {"reason": "登录请求过大"}}, status_code=413)
        from urllib.parse import parse_qs
        try:
            fields = parse_qs(bytes(body).decode("utf-8", errors="replace"), max_num_fields=4)
        except ValueError:
            return JSONResponse({"detail": {"reason": "登录字段格式无效"}}, status_code=400)
        result = access.login(request.client.host if request.client else "unknown", fields.get("username", [""])[0], fields.get("password", [""])[0])
        if result == "limited":
            return landing("limited", status_code=429)
        response = RedirectResponse("/" if result == "ok" else "/login?error=1", status_code=303)
        if result == "ok":
            if access.multiuser:
                principal = access.identity.authenticate(fields.get("username", [""])[0], fields.get("password", [""])[0])
                if principal is None:
                    return RedirectResponse("/login?error=1", status_code=303)
                token = access.identity.issue_session(principal["id"])
            else:
                token = access.session()
            response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, secure=True, httponly=True, samesite="lax", path="/")
        return response

    @app.post("/logout")
    async def logout(request: Request):
        if access.multiuser:
            access.identity.revoke_session(request.cookies.get(COOKIE))
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="lax", path="/")
        return response

    @app.get("/")
    async def index(request: Request):
        if not access.authenticated(request):
            return landing()
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        if access.multiuser:
            user = access.principal(request)
            import html as html_module
            banner_text = "公网研究空间 · 当前账号：" + html_module.escape(user["username"]) + "。仅处理已授权、已去标识数据。"
        else:
            banner_text = "公网研究空间 · 登录者共用病例。仅处理已授权、已去标识数据；定期备份研究产物。"
        banner = '<div class="notice-banner" style="display:flex;gap:16px;align-items:center;justify-content:space-between"><span>' + banner_text + '</span><form method="post" action="/logout"><button class="button button-quiet" type="submit">退出登录</button></form></div>'
        mode_flag = "window.WindcatcherMultiuser = true;" if access.multiuser else ""
        if access.multiuser and harness_advice.enabled():
            mode_flag += "window.WindcatcherHarnessEnabled = true;"
        deployment_flag = 'window.WindcatcherDeployment = "' + access.deployment + '";'
        html = html.replace("<body>", '<body><script>window.WindcatcherCloudAccess = true;' + deployment_flag + mode_flag + '</script>' + banner)
        if access.deployment == 'public':
            html = html.replace('<body>', '<body><script>window.WindcatcherActivityAccess = true;</script>', 1)
        html = html.replace("仅本机 · 127.0.0.1", "公网研究空间")
        html = html.replace('src="/reference-result"', 'src="about:blank"')
        return HTMLResponse(html)

    @app.get("/api/capabilities")
    async def cloud_capabilities(request: Request):
        result = await run_in_threadpool(server.capabilities.probe_capabilities)
        result["compute"] = server.detect_compute_capabilities()
        result["ai_interpretation"] = server.ai_report.get_config()
        for group in result["groups"]:
            for item in group["items"]:
                item.pop("install", None)
        return result

    @app.get("/reference-result")
    async def no_reference_case():
        return JSONResponse({"detail": {"reason": "云端未发布原始参考病例"}}, status_code=404)

    @app.get("/share-admin")
    async def share_admin():
        return RedirectResponse("/#share", status_code=303)

    if access.multiuser:
        @app.post("/api/cases/{case_id}/harness-advice")
        async def case_harness_advice(request: Request, case_id: str):
            actor_id = request.state.principal["id"]
            if not harness_advice.enabled():
                raise HTTPException(503, detail={"reason": "DeepSeek Harness 只读试点尚未启用"})
            if (not access.identity.authorize_case(actor_id, case_id, "view")
                    or cases.deletion_pending(case_id)):
                raise HTTPException(404, detail={"reason": "病例不存在或无权访问"})
            try:
                before = await run_in_threadpool(harness_projection.project_case_state, case_id)
            except harness_projection.ProjectionError as exc:
                raise HTTPException(409, detail={"reason": "病例状态摘要不可用，请检查病例"}) from exc
            try:
                advice = await run_in_threadpool(harness_advice.request_advice, before, actor_id)
            except harness_advice.AdviceUnavailable as exc:
                raise HTTPException(exc.status, detail={"reason": exc.reason}) from exc
            if (not access.identity.authorize_case(actor_id, case_id, "view")
                    or cases.deletion_pending(case_id)):
                raise HTTPException(404, detail={"reason": "病例不存在或无权访问"})
            try:
                after = await run_in_threadpool(harness_projection.project_case_state, case_id)
            except harness_projection.ProjectionError as exc:
                raise HTTPException(409, detail={"reason": "病例状态已变化，请重新获取建议"}) from exc
            if after["state_sha256"] != before["state_sha256"]:
                raise HTTPException(409, detail={"reason": "病例状态已变化，请重新获取建议"})
            return advice

        @app.get("/api/operator/preferences")
        async def get_operator_preferences(request: Request):
            return await run_in_threadpool(app.state.operator_preferences.get, request.state.principal["id"])

        @app.put("/api/operator/preferences")
        async def put_operator_preferences(request: Request, body: OperatorPreferencesBody):
            try:
                return await run_in_threadpool(app.state.operator_preferences.replace,
                                               request.state.principal["id"], body.expected_version,
                                               body.preferences)
            except PreferenceConflict as exc:
                raise HTTPException(409, detail={"reason": "个人偏好已在其他设备更新，请重新加载",
                                                 "current_version": exc.current_version}) from exc
            except PreferenceError as exc:
                raise HTTPException(422, detail={"reason": "个人偏好格式无效"}) from exc

        @app.get("/api/operator/recipe-candidates")
        async def list_operator_recipe_candidates(request: Request):
            actor_id = request.state.principal["id"]
            try:
                signals = await run_in_threadpool(activity_monitor.query_recipe_signals, actor_id)
                case_ids = sorted({row["case_id"] for row in signals})
                allowed = await run_in_threadpool(access.identity.allowed_case_ids,
                                                  actor_id, case_ids, "view")
                allowed = {case_id for case_id in allowed if not cases.deletion_pending(case_id)}
                suggestions = operator_recipe_candidates.suggest_candidates(
                    signals, actor_id, allowed)
            except Exception as exc:
                server._MONITOR_LOGGER.error("Recipe candidate read failed: %s", type(exc).__name__)
                raise HTTPException(503, detail={"reason": "暂时无法生成操作建议，请稍后重试"}) from exc
            return {"candidates": suggestions}

        @app.get("/api/operator/recipes")
        async def list_operator_recipes(request: Request):
            rows = await run_in_threadpool(app.state.operator_recipes.list, request.state.principal["id"])
            return {"recipes": rows}

        @app.post("/api/operator/recipes", status_code=201)
        async def create_operator_recipe(request: Request, body: OperatorRecipeCreateBody):
            try:
                return await run_in_threadpool(app.state.operator_recipes.create,
                                               request.state.principal["id"], body.spec)
            except RecipeError as exc:
                raise HTTPException(422, detail={"reason": "操作配方草案格式无效"}) from exc

        @app.get("/api/operator/recipes/{recipe_id}")
        async def get_operator_recipe(request: Request, recipe_id: str):
            try:
                row = await run_in_threadpool(app.state.operator_recipes.get,
                                              request.state.principal["id"], recipe_id)
            except RecipeError as exc:
                raise HTTPException(404, detail={"reason": "配方草案不存在"}) from exc
            if row is None:
                raise HTTPException(404, detail={"reason": "配方草案不存在"})
            return row

        async def change_operator_recipe(request: Request, recipe_id: str,
                                         expected_version: int, spec: dict | None):
            try:
                method = app.state.operator_recipes.revoke if spec is None else app.state.operator_recipes.replace
                args = (request.state.principal["id"], recipe_id, expected_version)
                if spec is not None:
                    args += (spec,)
                return await run_in_threadpool(method, *args)
            except RecipeNotFound as exc:
                raise HTTPException(404, detail={"reason": "配方草案不存在"}) from exc
            except RecipeConflict as exc:
                raise HTTPException(409, detail={"reason": "草案已在其他设备更新，请重新加载",
                                                 "current_version": exc.current_version}) from exc
            except RecipeRevoked as exc:
                raise HTTPException(409, detail={"reason": "配方草案已撤销"}) from exc
            except RecipeError as exc:
                raise HTTPException(422, detail={"reason": "操作配方草案格式无效"}) from exc

        @app.put("/api/operator/recipes/{recipe_id}")
        async def put_operator_recipe(request: Request, recipe_id: str, body: OperatorRecipeUpdateBody):
            return await change_operator_recipe(request, recipe_id, body.expected_version, body.spec)

        @app.post("/api/operator/recipes/{recipe_id}/revoke")
        async def revoke_operator_recipe(request: Request, recipe_id: str, body: OperatorRecipeRevokeBody):
            return await change_operator_recipe(request, recipe_id, body.expected_version, None)

        @app.get("/api/admin/users")
        async def users(request: Request):
            return access.identity.list_users(request.state.principal["id"])

        @app.post("/api/admin/users", status_code=201)
        async def create_user(request: Request, body: CreateUserBody):
            try:
                return access.identity.create_user(request.state.principal["id"], body.username,
                                                   body.password, body.global_role)
            except IdentityError as exc:
                raise HTTPException(400, detail={"reason": str(exc)}) from exc

        @app.post("/api/admin/users/{user_id}/disable")
        async def disable_user(request: Request, user_id: str):
            try:
                access.identity.disable_user(request.state.principal["id"], user_id)
            except IdentityError as exc:
                raise HTTPException(400, detail={"reason": str(exc)}) from exc
            ai_report.forget_user_key(user_id)
            return {"status": "disabled"}

        @app.get("/api/cases/{case_id}/grants")
        async def case_grants(request: Request, case_id: str):
            return access.identity.list_case_grants(request.state.principal["id"], case_id)

        @app.put("/api/cases/{case_id}/grants/{user_id}")
        async def grant_case(request: Request, case_id: str, user_id: str, body: GrantBody):
            try:
                access.identity.grant_case(request.state.principal["id"], case_id, user_id, body.role)
            except IdentityError as exc:
                raise HTTPException(400, detail={"reason": str(exc)}) from exc
            return {"status": "granted", "role": body.role}

        @app.delete("/api/cases/{case_id}/grants/{user_id}")
        async def revoke_grant(request: Request, case_id: str, user_id: str):
            try:
                access.identity.revoke_case_grant(request.state.principal["id"], case_id, user_id)
            except IdentityError as exc:
                raise HTTPException(400, detail={"reason": str(exc)}) from exc
            return {"status": "revoked"}

        @app.get("/api/cases")
        async def visible_cases(request: Request):
            rows = await run_in_threadpool(list_cases)
            permitted = set(access.identity.allowed_case_ids(request.state.principal["id"],
                                                             [row["case_id"] for row in rows]))
            return [row for row in rows if row["case_id"] in permitted]

        @app.delete("/api/cases/{case_id}")
        async def delete_owned_case(request: Request, case_id: str):
            from contextlib import ExitStack
            actor_id = request.state.principal["id"]
            with ExitStack() as reservations:
                queue = getattr(server.app.state, "batch_queue", None)
                if queue:
                    reservations.enter_context(queue.mutation(case_id))
                server.cfd_postprocess_jobs.reserve_mutation(case_id)
                reservations.callback(server.cfd_postprocess_jobs.release_mutation, case_id)
                server.scenario_jobs.reserve_mutation(case_id)
                reservations.callback(server.scenario_jobs.release_mutation, case_id)
                await run_in_threadpool(cases.begin_case_deletion, case_id, actor_id)
                try:
                    access.identity.revoke_case_collaborators(actor_id, case_id)
                    await run_in_threadpool(patient_shares.revoke_case, case_id)
                    await run_in_threadpool(reference_cases.revoke_case, case_id)
                except Exception as exc:
                    server._MONITOR_LOGGER.error("Case deletion revocation failed: %s", type(exc).__name__)
                    raise HTTPException(503, detail={"reason": "访问撤回未完成，请再次点击删除"}) from exc
                await run_in_threadpool(lambda: cases.delete_case(case_id, keep_marker=True, owner_id=actor_id))
                try:
                    access.identity.retire_case(actor_id, case_id)
                except Exception as exc:
                    server._MONITOR_LOGGER.error("Case ACL retirement failed after file deletion: %s", type(exc).__name__)
                    raise HTTPException(503, detail={"reason": "病例文件已清理，权限收尾失败，请再次点击删除"}) from exc
                try:
                    await run_in_threadpool(cases.finish_case_deletion, case_id)
                except OSError:
                    server._MONITOR_LOGGER.exception("Case deletion marker cleanup failed")
            return {"case_id": case_id, "status": "deleted", "message": "病例及其本地产物已删除"}

        @app.post("/api/cases/upload")
        async def upload_case(request: Request, background_tasks: BackgroundTasks):
            actor_id = request.state.principal["id"]
            binder_token = server.CURRENT_CASE_OWNER_BINDER.set(
                lambda case_id: access.identity.bind_case_owner(case_id, actor_id))
            try:
                return await server.api_upload_case(request, background_tasks)
            finally:
                server.CURRENT_CASE_OWNER_BINDER.reset(binder_token)

        @app.post("/api/uploads")
        async def start_upload(request: Request, body: dict):
            from pydantic import ValidationError
            try:
                validated = server.UploadStartBody.model_validate(body)
            except ValidationError as exc:
                raise HTTPException(422, detail={"reason": "上传清单无效"}) from exc
            result = await server.api_start_upload(validated)
            try:
                access.identity.bind_upload_owner(result["upload_id"], request.state.principal["id"])
            except Exception as exc:
                try:
                    await server.api_cancel_upload(result["upload_id"])
                    access.identity.forget_unfinished_upload(result["upload_id"], request.state.principal["id"])
                except Exception:
                    server._MONITOR_LOGGER.exception("Failed to remove unowned upload staging")
                raise HTTPException(503, detail={"reason": "上传任务登记失败，请稍后重试"}) from exc
            try:
                access.identity.prune_missing_unfinished_uploads(server.CASES_DIR)
            except Exception as exc:
                server._MONITOR_LOGGER.error("Expired upload ownership cleanup failed: %s", type(exc).__name__)
            return result

        @app.post("/api/uploads/{upload_id}/complete")
        async def complete_upload(request: Request, upload_id: str, background_tasks: BackgroundTasks):
            actor_id = request.state.principal["id"]
            pending_case_id = access.identity.upload_case_id(actor_id, upload_id)
            def recover():
                case_id = access.identity.upload_case_id(actor_id, upload_id)
                if case_id and (server.case_dir(case_id) / "import" / "dicom").is_dir():
                    return server._uploaded_case_result(case_id)
                return None
            binder_token = server.CURRENT_CASE_OWNER_BINDER.set(
                lambda case_id: access.identity.finish_upload(upload_id, case_id, actor_id))
            recover_token = server.CURRENT_UPLOAD_RECOVERER.set(recover)
            pending_token = server.CURRENT_UPLOAD_PENDING_CASE_ID.set(pending_case_id)
            try:
                return await server.api_complete_upload(upload_id, background_tasks)
            finally:
                server.CURRENT_UPLOAD_PENDING_CASE_ID.reset(pending_token)
                server.CURRENT_UPLOAD_RECOVERER.reset(recover_token)
                server.CURRENT_CASE_OWNER_BINDER.reset(binder_token)

        @app.get("/api/automation/cases")
        async def allowed_automation_cases(request: Request, offset: int = Query(0, ge=0),
                                           limit: int = Query(20, ge=1, le=50)):
            server._automation_queue(request)
            rows = list_cases()
            permitted = set(access.identity.allowed_case_ids(request.state.principal["id"],
                                                             [row["case_id"] for row in rows]))
            rows = [row for row in rows if row["case_id"] in permitted]
            return {"cases": [{k: row[k] for k in ("case_id", "synthetic", "stages")}
                              for row in rows[offset:offset + limit]], "total": len(rows),
                    "next_offset": offset + limit if offset + limit < len(rows) else None}

        @app.post("/api/automation/batches", status_code=202)
        async def submit_automation_batch(request: Request, body: dict):
            from pydantic import ValidationError
            try:
                validated = server.automation.BatchBody.model_validate(body)
            except ValidationError as exc:
                raise HTTPException(422, detail={"reason": "批量请求格式无效"}) from exc
            case_ids = [item.case_id for item in validated.items]
            if len(access.identity.allowed_case_ids(request.state.principal["id"], case_ids, "edit")) != len(case_ids):
                raise HTTPException(404, detail={"reason": "病例不存在或无权访问"})
            return server.api_automation_submit(validated, request)

    @app.get("/api/cases/{case_id}/review-events")
    async def reviews(case_id: str):
        return await run_in_threadpool(patient_shares.list_reviews, case_id)

    @app.post("/api/cases/{case_id}/review-events")
    async def add_review(case_id: str, body: ReviewBody, request: Request):
        try:
            actor = (access.actor_name(request) if access.multiuser else
                     access.username + "/" + body.operator.strip())
            return await run_in_threadpool(patient_shares.record_review, case_id,
                                           actor,
                                           body.stage, body.action, body.reason)
        except patient_shares.ShareError as exc:
            share_error(exc)

    @app.get("/api/cases/{case_id}/cfd-reviews")
    async def cfd_review_records(case_id: str):
        try:
            return await run_in_threadpool(cfd_reviews.list_reviews, case_id)
        except cfd_reviews.CfdReviewError as exc:
            cfd_review_error(exc)

    @app.post("/api/cases/{case_id}/cfd-reviews")
    async def add_cfd_review(case_id: str, body: CfdReviewBody, request: Request):
        payload = {
            "expected_signature": body.expected_signature, "kind": body.kind,
            "reviewer": (access.actor_name(request) if access.multiuser else
                         access.username + "/" + body.reviewer.strip()),
            "note": body.note, "evidence_ref": body.evidence_ref,
            "decision": body.decision, "grids": body.grids,
        }
        try:
            return await run_in_threadpool(cfd_reviews.record_review, case_id, payload)
        except cfd_reviews.CfdReviewError as exc:
            cfd_review_error(exc)

    @app.get("/api/cases/{case_id}/shares")
    async def shares(case_id: str):
        return await run_in_threadpool(patient_shares.list_shares, case_id)

    @app.post("/api/cases/{case_id}/shares")
    async def publish_share(case_id: str, body: PublishBody, request: Request):
        from contextlib import ExitStack
        try:
            with ExitStack() as reservations:
                queue = getattr(server.app.state, "batch_queue", None)
                if queue:
                    reservations.enter_context(queue.mutation(case_id))
                server.cfd_postprocess_jobs.reserve_mutation(case_id)
                reservations.callback(server.cfd_postprocess_jobs.release_mutation, case_id)
                server.scenario_jobs.reserve_mutation(case_id)
                reservations.callback(server.scenario_jobs.release_mutation, case_id)
                actor = (access.actor_name(request) if access.multiuser else
                         access.username + "/" + body.reviewer.strip())
                return await run_in_threadpool(patient_shares.publish, case_id,
                                               actor,
                                               body.review_note, body.days)
        except patient_shares.ShareError as exc:
            share_error(exc)
        except PipelineError as exc:
            raise HTTPException(409, detail={"reason": exc.reason}) from exc

    @app.post("/api/cases/{case_id}/shares/test")
    async def publish_test_share(case_id: str, body: TestPublishBody, request: Request):
        if access.deployment != "public":
            raise HTTPException(404, detail={"reason": "此入口仅用于本机公网测试分享"})
        from contextlib import ExitStack
        try:
            with ExitStack() as reservations:
                queue = getattr(server.app.state, "batch_queue", None)
                if queue:
                    reservations.enter_context(queue.mutation(case_id))
                server.cfd_postprocess_jobs.reserve_mutation(case_id)
                reservations.callback(server.cfd_postprocess_jobs.release_mutation, case_id)
                server.scenario_jobs.reserve_mutation(case_id)
                reservations.callback(server.scenario_jobs.release_mutation, case_id)
                actor = (access.actor_name(request) if access.multiuser else
                         access.username + "/" + body.operator.strip())
                return await run_in_threadpool(patient_shares.publish_test, case_id,
                                               actor,
                                               body.reason, body.recipient_confirmed)
        except patient_shares.ShareError as exc:
            share_error(exc)
        except PipelineError as exc:
            raise HTTPException(409, detail={"reason": exc.reason}) from exc

    @app.post("/api/cases/{case_id}/shares/{share_id}/revoke")
    async def revoke_share(case_id: str, share_id: str):
        try:
            await run_in_threadpool(patient_shares.revoke, case_id, share_id)
        except patient_shares.ShareError as exc:
            share_error(exc)
        return {"status": "revoked"}

    @app.get("/share/{token}")
    async def patient_page(token: str):
        try:
            row = await run_in_threadpool(patient_shares.lookup, token)
        except patient_shares.ShareError as exc:
            share_error(exc)
        html = (ROOT / "deploy" / "patient-view.html").read_text(encoding="utf-8")
        if patient_shares.share_mode(row) == "test":
            title = "未验证测试可视化"
            intro = "此模型尚未通过 CFD 数值、网格敏感性及工程复核；只用于测试链接、访问码与页面显示，不作为患者结果或诊疗依据。"
            warning = '<p class="test-warning" role="alert">测试分享 · 未验证结果 · 24 小时后失效。请勿据此判断病情或治疗方案。</p>'
            model_warning = '<p class="test-warning">未验证的测试模型，仅用于核对页面显示，不能作为诊疗依据。</p>'
        else:
            title = "你的鼻腔气流模型"
            intro = "这是一份经过团队复核的固定版本气流可视化。颜色表示模拟速度或压力，不能据此判断病因、病情或治疗方案。"
            warning = ""
            model_warning = ""
        html = html.replace("<!--SHARE_TITLE-->", title).replace("<!--SHARE_INTRO-->", intro)
        html = html.replace("<!--SHARE_WARNING-->", warning).replace("<!--SHARE_MODEL_WARNING-->", model_warning)
        return HTMLResponse(html)

    @app.post("/share/{token}/unlock")
    async def patient_unlock(token: str, body: UnlockBody, request: Request):
        try:
            row = await run_in_threadpool(patient_shares.lookup, token)
        except patient_shares.ShareError as exc:
            share_error(exc)
        address = request.client.host if request.client else "unknown"
        limit_key = row["id"] + ":" + address
        if not access.check_share_limit(limit_key):
            await run_in_threadpool(patient_shares.log_access, row["id"], "unlock", "limited", address, access.key)
            raise HTTPException(429, detail={"reason": "尝试过多，请 15 分钟后重试"})
        if not await run_in_threadpool(patient_shares.verify_code, row, body.access_code):
            access.mark_share_failure(limit_key)
            await run_in_threadpool(patient_shares.log_access, row["id"], "unlock", "denied", address, access.key)
            raise HTTPException(403, detail={"reason": "访问码不正确"})
        access.clear_share_failure(limit_key)
        await run_in_threadpool(patient_shares.log_access, row["id"], "unlock", "ok", address, access.key)
        response = JSONResponse({"status": "unlocked"})
        response.set_cookie(PATIENT_COOKIE, access.patient_session(row["id"], row["expires_at"]),
                            max_age=min(SESSION_SECONDS, row["expires_at"] - int(time.time())),
                            secure=True, httponly=True, samesite="strict", path="/share/" + token)
        return response

    @app.get("/share/{token}/model")
    async def patient_model(token: str, request: Request):
        try:
            row = await run_in_threadpool(patient_shares.lookup, token)
        except patient_shares.ShareError as exc:
            share_error(exc)
        address = request.client.host if request.client else "unknown"
        if not access.patient_authenticated(request, row["id"], row["expires_at"]):
            await run_in_threadpool(patient_shares.log_access, row["id"], "model", "denied", address, access.key)
            raise HTTPException(401, detail={"reason": "请先输入访问码"})
        try:
            data = await run_in_threadpool(patient_shares.read_patient_model, row)
        except patient_shares.ShareError as exc:
            share_error(exc)
        await run_in_threadpool(patient_shares.log_access, row["id"], "model", "ok", address, access.key)
        return Response(data, media_type="application/json")

    @app.get('/share/{token}/metadata')
    async def patient_metadata(token: str, request: Request):
        import json
        row = await run_in_threadpool(patient_shares.lookup, token)
        if not access.patient_authenticated(request, row['id'], row['expires_at']):
            raise HTTPException(401, detail={'reason': '请先输入访问码'})
        testing = patient_shares.share_mode(row) == 'test'
        return {'kind': 'flow_test' if testing else 'flow', 'flow_verified': not testing,
                'warning': reference_cases.FLOW_TEST_WARNING if testing else '',
                'conditions': reference_cases.public_conditions(json.loads(row['parameters']))}

    if not access.multiuser:
        @app.delete("/api/cases/{case_id}")
        async def delete_shared_case(case_id: str):
            from contextlib import ExitStack
            if not CASE_ID.fullmatch(case_id):
                raise HTTPException(404, detail={"reason": "病例不存在"})
            with ExitStack() as reservations:
                queue = getattr(server.app.state, "batch_queue", None)
                if queue:
                    reservations.enter_context(queue.mutation(case_id))
                server.cfd_postprocess_jobs.reserve_mutation(case_id)
                reservations.callback(server.cfd_postprocess_jobs.release_mutation, case_id)
                server.scenario_jobs.reserve_mutation(case_id)
                reservations.callback(server.scenario_jobs.release_mutation, case_id)
                await run_in_threadpool(cases.begin_case_deletion, case_id)
                try:
                    await run_in_threadpool(patient_shares.revoke_case, case_id)
                    await run_in_threadpool(reference_cases.revoke_case, case_id)
                except Exception as exc:
                    server._MONITOR_LOGGER.error("Shared case deletion revocation failed: %s", type(exc).__name__)
                    raise HTTPException(503, detail={"reason": "访问撤回未完成，请再次点击删除"}) from exc
                await run_in_threadpool(cases.delete_case, case_id)
            return {"case_id": case_id, "status": "deleted", "message": "病例及其本地产物已删除"}

    app.mount("/", server.app)
    return app
