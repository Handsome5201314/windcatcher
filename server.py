"""风见本机服务：只绑定 127.0.0.1，页面与真实管线 API 一一对应。

铁律：接口返回的每个数值都来自 cases/<id>/ 下的真实产物；
缺失工具如实报缺失；没有求解就没有结果。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import ExitStack, asynccontextmanager
from io import BytesIO
from pathlib import Path, PurePosixPath

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from pipeline import activity_monitor, ai_report, automation, batch_jobs, capabilities, cfd_postprocess_jobs, cfd_prep, dicom_qc, endplanes, geom_qc, installers, mask_review, openfoam_runner, plug_models, preview, report, scenario_cfd, scenario_jobs, scenarios, segmentation, selection_preview, slicer_review, surface
from pipeline.anonymize import anonymize_series
from pipeline.cases import artifact_path, case_dir, create_case, delete_case, get_case, list_cases, recover_interrupted_cases, update_job, update_stage
from pipeline.config import APP_DIR, CASES_DIR, PROJECT_ROOT, detect_compute_capabilities
from pipeline.errors import PipelineError
from pipeline.fetch_direct import fetch_direct
from pipeline.volume import load_series, save_volume

REFERENCE_HTML = PROJECT_ROOT / "原始资料" / "原始鼻腔CFD_交互查看_v3.html"
_DIRECTORY_PICKER_LOCK = threading.Lock()
_DIRECTORY_PICKER_TIMEOUT_SECONDS = 60
_UPLOAD_MAX_FILES = 5000
_UPLOAD_MAX_FILE_BYTES = 128 * 1024 * 1024
_UPLOAD_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
_UPLOAD_MAX_PATH_CHARS = 240
_CFD_VALIDATION_CACHE: dict[str, tuple[tuple, dict]] = {}
_CFD_SUMMARY_CACHE: dict[str, tuple[tuple, dict]] = {}
_CFD_VALIDATION_LOCK = threading.Lock()
_MONITOR_LOGGER = logging.getLogger("windcatcher.monitor")
_MONITOR_FAILURE: str | None = None


def _cleanup_upload_staging() -> None:
    """Remove incomplete browser uploads left by a stopped server."""
    try:
        CASES_DIR.mkdir(parents=True, exist_ok=True)
        for staging in CASES_DIR.glob(".windcatcher-upload-*"):
            if staging.is_dir() and not staging.is_symlink():
                shutil.rmtree(staging, ignore_errors=True)
    except OSError as exc:
        _MONITOR_LOGGER.warning("无法清理中断的 DICOM 上传暂存：%s", type(exc).__name__)


def _record_activity(event: dict) -> int | None:
    global _MONITOR_FAILURE
    try:
        event_id = activity_monitor.record_event(event)
    except Exception as exc:
        _MONITOR_FAILURE = type(exc).__name__
        _MONITOR_LOGGER.error("Activity monitor write failed: %s", _MONITOR_FAILURE)
        return None
    _MONITOR_FAILURE = None
    return event_id

@asynccontextmanager
async def app_lifespan(application: FastAPI):
    # 只在本进程首次启动、开始接收请求前恢复。重复进入 lifespan 时不能改写本进程正在执行的任务。
    if not getattr(application.state, "interrupted_cases_recovered", False):
        _cleanup_upload_staging()
        recover_interrupted_cases()
        cfd_postprocess_jobs.recover_interrupted()
        scenario_jobs.recover_interrupted()
        application.state.interrupted_cases_recovered = True
    await run_in_threadpool(_record_activity, {"kind": "lifecycle", "action": "server_started"})
    queue = batch_jobs.BatchQueue(Path(os.environ.get("WINDCATCHER_AUTOMATION_DIR", str(APP_DIR / ".local-automation"))) / "jobs.sqlite3", automation.preflight, automation.execute)
    application.state.batch_queue = queue
    queue.start()
    try:
        yield
    finally:
        queue.stop()
        application.state.batch_queue = None
        await run_in_threadpool(_record_activity, {"kind": "lifecycle", "action": "server_stopped"})


app = FastAPI(title="风见 · 鼻腔气流工作台", docs_url=None, redoc_url=None, lifespan=app_lifespan)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")


@app.middleware("http")
async def local_only_headers(request, call_next):
    mutation_case = None
    scenario_mutation_case = None
    batch_reservation = ExitStack()
    # Only a real case id participates in the per-case mutation lock;
    # collection routes such as /api/cases/upload must not share a fake id.
    match = re.fullmatch(r"/api/cases/(case-\d{8}-\d{6}-[0-9a-f]{4})(/.*)?", request.url.path)
    suffix = match.group(2) or "" if match else ""
    if match and request.method in {"POST", "PUT", "DELETE"} and suffix not in {
        "/cfd-postprocess", "/cfd-postprocess/cancel", "/segment/endplanes/preview",
    }:
        mutation_case = match.group(1)
        try:
            queue = getattr(app.state, "batch_queue", None)
            if queue:
                batch_reservation.enter_context(queue.mutation(mutation_case))
            cfd_postprocess_jobs.reserve_mutation(mutation_case)
        except PipelineError as exc:
            batch_reservation.close()
            return JSONResponse(status_code=409, content=exc.to_dict())
        # The long-running scenario solver owns its own lock.  All other case
        # mutations must wait for it, otherwise a new placement could race the
        # version that the solver is reading.
        if not re.fullmatch(r"/scenarios/[^/]+/cfd-run", suffix):
            try:
                scenario_jobs.reserve_mutation(mutation_case)
                scenario_mutation_case = mutation_case
            except PipelineError as exc:
                cfd_postprocess_jobs.release_mutation(mutation_case)
                batch_reservation.close()
                return JSONResponse(status_code=409, content=exc.to_dict())
    try:
        response = await call_next(request)
    finally:
        if mutation_case:
            cfd_postprocess_jobs.release_mutation(mutation_case)
        if scenario_mutation_case:
            scenario_jobs.release_mutation(scenario_mutation_case)
        batch_reservation.close()
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.middleware("http")
async def activity_request_log(request: Request, call_next):
    if not request.url.path.startswith("/api/") or request.url.path.startswith("/api/monitor/"):
        return await call_next(request)
    request_id = request.headers.get("X-Windcatcher-Request-Id", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", request_id):
        request_id = secrets.token_hex(12)
    session_id = request.headers.get("X-Windcatcher-Session", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", session_id):
        session_id = None
    # Only route templates are persisted, never raw URLs, query strings or bodies.
    route = next((route for route in app.routes if hasattr(route, "path_regex") and route.path_regex.fullmatch(request.url.path)), None)
    template = route.path if route else "/api/unknown"
    case_match = re.match(r"/api/cases/(case-\d{8}-\d{6}-[0-9a-f]{4})(?:/|$)", request.url.path)
    event = {
        "kind": "request", "action": "started",
        "request_id": request_id, "session_id": session_id,
        "case_id": case_match.group(1) if case_match else None,
        "target": re.sub(r"\{[^}]+\}", "item", template.removeprefix("/api/")).replace("/", ":"),
        "details": {"method": request.method},
    }
    context = activity_monitor.CURRENT_CONTEXT.set({"request_id": request_id, "session_id": session_id})
    started = time.perf_counter()
    await run_in_threadpool(_record_activity, event)
    try:
        response = await call_next(request)
    except Exception as exc:
        await run_in_threadpool(_record_activity, {**event, "action": "failed", "details": {
            "method": request.method, "outcome": "error", "error_type": type(exc).__name__,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        }})
        raise
    else:
        await run_in_threadpool(_record_activity, {**event, "action": "finished", "details": {
            "method": request.method, "status_code": response.status_code,
            "outcome": "ok" if response.status_code < 400 else "failed",
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        }})
        response.headers["X-Windcatcher-Request-Id"] = request_id
        if _MONITOR_FAILURE:
            response.headers["X-Windcatcher-Monitor"] = "unavailable"
        return response
    finally:
        activity_monitor.CURRENT_CONTEXT.reset(context)


@app.exception_handler(PipelineError)
async def pipeline_error_handler(_request, exc: PipelineError):
    return JSONResponse(status_code=400, content=exc.to_dict())


class NewCaseBody(BaseModel):
    path: str
    synthetic: bool = False
    anonymize: bool = False


class _UploadRejected(Exception):
    """Rejected browser upload with a user-facing reason and HTTP status."""

    def __init__(self, reason: str, remediation: list[str], status_code: int = 400):
        super().__init__(reason)
        self.reason = reason
        self.remediation = remediation
        self.status_code = status_code


class ActivityBatchBody(BaseModel):
    events: list[dict] = Field(min_length=1, max_length=50)


def _monitor_local_request(request: Request) -> None:
    if request.client and request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(403, detail={"reason": "操作监控仅供本机查询"})
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(403, detail={"reason": "操作监控不接受其他站点的请求"})


@app.post("/api/monitor/events")
def api_record_activity(body: ActivityBatchBody, request: Request):
    _monitor_local_request(request)
    if any(event.get("kind") not in {"lifecycle", "ui", "navigation", "viewer", "request", "error", "stage"}
           or not isinstance(event.get("action"), str)
           or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_:.-]{0,99}", event["action"])
           for event in body.events):
        raise HTTPException(422, detail={"reason": "操作事件格式无效"})
    ids = []
    for event in body.events:
        event_id = _record_activity(event)
        if event_id is None:
            raise HTTPException(503, detail={"reason": "操作日志暂时无法写入，请检查本机磁盘后重试"})
        ids.append(event_id)
    return {"accepted": len(ids), "last_event_id": ids[-1]}


@app.get("/api/monitor/status")
def api_activity_status(request: Request):
    _monitor_local_request(request)
    try:
        status = activity_monitor.get_status()
    except Exception as exc:
        _MONITOR_LOGGER.error("Activity monitor query failed: %s", type(exc).__name__)
        raise HTTPException(503, detail={"reason": "操作日志暂时不可读取，请检查本机磁盘"}) from exc
    if _MONITOR_FAILURE:
        status.update(status="unavailable", error_type=_MONITOR_FAILURE)
    return status


@app.get("/api/monitor/events")
def api_activity_events(
    request: Request, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500),
    case_id: str | None = None, kind: str | None = None, session_id: str | None = None,
    errors_only: bool = False, latest: bool = False,
):
    _monitor_local_request(request)
    try:
        return activity_monitor.query_events(after, limit, case_id, kind, session_id, errors_only, latest)
    except ValueError as exc:
        raise HTTPException(422, detail={"reason": "操作日志查询条件无效"}) from exc
    except Exception as exc:
        _MONITOR_LOGGER.error("Activity monitor query failed: %s", type(exc).__name__)
        raise HTTPException(503, detail={"reason": "操作日志暂时不可读取，请检查本机磁盘"}) from exc


def _automation_queue(request: Request):
    _monitor_local_request(request)
    if request.url.hostname not in {"127.0.0.1", "localhost", "::1", "testserver"} or any(
        name in request.headers for name in ("x-forwarded-for", "forwarded", "cf-connecting-ip")
    ):
        raise HTTPException(403, detail={"reason": "自动化接口仅供本机 MCP 调用"})
    queue = getattr(app.state, "batch_queue", None)
    if not queue or not queue.health()["worker_alive"]:
        raise HTTPException(503, detail={"reason": "批量队列未运行，请检查服务日志并重启服务"})
    return queue


@app.get("/api/automation/status")
def api_automation_status(request: Request):
    queue = _automation_queue(request)
    return {"status": "ready", **queue.health(), "llm_tokens": None,
            "usage_source": "Codex/WorkBuddy 宿主 usage；本机 CFD 计算不调用大模型"}


@app.get("/api/automation/cases")
def api_automation_cases(request: Request, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=50)):
    _automation_queue(request)
    rows = list_cases()
    return {"cases": [{k: row[k] for k in ("case_id", "synthetic", "stages")} for row in rows[offset:offset + limit]],
            "total": len(rows), "next_offset": offset + limit if offset + limit < len(rows) else None}


@app.get("/api/automation/cases/{case_id}")
def api_automation_case(case_id: str, request: Request):
    _automation_queue(request)
    return automation.inspect_case(case_id)


@app.post("/api/automation/batches", status_code=202)
def api_automation_submit(body: automation.BatchBody, request: Request):
    queue = _automation_queue(request)
    return queue.submit(body.request_key, [item.model_dump() for item in body.items])


@app.get("/api/automation/batches/{batch_id}")
def api_automation_batch(batch_id: str, request: Request):
    return _automation_queue(request).get(batch_id)


@app.post("/api/automation/batches/{batch_id}/cancel")
def api_automation_cancel(batch_id: str, request: Request):
    return _automation_queue(request).cancel(batch_id)


class StageBody(BaseModel):
    path: str | None = None
    params: dict | None = None
    backend: str | None = None


class FetchBody(BaseModel):
    url: str
    authorized: bool = False
    allowed_domains: list[str] | None = None


class InstallBody(BaseModel):
    target: str


class MaskEditPointBody(BaseModel):
    x: int
    y: int


class MaskEditOperationBody(BaseModel):
    view: str
    index: int
    action: str
    points: list[MaskEditPointBody] = Field(default_factory=list)
    radius: int = 0


class MaskEditBody(BaseModel):
    # 单次编辑是默认契约；operations 兼容前端一次提交多笔笔划的调用方式。
    view: str | None = None
    index: int | None = None
    action: str | None = None
    points: list[MaskEditPointBody] = Field(default_factory=list)
    radius: int = 0
    operations: list[MaskEditOperationBody] | None = None
    reset: bool = False


class EndplanesBody(BaseModel):
    scope: str
    mask_version: int
    anterior_index: int | None = None
    posterior_index: int | None = None
    anterior_plane: dict | None = None
    posterior_plane: dict | None = None


class SingleEndplaneBody(BaseModel):
    scope: str
    mask_version: int
    plane: dict


class CfdSectionPreviewBody(BaseModel):
    position_fraction: float = Field(gt=0, lt=1, allow_inf_nan=False)


class CfdSectionBody(BaseModel):
    # 仅用于校验历史请求体；新流程不再接受六区人工标注写入。
    plane: dict
    regions: list[dict] = Field(min_length=1, max_length=6)
    confirmed: bool
    source_signature: str = Field(min_length=1, max_length=128)
    geometry_signature: str = Field(min_length=1, max_length=128)


class CfdSectionAnalysisBody(BaseModel):
    position_fraction: float = Field(gt=0, lt=1, allow_inf_nan=False)
    source_signature: str = Field(min_length=1, max_length=128)
    geometry_signature: str = Field(min_length=1, max_length=128)


class AiReportConfigBody(BaseModel):
    """AI 配置提交地址、模型和密钥；密钥只保存在当前服务进程内存。"""

    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None


class AiReportModelsBody(BaseModel):
    """连接测试只接受本次请求的密钥，不把密钥并入持久化配置。"""

    base_url: str
    api_key: str


class ScenarioPlacementBody(BaseModel):
    """鼻塞模型在病例坐标系中的人工摆位，长度单位固定为毫米。"""

    translation_mm: list[float] = Field(min_length=3, max_length=3)
    rotation_deg: list[float] = Field(min_length=3, max_length=3)
    scale: list[float] = Field(min_length=3, max_length=3)
    expected_version: int = Field(ge=1)


class ScenarioReviewBody(BaseModel):
    """人工复核记录；角色和备注不应包含患者直接标识符。"""

    approved: bool
    reviewer_role: str = Field(min_length=1, max_length=80)
    notes: str = Field(default="", max_length=2000)
    expected_version: int = Field(ge=1)


class ScenarioCfdBody(BaseModel):
    """独立鼻塞算例的准备/求解参数；版本默认取当前已审核版本。"""

    version: int | None = Field(default=None, ge=1)
    backend: str | None = Field(default=None, max_length=32)
    timeout_s: int = Field(default=7200, ge=60, le=86400)


def _cfd_signature(case_id: str) -> tuple:
    root = case_dir(case_id) / "cfd"
    paths = [root / relative for relative in (
        "case_meta.json", "run_status.json", "system/controlDict", "system/fvSolution",
        "logs/simpleFoam.log", "logs/checkMesh.log", "constant/polyMesh/boundary",
        "constant/polyMesh/points", "constant/polyMesh/faces", "constant/polyMesh/owner",
        "constant/polyMesh/neighbour",
    )]
    paths.extend(root.glob("[0-9]*/U"))
    paths.extend(root.glob("[0-9]*/p"))
    paths.extend(root.glob("[0-9]*/phi"))
    paths.extend(root.glob("[0-9]*/polyMesh/*"))
    paths.extend((root / "postProcessing").rglob("*.dat"))
    signature = []
    for path in paths:
        if path.is_file():
            stat = path.stat()
            signature.append((str(path.relative_to(root)), stat.st_size, stat.st_mtime_ns))
    return tuple(sorted(signature))


def _cfd_summary(case_id: str) -> dict:
    signature = _cfd_signature(case_id)
    cached = _CFD_SUMMARY_CACHE.get(case_id)
    if cached and cached[0] == signature:
        return cached[1]
    root = case_dir(case_id) / "cfd"
    log = root / "logs" / "simpleFoam.log"
    if not log.is_file():
        return {}
    solution = root / "system" / "fvSolution"
    control = root / "system" / "controlDict"
    summary = {
        "convergence": openfoam_runner.parse_residuals(
            log.read_text(encoding="utf-8"),
            solution.read_text(encoding="utf-8") if solution.is_file() else None,
            control.read_text(encoding="utf-8") if control.is_file() else None,
        ),
        "postprocessing": openfoam_runner._read_postprocessing(root),
    }
    _CFD_SUMMARY_CACHE[case_id] = (signature, summary)
    return summary


def _explain_legacy_run_status(run_status: dict, cfd_dir: Path | None = None) -> dict:
    """从退出码与真实日志解释旧失败记录，不写回病例或运行产物。"""
    if run_status.get("status") != "failed":
        return run_status
    steps = run_status.get("steps") if isinstance(run_status.get("steps"), list) else []
    failed_step = run_status.get("failed_step")
    failed = next((item for item in steps if item.get("name") == failed_step), None)
    returncode = run_status.get("returncode")
    if returncode is None and isinstance(failed, dict):
        returncode = failed.get("returncode")
    timed_out = bool(run_status.get("timed_out") or (returncode == 124))
    if not timed_out:
        # 旧版只显示退出码；有日志证据时才补充 MPI 启动原因。
        # 已有具体原因优先，不能用残留日志改写另一次失败或超时。
        generic_message = f"{failed_step} 退出码 {returncode}"
        if (cfd_dir is None or failed_step not in {"simpleFoam", "mpiPreflight"}
                or run_status.get("message") not in {None, "", generic_message}):
            return run_status
        log_path = cfd_dir / "logs" / f"{failed_step}.log"
        if not log_path.is_file():
            return run_status
        try:
            with log_path.open("rb") as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - 65536))
                output = stream.read().decode("utf-8", errors="replace")
        except OSError as exc:
            _MONITOR_LOGGER.warning("无法读取 CFD 失败诊断日志：%s", exc)
            return run_status
        if re.search(r"not enough slots", output, flags=re.I):
            timeout = run_status.get("timeout_seconds") or 7200
            message, remediation = openfoam_runner._error_diagnosis(
                failed_step, {"returncode": returncode}, output, timeout,
            )
            return {**run_status, "message": message, "remediation": remediation}
        return run_status
    timeout = run_status.get("timeout_seconds")
    limit = f"超过 {timeout} 秒" if isinstance(timeout, (int, float)) else "达到运行超时上限"
    enriched = dict(run_status)
    enriched["returncode"] = returncode
    enriched["timed_out"] = True
    enriched.setdefault(
        "message",
        f"{failed_step or 'OpenFOAM 求解器'} {limit}，旧版记录为超时；旧版未记录容器停止结果，无法据此确认后台当时是否停止",
    )
    if not run_status.get("run_id"):
        enriched["remediation"] = [
        "先查看最后一次外层迭代和残差，确认求解是否仍在推进",
        "重试将采用新版实时日志、容器停止检查和 CPU 并行配置；旧记录不能证明已收敛",
        ]
    return enriched


def _cfd_validation(case_id: str) -> dict:
    signature = _cfd_signature(case_id)
    with _CFD_VALIDATION_LOCK:
        cached = _CFD_VALIDATION_CACHE.get(case_id)
        if cached and cached[0] == signature:
            return cached[1]
        validation = openfoam_runner.check_result_gate(case_id)
        _CFD_VALIDATION_CACHE[case_id] = (signature, validation)
        return validation


def _require_case(case_id: str) -> dict:
    try:
        case = get_case(case_id)
        # 兼容早期病例：把已经存在的真实产物字段补回 API，避免前端显示 ?/—。
        surface_data = case.get("stages", {}).get("surface", {}).get("data", {})
        geom_path = case_dir(case_id) / "surface" / "geom_qc.json"
        if geom_path.exists():
            try:
                geom = json.loads(geom_path.read_text(encoding="utf-8"))
                openings = geom.get("openings_by_end", {})
                surface_data.setdefault("opening_anterior", openings.get("anterior", 0))
                surface_data.setdefault("opening_posterior", openings.get("posterior", 0))
                surface_data.setdefault("opening_middle", openings.get("middle", 0))
                surface_data.setdefault("faces", geom.get("faces"))
                surface_data.setdefault("boundary_edges", geom.get("boundary_edges"))
                surface_data.setdefault("verdict", geom.get("verdict"))
                surface_data.setdefault("reasons", geom.get("reasons", []))
                surface_data.setdefault("volume_check", geom.get("volume_check", {}))
                surface_data.setdefault(
                    "volume_deviation_percent",
                    geom.get("volume_check", {}).get("deviation_percent"),
                )
            except (OSError, json.JSONDecodeError):
                pass
        cfd_data = case.get("stages", {}).get("cfd_prep", {}).get("data", {})
        meta_path = case_dir(case_id) / "cfd" / "case_meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                cfd_data.setdefault("total_flow_lpm", meta.get("total_flow_lpm"))
            except (OSError, json.JSONDecodeError):
                pass
        cfd_run = case.get("stages", {}).get("cfd_run", {})
        cfd_run_data = cfd_run.setdefault("data", {})
        # run_status 是文件产物的只读衍生字段；case.json 中残留的旧快照
        # 不能在 pending/running 阶段继续暴露给前端。
        cfd_run_data.pop("run_status", None)
        cfd_run_data.pop("run_status_error", None)
        run_status_path = case_dir(case_id) / "cfd" / "run_status.json"
        run_status_allowed = cfd_run.get("status") in {"ok", "warn", "failed", "blocked"}
        if cfd_run.get("status") == "running" and openfoam_runner.status_exists(run_status_path):
            # 运行中记录只有在 run_id 与当前阶段一致时才暴露，避免刷新时显示上一次求解。
            try:
                    live_status = openfoam_runner.read_latest_status(run_status_path)
                    run_status_allowed = (
                    isinstance(live_status, dict)
                    and live_status.get("status") == "running"
                    and live_status.get("run_id") == cfd_run_data.get("run_id")
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                run_status_allowed = False
        if run_status_allowed:
            if openfoam_runner.status_exists(run_status_path):
                try:
                    run_status = openfoam_runner.read_latest_status(run_status_path)
                    if not isinstance(run_status, dict):
                        raise ValueError("顶层内容不是 JSON 对象")
                    cfd_run_data["run_status"] = _explain_legacy_run_status(run_status, run_status_path.parent)
                    cfd_run_data.update(_cfd_summary(case_id))
                except UnicodeDecodeError as exc:
                    cfd_run_data["run_status_error"] = f"无法读取 cfd/run_status.json：文件编码错误（{exc}）"
                except json.JSONDecodeError as exc:
                    cfd_run_data["run_status_error"] = f"无法读取 cfd/run_status.json：JSON 格式错误（{exc.msg}）"
                except (OSError, ValueError) as exc:
                    cfd_run_data["run_status_error"] = f"无法读取 cfd/run_status.json：{exc}"
            elif cfd_run.get("status") in {"ok", "warn"}:
                cfd_run_data["run_status_error"] = "cfd/run_status.json 缺失，无法确认求解结果"
        if not case.get("job"):
            case["job"] = {"status": "idle", "stage": None, "message": ""}
        return case
    except PipelineError as exc:
        raise HTTPException(status_code=404, detail=exc.to_dict()) from exc


def _mark_stage_error(case_id: str, stage: str, exc: Exception) -> None:
    """把 API 调用中的异常写回阶段，保证刷新页面后仍能看到失败原因。"""
    if isinstance(exc, PipelineError):
        status = exc.status
        reason = exc.reason
        detail = {"error": exc.to_dict()}
    else:
        status = "failed"
        reason = f"{stage} 阶段出现未处理错误：{exc}"
        detail = {"error": {"status": "failed", "reason": str(exc)}}
    try:
        update_stage(case_id, stage, status, message=reason, data=detail)
    except Exception:
        pass


def _require_confirmed_mask(case_id: str) -> None:
    """拒绝直接读取尚未人工确认的旧 STL/CFD/报告产物。"""
    path = case_dir(case_id) / "segment" / "segmentation.json"
    try:
        segmentation_data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=409, detail={
            "error": True,
            "status": "blocked",
            "reason": "当前病例还没有可确认的气道掩膜",
            "remediation": ["先完成气道分割并在 CT 复核视窗确认"],
        }) from exc
    flags = segmentation_data.get("review_flags", {})
    if segmentation_data.get("need_human_confirm", True) or flags.get("external_air_suspected"):
        raise HTTPException(status_code=409, detail={
            "error": True,
            "status": "blocked",
            "reason": "当前病例的气道掩膜尚未人工确认，旧下游产物不可用",
            "remediation": ["先确认或导入修正掩膜", "确认后重新生成 STL、CFD 和报告"],
        })
    if not mask_review.has_current_external_air_validation(segmentation_data):
        raise HTTPException(status_code=409, detail={
            "error": True,
            "status": "blocked",
            "reason": "当前掩膜缺少针对头部外缘空气的有效复核，旧下游产物不可用",
            "remediation": ["先去除体外空气并逐层复核鼻孔、鼻咽和鼻窦", "再次确认气道范围后重新生成 STL"],
        })


def _pick_directory() -> dict:
    """在 Windows 本机弹出文件夹选择器，返回用户选中的目录。"""
    if not _DIRECTORY_PICKER_LOCK.acquire(blocking=False):
        return {
            "status": "busy",
            "message": "已有一个文件夹选择器正在等待，请先关闭它再重试",
            "remediation": ["按 Alt+Tab 查找“选择包含同一套 DICOM 序列的文件夹”窗口", "也可以关闭当前选择器后使用高级路径输入"],
        }

    try:
        return _pick_directory_locked()
    finally:
        _DIRECTORY_PICKER_LOCK.release()


def _pick_directory_locked() -> dict:
    """在已持有互斥锁时打开选择器，避免重复创建后台对话框。"""
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        return {
            "status": "unavailable",
            "message": "当前环境没有可用的 Windows 文件夹选择器",
            "remediation": ["直接在高级选项中输入 DICOM 文件夹路径", "或在 Windows PowerShell 环境启动风见"],
        }

    script = r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class WindcatcherWindow {
  public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassName(IntPtr hWnd, StringBuilder className, int maxCount);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr insertAfter, int x, int y, int width, int height, uint flags);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  public static readonly IntPtr HwndTopmost = new IntPtr(-1);
  public const uint SwpNoSize = 0x0001;
  public const uint SwpNoMove = 0x0002;
  public const uint SwpShowWindow = 0x0040;
  public static IntPtr FindFolderDialog(uint wantedProcessId) {
    IntPtr found = IntPtr.Zero;
    EnumWindows((hWnd, _) => {
      if (!IsWindowVisible(hWnd)) return true;
      uint processId = 0;
      GetWindowThreadProcessId(hWnd, out processId);
      if (processId != wantedProcessId) return true;
      var className = new StringBuilder(128);
      GetClassName(hWnd, className, className.Capacity);
      if (className.ToString() == "#32770") { found = hWnd; return false; }
      return true;
    }, IntPtr.Zero);
    return found;
  }
}
"@
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = '选择包含同一套 DICOM 序列的文件夹'
$dialog.ShowNewFolderButton = $false
$pickerPid = [Diagnostics.Process]::GetCurrentProcess().Id
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 100
$timer.Add_Tick({
  $handle = [WindcatcherWindow]::FindFolderDialog([uint32]$pickerPid)
  if ($handle -ne [IntPtr]::Zero) {
    [WindcatcherWindow]::SetWindowPos(
      $handle,
      [WindcatcherWindow]::HwndTopmost,
      0, 0, 0, 0,
      [WindcatcherWindow]::SwpNoMove -bor [WindcatcherWindow]::SwpNoSize -bor [WindcatcherWindow]::SwpShowWindow
    ) | Out-Null
    [WindcatcherWindow]::SetForegroundWindow($handle) | Out-Null
  }
})
$timer.Start()
try {
  $result = $dialog.ShowDialog()
} finally {
  $timer.Stop()
  $timer.Dispose()
}
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
  [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
  Write-Output $dialog.SelectedPath
}
"""
    try:
        proc = subprocess.run(
            [powershell, "-NoProfile", "-STA", "-WindowStyle", "Hidden", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_DIRECTORY_PICKER_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "message": "文件夹选择器等待超时，请重新点击选择",
            "remediation": ["确认选择器窗口没有被其他窗口遮挡", "或直接使用高级路径输入"],
        }
    except OSError as exc:
        return {
            "status": "failed",
            "message": f"无法打开本机文件夹选择器：{exc}",
            "remediation": ["直接使用高级路径输入", "确认在 Windows 桌面会话中启动风见"],
        }

    selected = next((line.strip() for line in proc.stdout.splitlines() if line.strip()), "")
    if proc.returncode != 0:
        return {
            "status": "failed",
            "message": "本机文件夹选择器没有正常返回",
            "remediation": ["直接使用高级路径输入", "确认在 Windows 桌面会话中启动风见"],
        }
    if not selected:
        return {"status": "cancelled", "message": "已取消选择文件夹"}
    path = Path(selected)
    if not path.is_dir():
        return {
            "status": "failed",
            "message": "选择的路径不是文件夹",
            "remediation": ["请选择包含 DICOM 文件的具体序列文件夹"],
        }
    return {"status": "ok", "path": str(path)}


def _safe_upload_path(raw: str | None) -> Path:
    """Resolve a browser-supplied relative path without allowing traversal."""
    value = str(raw or "").replace("\\", "/")
    if not value or "\x00" in value or value.startswith("/") or re.match(r"^[A-Za-z]:/", value):
        raise _UploadRejected(
            "上传文件名无效，不能使用绝对路径",
            ["重新选择 DICOM 序列文件夹", "不要把本机路径粘贴到上传文件名中"],
        )
    parts = [part for part in value.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." or ":" in part for part in parts):
        raise _UploadRejected(
            "上传文件名包含非法目录跳转或盘符",
            ["重新选择 DICOM 序列文件夹"],
        )
    normalized = PurePosixPath(*parts)
    if len(normalized.as_posix()) > _UPLOAD_MAX_PATH_CHARS:
        raise _UploadRejected(
            "上传文件名过长",
            [f"请把 DICOM 序列放在较短的文件夹路径下（最多 {_UPLOAD_MAX_PATH_CHARS} 个字符）"],
        )
    # Path() is deliberately created only after the POSIX path has been checked;
    # this keeps browser paths stable on both Windows and Linux hosts.
    return Path(*normalized.parts)


def _store_uploaded_files(files: list[UploadFile], relative_paths: list[str] | None) -> Path:
    """Stream one multipart request into an isolated staging directory."""
    if not files:
        raise _UploadRejected(
            "没有收到 DICOM 文件",
            ["点击选择按钮，选择包含同一套 DICOM 序列的文件夹"],
        )
    if len(files) > _UPLOAD_MAX_FILES:
        raise _UploadRejected(
            f"一次最多上传 {_UPLOAD_MAX_FILES} 个文件",
            ["只选择一套 DICOM 序列，不要选择整个检查目录"],
            status_code=413,
        )
    if relative_paths is not None and len(relative_paths) != len(files):
        raise _UploadRejected(
            "上传文件清单与文件数量不一致",
            ["重新选择 DICOM 序列文件夹后重试"],
        )

    from pipeline.config import ensure_dirs

    ensure_dirs()
    staging = Path(tempfile.mkdtemp(prefix=".windcatcher-upload-", dir=CASES_DIR))
    seen: set[str] = set()
    total_bytes = 0
    try:
        for index, upload in enumerate(files):
            candidate = relative_paths[index] if relative_paths is not None else upload.filename
            relative = _safe_upload_path(candidate)
            key = relative.as_posix().casefold()
            if key in seen:
                raise _UploadRejected(
                    f"上传文件名重复：{relative.as_posix()}",
                    ["请只选择一套 DICOM 序列，避免同名文件重复"],
                )
            seen.add(key)
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            file_bytes = 0
            with target.open("xb") as output:
                while True:
                    chunk = upload.file.read(1024 * 1024)
                    if not chunk:
                        break
                    file_bytes += len(chunk)
                    total_bytes += len(chunk)
                    if file_bytes > _UPLOAD_MAX_FILE_BYTES:
                        raise _UploadRejected(
                            f"单个上传文件超过 {_UPLOAD_MAX_FILE_BYTES // (1024 * 1024)} MB",
                            ["确认选择的是 DICOM 切片，而不是整个压缩包或其他大文件"],
                            status_code=413,
                        )
                    if total_bytes > _UPLOAD_MAX_TOTAL_BYTES:
                        raise _UploadRejected(
                            f"本次上传超过 {_UPLOAD_MAX_TOTAL_BYTES // (1024 * 1024 * 1024)} GB",
                            ["只选择一套 DICOM 序列，或分批处理"],
                            status_code=413,
                        )
                    output.write(chunk)
            if file_bytes == 0:
                raise _UploadRejected(
                    f"上传文件为空：{relative.as_posix()}",
                    ["确认 DICOM 序列文件可正常读取后重试"],
                )
        _collapse_upload_root(staging)
        return staging
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _parse_upload_paths(raw: object, count: int) -> list[str] | None:
    """Parse the browser's relative path sidecar, keeping names out of logs."""
    if raw in (None, ""):
        return None
    try:
        paths = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _UploadRejected("上传文件清单格式无效", ["重新选择 DICOM 序列文件夹后重试"]) from exc
    if not isinstance(paths, list) or len(paths) != count or not all(isinstance(path, str) for path in paths):
        raise _UploadRejected("上传文件清单格式无效", ["重新选择 DICOM 序列文件夹后重试"])
    return paths


def _parse_upload_bool(raw: object, name: str) -> bool:
    if raw in (None, ""):
        return False
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise _UploadRejected(f"上传选项 {name} 无效", ["刷新页面后重新提交"])


def _collapse_upload_root(staging: Path) -> None:
    """Drop the synthetic folder component added by directory pickers."""
    entries = list(staging.iterdir())
    if len(entries) != 1 or not entries[0].is_dir():
        return
    selected_root = entries[0]
    children = list(selected_root.iterdir())
    for child in children:
        target = staging / child.name
        if target.exists():
            raise _UploadRejected(
                f"上传目录中存在重复文件名：{child.name}",
                ["请只选择一个 DICOM 序列文件夹"],
            )
        shutil.move(str(child), str(target))
    selected_root.rmdir()


def _prepare_case(case_id: str, source: Path, anonymize: bool) -> None:
    """后台执行导入质控；每个可观察阶段都实时落盘。"""
    active_stage: str | None = "import"
    try:
        update_job(case_id, "running", "import", "正在读取 DICOM 文件并建立本地体数据")
        update_stage(case_id, "import", "running", message="正在读取 DICOM 文件并建立本地体数据")
        volume = load_series(str(source))
        save_volume(case_id, volume)
        update_stage(
            case_id,
            "import",
            "ok",
            message="已读取 DICOM 并建立本地体数据，准备执行输入质控",
            artifacts=["volume/hu.npy", "volume/meta.json"],
            data={"loader": volume.meta.get("loader"), "shape": list(volume.array.shape)},
        )

        active_stage = "qc"
        update_job(case_id, "running", "qc", "正在执行输入质控：检查层厚、序列完整性与隐私标记")
        update_stage(case_id, "qc", "running", message="正在执行输入质控：检查层厚、序列完整性与隐私标记")
        qc = dicom_qc.run_qc(str(source))
        dicom_qc.save_qc(case_id, qc)
        qc_status = "failed" if qc["blocking"] else ("warn" if qc["verdict"] == "warn" else "ok")
        update_stage(
            case_id,
            "qc",
            qc_status,
            message="；".join(qc["reasons"])[:200],
            artifacts=["volume/qc.json", "volume/hu.npy", "volume/meta.json"],
            data={
                "slice_thickness_mm": qc["primary_series"].get("slice_thickness_mm"),
                "pixel_spacing_mm": qc["primary_series"].get("pixel_spacing_mm"),
                "files": qc["primary_series"].get("files"),
                "contains_phi": qc["privacy"]["contains_phi"],
            },
        )
        if qc["blocking"]:
            update_job(case_id, "failed", "qc", "输入质控未通过，导入流程已停止")
            return

        if anonymize:
            active_stage = "anonymize"
            update_job(case_id, "running", "anonymize", "正在生成本地匿名化副本")
            update_stage(case_id, "anonymize", "running", message="正在生成本地匿名化副本")
            result = anonymize_series(str(source), case_dir(case_id) / "anonymized", case_id)
            update_stage(
                case_id,
                "anonymize",
                "ok",
                message=f"已生成匿名化副本 {result['files_written']} 个文件",
                artifacts=["anonymized/"],
                data={"files": result["files_written"]},
            )
        else:
            update_job(case_id, "running", "anonymize", "未勾选匿名化副本，正在记录跳过原因")
            update_stage(
                case_id,
                "anonymize",
                "warn",
                message="未生成匿名化副本（按导入选项跳过）；数据仍只在本机处理",
                data={"skipped": True},
            )

        active_stage = "preview"
        update_job(case_id, "running", "preview", "正在生成轴位、冠状位和矢状位切面预览")
        preview.render_all(case_id)
        update_job(case_id, "completed", None, "导入、质控与切面预览已完成")
    except PipelineError as exc:
        if active_stage in ("import", "qc", "anonymize"):
            try:
                update_stage(case_id, active_stage, "failed", message=exc.reason, data={"error": exc.to_dict()})
            except Exception:
                pass
        update_job(case_id, "failed", active_stage, exc.reason)
    except Exception as exc:  # 后台任务必须把异常转成可观察状态
        if active_stage in ("import", "qc", "anonymize"):
            try:
                update_stage(
                    case_id,
                    active_stage,
                    "failed",
                    message=f"导入阶段出现未处理错误：{exc}",
                    data={"error": {"status": "failed", "reason": str(exc)}},
                    )
            except Exception:
                pass
        update_job(case_id, "failed", active_stage, f"导入阶段出现未处理错误：{exc}")


@app.get("/")
async def index():
    return FileResponse(APP_DIR / "index.html")


@app.get("/api/capabilities")
async def api_capabilities():
    result = capabilities.probe_capabilities()
    # Keep environment detection (installed tools) separate from compute
    # detection (what the local machine can actually run in parallel).
    result["compute"] = detect_compute_capabilities()
    result["ai_interpretation"] = ai_report.get_config()
    return result


@app.get("/api/environment/installers")
async def api_installers():
    """返回固定安装白名单，前端不能借此提交任意命令。"""
    return installers.catalog()


@app.post("/api/environment/install")
async def api_install(body: InstallBody, background_tasks: BackgroundTasks):
    try:
        job = installers.start(body.target)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "error": True,
            "status": "failed",
            "reason": str(exc),
            "remediation": ["从环境页选择固定的安装目标"],
        }) from exc
    background_tasks.add_task(installers.run, job["job_id"])
    return job


@app.get("/api/environment/install/{job_id}")
async def api_install_status(job_id: str):
    job = installers.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail={
            "error": True,
            "status": "missing",
            "reason": "安装任务不存在或服务已重启",
            "remediation": ["回到环境页重新探测，再重新开始安装"],
        })
    return job


@app.get("/api/pick-directory")
async def api_pick_directory():
    """打开本机文件夹选择器；目录内容不会经过浏览器上传。"""
    return await asyncio.to_thread(_pick_directory)


@app.post("/api/cases/upload")
async def api_upload_case(request: Request, background_tasks: BackgroundTasks):
    """接收公网浏览器选中的 DICOM 序列并在服务主机上启动导入。"""
    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith("multipart/form-data"):
        raise HTTPException(status_code=415, detail={
            "error": True,
            "status": "failed",
            "reason": "上传请求必须使用 multipart/form-data",
            "remediation": ["回到病例页重新选择 DICOM 文件夹"],
        })
    content_length = request.headers.get("content-length")
    try:
        declared_length = int(content_length) if content_length else None
    except ValueError:
        declared_length = None
    # Allow multipart field overhead while rejecting an obviously oversized body
    # before Starlette allocates its parser buffers.
    if declared_length is not None and declared_length > _UPLOAD_MAX_TOTAL_BYTES + 128 * 1024 * 1024:
        raise HTTPException(status_code=413, detail={
            "error": True,
            "status": "failed",
            "reason": f"本次上传超过 {_UPLOAD_MAX_TOTAL_BYTES // (1024 * 1024 * 1024)} GB",
            "remediation": ["只选择一套 DICOM 序列，或分批处理"],
        })

    staging: Path | None = None
    try:
        # FastAPI's UploadFile dependency uses the default 1000-file parser cap;
        # the explicit form parser keeps large CT series usable without exposing
        # arbitrary form fields.
        async with request.form(max_files=_UPLOAD_MAX_FILES, max_fields=8, max_part_size=_UPLOAD_MAX_FILE_BYTES) as form:
            files = [item for item in form.getlist("files") if isinstance(item, UploadFile)]
            raw_paths = form.get("relative_paths")
            relative_paths = _parse_upload_paths(raw_paths, len(files))
            synthetic = _parse_upload_bool(form.get("synthetic"), "synthetic")
            anonymize = _parse_upload_bool(form.get("anonymize"), "anonymize")
            staging = await asyncio.to_thread(_store_uploaded_files, files, relative_paths)
    except _UploadRejected as exc:
        raise HTTPException(status_code=exc.status_code, detail={
            "error": True, "status": "failed", "reason": exc.reason, "remediation": exc.remediation,
        }) from exc
    except Exception as exc:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)
        raise HTTPException(status_code=400, detail={
            "error": True,
            "status": "failed",
            "reason": "无法读取上传的 DICOM 文件",
            "remediation": ["确认浏览器允许文件夹上传", "重新选择同一套 DICOM 序列后重试"],
        }) from exc

    if staging is None:
        raise HTTPException(status_code=400, detail={
            "error": True, "status": "failed", "reason": "没有收到 DICOM 文件",
            "remediation": ["点击选择按钮，选择包含同一套 DICOM 序列的文件夹"],
        })
    case_id = None
    try:
        case_id = create_case("browser_upload", "<浏览器上传的 DICOM 文件夹>", synthetic=synthetic)
        source = case_dir(case_id) / "import" / "dicom"
        source.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), str(source))
        staging = None
        update_stage(case_id, "import", "running", message="已接收上传文件，等待读取 DICOM")
        update_job(case_id, "running", "import", "已接收上传文件，等待读取 DICOM")
        background_tasks.add_task(_prepare_case, case_id, source, anonymize)
    except Exception as exc:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)
        if case_id:
            shutil.rmtree(case_dir(case_id), ignore_errors=True)
        raise HTTPException(status_code=500, detail={
            "error": True,
            "status": "failed",
            "reason": "服务端登记上传病例失败",
            "remediation": ["检查服务主机磁盘空间后重试"],
        }) from exc
    return {
        "case_id": case_id,
        "status": "running",
        "message": "已接收上传病例，正在执行输入质控；页面会持续更新阶段进度",
    }


@app.get("/api/cases")
async def api_cases():
    return list_cases()


@app.post("/api/cases")
async def api_create_case(body: NewCaseBody, background_tasks: BackgroundTasks):
    source = Path(body.path)
    if not source.exists() or not source.is_dir():
        raise HTTPException(status_code=400, detail={
            "error": True, "status": "failed", "reason": f"DICOM 文件夹不存在：{body.path}",
            "remediation": ["点击“选择 DICOM 文件夹”选择具体序列目录", "压缩包请先解压"],
        })
    case_id = create_case("synthetic" if body.synthetic else "local_path", "<本机选择的 DICOM 文件夹>", synthetic=body.synthetic)
    # create_case 也被 CLI 复用；Web 导入在后台开始前显式标成 running，避免页面首轮 GET 看到一组静态 pending。
    update_stage(case_id, "import", "running", message="已登记输入，等待后台读取 DICOM")
    update_job(case_id, "running", "import", "已登记输入，等待后台读取 DICOM")
    background_tasks.add_task(_prepare_case, case_id, source, body.anonymize)
    return {
        "case_id": case_id,
        "status": "running",
        "message": "已登记病例，正在执行输入质控；页面会持续更新阶段进度",
    }


@app.get("/api/cases/{case_id}")
async def api_case(case_id: str):
    return _require_case(case_id)


def _scenario_http_error(exc: PipelineError) -> HTTPException:
    """把方案的业务状态映射为可恢复的 HTTP 错误，不暴露本机路径。"""
    if exc.status == "blocked":
        status_code = 409
    elif any(token in exc.reason for token in ("不存在", "缺失")):
        status_code = 404
    else:
        status_code = 400
    return HTTPException(status_code=status_code, detail=exc.to_dict())


def _scenario_links(case_id: str, payload: dict) -> dict:
    scenario_id = payload.get("scenario_id")
    if isinstance(scenario_id, str):
        payload = dict(payload)
        payload["download_url"] = f"/api/cases/{case_id}/scenarios/{scenario_id}/download"
    return payload


@app.post("/api/cases/{case_id}/scenarios")
async def api_create_scenario(case_id: str, request: Request):
    """上传一个鼻塞 STL，生成原始/规范化/初始摆位版本。"""
    _require_case(case_id)
    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith("multipart/form-data"):
        raise HTTPException(status_code=400, detail={
            "error": True,
            "status": "failed",
            "reason": "鼻塞模型上传必须使用 multipart/form-data",
            "remediation": ["选择 STL 文件并提交模型单位（mm、cm、m 或 inch）"],
        })
    try:
        async with request.form(max_files=2, max_fields=4, max_part_size=plug_models.MODEL_MAX_BYTES) as form:
            upload = form.get("model") or form.get("file")
            unit = str(form.get("unit") or "").strip().lower()
            if not isinstance(upload, UploadFile):
                raise PipelineError("failed", "没有收到鼻塞 STL 文件", ["在 model 字段选择 STL 文件"])
            if unit not in plug_models.UNIT_TO_MM:
                raise PipelineError("failed", "鼻塞 STL 单位无效", [f"单位必须是：{', '.join(plug_models.UNIT_TO_MM)}"])
            data = await asyncio.to_thread(upload.file.read, plug_models.MODEL_MAX_BYTES + 1)
            if len(data) > plug_models.MODEL_MAX_BYTES:
                raise PipelineError("failed", f"鼻塞 STL 超过 {plug_models.MODEL_MAX_BYTES // (1024 * 1024)} MB 限制", ["降低模型面数或导出较小的 STL"])
            result = await asyncio.to_thread(scenarios.create_scenario, case_id, data, unit)
            return _scenario_links(case_id, result)
    except PipelineError as exc:
        raise _scenario_http_error(exc) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail={
            "error": True,
            "status": "failed",
            "reason": "读取鼻塞 STL 失败",
            "remediation": ["确认文件未被占用且为标准三角面 STL", "重新上传后重试"],
        }) from exc


@app.get("/api/cases/{case_id}/scenarios")
async def api_list_scenarios(case_id: str):
    _require_case(case_id)
    try:
        return [_scenario_links(case_id, item) for item in await asyncio.to_thread(scenarios.list_scenarios, case_id)]
    except PipelineError as exc:
        raise _scenario_http_error(exc) from exc


@app.get("/api/cases/{case_id}/scenarios/{scenario_id}")
async def api_get_scenario(case_id: str, scenario_id: str):
    _require_case(case_id)
    try:
        result = await asyncio.to_thread(scenarios.get_scenario, case_id, scenario_id)
        return _scenario_links(case_id, result)
    except PipelineError as exc:
        raise _scenario_http_error(exc) from exc


@app.put("/api/cases/{case_id}/scenarios/{scenario_id}/placement")
async def api_update_scenario_placement(case_id: str, scenario_id: str, body: ScenarioPlacementBody):
    _require_case(case_id)
    try:
        result = await asyncio.to_thread(
            scenarios.update_placement,
            case_id,
            scenario_id,
            body.model_dump(exclude={"expected_version"}),
            expected_version=body.expected_version,
        )
        return _scenario_links(case_id, result)
    except PipelineError as exc:
        raise _scenario_http_error(exc) from exc


@app.post("/api/cases/{case_id}/scenarios/{scenario_id}/review")
async def api_review_scenario(case_id: str, scenario_id: str, body: ScenarioReviewBody):
    _require_case(case_id)
    try:
        result = await asyncio.to_thread(
            scenarios.record_review,
            case_id,
            scenario_id,
            approved=body.approved,
            reviewer_role=body.reviewer_role,
            notes=body.notes,
            expected_version=body.expected_version,
        )
        return _scenario_links(case_id, result)
    except PipelineError as exc:
        raise _scenario_http_error(exc) from exc


@app.post("/api/cases/{case_id}/scenarios/{scenario_id}/cfd-prep")
async def api_prepare_scenario_cfd(case_id: str, scenario_id: str, body: ScenarioCfdBody):
    """为已审核摆位生成独立 CFD 算例；不覆盖基准病例算例。"""
    _require_case(case_id)
    try:
        result = await asyncio.to_thread(
            scenario_cfd.prepare_scenario, case_id, scenario_id, version=body.version,
        )
        return _scenario_links(case_id, result)
    except PipelineError as exc:
        raise _scenario_http_error(exc) from exc


@app.post("/api/cases/{case_id}/scenarios/{scenario_id}/cfd-run")
async def api_run_scenario_cfd(case_id: str, scenario_id: str, body: ScenarioCfdBody):
    """提交独立鼻塞算例；求解在后台进行，避免公网请求等待 OpenFOAM。"""
    _require_case(case_id)
    try:
        task = await asyncio.to_thread(
            scenario_jobs.start,
            case_id,
            scenario_id,
            backend=body.backend,
            timeout_s=body.timeout_s,
            version=body.version,
        )
        scenario_payload = await asyncio.to_thread(scenarios.get_scenario, case_id, scenario_id)
        payload = {
            "status": task.get("status", "queued"),
            "task": task,
            "scenario": _scenario_links(case_id, scenario_payload),
        }
        return JSONResponse(status_code=202, content=_scenario_links(case_id, payload))
    except PipelineError as exc:
        raise _scenario_http_error(exc) from exc


@app.get("/api/cases/{case_id}/scenarios/{scenario_id}/cfd-status")
async def api_scenario_cfd_status(case_id: str, scenario_id: str, version: int | None = Query(None, ge=1)):
    _require_case(case_id)
    try:
        return _scenario_links(case_id, await asyncio.to_thread(scenario_cfd.get_status, case_id, scenario_id, version=version))
    except PipelineError as exc:
        raise _scenario_http_error(exc) from exc


@app.get("/api/cases/{case_id}/scenarios/{scenario_id}/comparison")
async def api_scenario_comparison(case_id: str, scenario_id: str, version: int | None = Query(None, ge=1)):
    _require_case(case_id)
    try:
        result = await asyncio.to_thread(scenario_cfd.get_comparison, case_id, scenario_id, version=version)
        selected_version = int(result.get("version")) if isinstance(result.get("version"), int) else version
        await asyncio.to_thread(
            scenarios.record_event,
            case_id,
            scenario_id,
            "scenario_comparison_viewed",
            {"version": selected_version, "status": result.get("status")},
        )
        return result
    except PipelineError as exc:
        raise _scenario_http_error(exc) from exc


@app.get("/api/cases/{case_id}/scenarios/{scenario_id}/download")
async def api_download_scenario(
    case_id: str,
    scenario_id: str,
    artifact: str = Query("bundle"),
    version: int | None = Query(None, ge=1),
):
    _require_case(case_id)
    try:
        path, media_type, filename = await asyncio.to_thread(
            scenarios.download_artifact,
            case_id,
            scenario_id,
            artifact=artifact.strip().lower(),
            version=version,
        )
        return FileResponse(path, media_type=media_type, filename=filename)
    except PipelineError as exc:
        raise _scenario_http_error(exc) from exc


@app.delete("/api/cases/{case_id}")
async def api_delete_case(case_id: str):
    try:
        delete_case(case_id)
    except PipelineError as exc:
        status_code = 409 if exc.status == "blocked" else 400
        raise HTTPException(status_code=status_code, detail=exc.to_dict()) from exc
    return {"case_id": case_id, "status": "deleted", "message": "病例及其本地产物已删除"}


@app.post("/api/cases/{case_id}/segment")
async def api_segment(case_id: str, body: StageBody):
    _require_case(case_id)
    update_stage(case_id, "segment", "running", message="正在执行气道分割，请保持页面打开查看进度")
    try:
        # 阈值、连通域和体数据写盘都是 CPU/IO 密集操作，不能阻塞事件循环；
        # 页面需要在这里持续轮询病例状态来显示“进行中”。
        return await asyncio.to_thread(segmentation.run_segment, case_id, body.params or {})
    except PipelineError as exc:
        _mark_stage_error(case_id, "segment", exc)
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    except Exception as exc:
        _mark_stage_error(case_id, "segment", exc)
        raise


@app.post("/api/cases/{case_id}/segment/import-mask")
async def api_import_review_mask(case_id: str):
    """读取 Slicer 导出的固定修正掩膜，并使旧下游产物失效。"""
    _require_case(case_id)
    try:
        return await asyncio.to_thread(mask_review.import_corrected_mask, case_id)
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail={
            "error": True,
            "status": "failed",
            "reason": f"读取修正掩膜失败：{exc}",
            "remediation": ["确认 Slicer 导出的文件完整，再重试"],
        }) from exc


@app.post("/api/cases/{case_id}/segment/edit-mask")
async def api_edit_mask(case_id: str, body: MaskEditBody):
    """编辑当前切片掩膜；只接受受限坐标点，不接受整幅文件或本机路径。"""
    _require_case(case_id)
    try:
        if body.reset:
            return await asyncio.to_thread(mask_review.reset_mask, case_id)
        if body.operations is not None:
            if not body.operations:
                raise PipelineError("failed", "编辑操作列表不能为空", ["至少提交一笔切片编辑"])
            results = []
            for operation in body.operations:
                results.append(
                    await asyncio.to_thread(
                        mask_review.edit_mask,
                        case_id,
                        operation.view,
                        operation.index,
                        operation.action,
                        [point.model_dump() for point in operation.points],
                        operation.radius,
                    )
                )
            return {"status": "edited", "operations": results, "last": results[-1]}
        if body.view is None or body.index is None or body.action is None:
            raise PipelineError("failed", "编辑请求缺少 view、index 或 action", ["提交切片视图、层号和编辑动作"])
        return await asyncio.to_thread(
            mask_review.edit_mask,
            case_id,
            body.view,
            body.index,
            body.action,
            [point.model_dump() for point in body.points],
            body.radius,
        )
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    except Exception as exc:
        _mark_stage_error(case_id, "segment", exc)
        raise HTTPException(status_code=500, detail={
            "error": True,
            "status": "failed",
            "reason": f"保存切片掩膜编辑失败：{exc}",
            "remediation": ["刷新病例后重试"],
        }) from exc


@app.post("/api/cases/{case_id}/segment/reset-mask")
async def api_reset_mask(case_id: str):
    """恢复首次编辑前的候选掩膜，必要时重新运行候选分割。"""
    _require_case(case_id)
    try:
        return await asyncio.to_thread(mask_review.reset_mask, case_id)
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    except Exception as exc:
        _mark_stage_error(case_id, "segment", exc)
        raise HTTPException(status_code=500, detail={
            "error": True,
            "status": "failed",
            "reason": f"恢复候选掩膜失败：{exc}",
            "remediation": ["刷新病例后重试"],
        }) from exc


@app.post("/api/cases/{case_id}/segment/remove-external-air")
async def api_remove_external_air(case_id: str):
    """按 CT 体内包络清理可识别的体外空气，结果仍需人工复核。"""
    _require_case(case_id)
    try:
        return await asyncio.to_thread(mask_review.auto_remove_external_air, case_id)
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    except Exception as exc:
        _mark_stage_error(case_id, "segment", exc)
        raise HTTPException(status_code=500, detail={
            "error": True,
            "status": "failed",
            "reason": f"自动去除体外空气失败：{exc}",
            "remediation": ["保留当前掩膜，改用画笔/橡皮在三平面视图中手动精修"],
        }) from exc


@app.post("/api/cases/{case_id}/segment/undo-external-air")
async def api_undo_external_air(case_id: str):
    """恢复紧接着上一次体外空气清理之前的两个掩膜。"""
    _require_case(case_id)
    try:
        return await asyncio.to_thread(mask_review.undo_external_air, case_id)
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    except Exception as exc:
        _mark_stage_error(case_id, "segment", exc)
        raise HTTPException(status_code=500, detail={
            "error": True,
            "status": "failed",
            "reason": f"撤销体外空气清理失败：{exc}",
            "remediation": ["刷新病例后重试"],
        }) from exc


@app.get("/api/cases/{case_id}/segment/mask-export")
async def api_export_mask(case_id: str, format: str = "nrrd", scope: str = "full_candidate"):
    """下载指定范围的浏览器编辑掩膜；默认是完整候选。"""
    _require_case(case_id)
    _require_confirmed_mask(case_id)
    try:
        path = await asyncio.to_thread(mask_review.export_mask, case_id, format, scope)
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    media_type = "application/octet-stream" if path.suffix.lower() == ".npy" else "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=path.name)


@app.post("/api/cases/{case_id}/segment/confirm")
async def api_confirm_mask(case_id: str):
    """记录用户完成三平面复核；有体外空气风险时拒绝确认。"""
    _require_case(case_id)
    try:
        return await asyncio.to_thread(mask_review.confirm_mask, case_id)
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail={
            "error": True,
            "status": "failed",
            "reason": f"确认气道掩膜失败：{exc}",
            "remediation": ["刷新病例后重试"],
        }) from exc


@app.get("/api/cases/{case_id}/segment/endplanes")
async def api_get_endplanes(case_id: str, scope: str = "main_airway"):
    """逐层返回端面估算面积与当前范围已保存的选择。"""
    _require_case(case_id)
    try:
        return await asyncio.to_thread(endplanes.get_endplanes, case_id, scope)
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc


@app.get("/api/cases/{case_id}/segment/selection-model")
async def api_selection_model(case_id: str, scope: str = "main_airway", mask_version: int | None = None):
    """当前已确认掩膜的只读 3D 选端预览；与正式 STL/CFD 产物隔离。"""
    _require_case(case_id)
    try:
        path, version = await asyncio.to_thread(selection_preview.selection_model, case_id, scope, mask_version)
        return FileResponse(
            path,
            media_type="model/stl",
            filename="selection-model.stl",
            headers={"X-Mask-Version": str(version), "X-Model-Scope": scope},
        )
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc


@app.post("/api/cases/{case_id}/segment/endplanes/preview")
async def api_preview_oblique_endplanes(case_id: str, body: EndplanesBody):
    """只预检可旋转切面的技术可用性；不保存方案或修改阶段。"""
    _require_case(case_id)
    try:
        if body.anterior_plane is None or body.posterior_plane is None or body.anterior_index is not None or body.posterior_index is not None:
            raise PipelineError(
                "failed", "3D 切面预检需要鼻孔端与鼻咽端的点和法向",
                ["在 3D 中分别设置两张切面后重新预检"],
            )
        return await asyncio.to_thread(
            endplanes.preview_oblique_endplanes,
            case_id, body.scope, body.anterior_plane, body.posterior_plane, body.mask_version,
        )
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc


@app.post("/api/cases/{case_id}/segment/endplanes")
async def api_save_endplanes(case_id: str, body: EndplanesBody):
    """保存 CT 层面或 3D 可旋转切面；旧下游产物随之失效。"""
    _require_case(case_id)
    try:
        has_planes = body.anterior_plane is not None or body.posterior_plane is not None
        has_indices = body.anterior_index is not None or body.posterior_index is not None
        if has_planes and not has_indices and body.anterior_plane is not None and body.posterior_plane is not None:
            return await asyncio.to_thread(
                endplanes.save_oblique_endplanes,
                case_id, body.scope, body.anterior_plane, body.posterior_plane, body.mask_version,
            )
        if has_indices and not has_planes and body.anterior_index is not None and body.posterior_index is not None:
            return await asyncio.to_thread(
                endplanes.save_endplanes,
                case_id, body.scope, body.anterior_index, body.posterior_index, body.mask_version,
            )
        raise PipelineError(
            "failed", "端面参数不完整或同时包含两种选择方式",
            ["分别提供两张 3D 切面的点与法向，或分别提供鼻孔端和鼻咽端 CT 层号"],
        )
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc


@app.post("/api/cases/{case_id}/segment/endplanes/{side}")
async def api_save_single_endplane(case_id: str, side: str, body: SingleEndplaneBody):
    """保存一侧端面；联合完整性在另一个端面保存后核验。"""
    _require_case(case_id)
    try:
        return await asyncio.to_thread(
            endplanes.save_single_endplane, case_id, body.scope, side, body.plane, body.mask_version,
        )
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc


@app.post("/api/cases/{case_id}/surface")
async def api_surface(case_id: str, body: StageBody):
    _require_case(case_id)
    _require_confirmed_mask(case_id)
    update_stage(case_id, "surface", "running", message="正在生成 STL 并执行几何质检，请保持页面打开查看进度")
    try:
        result = await asyncio.to_thread(surface.run_surface, case_id, body.params or {})
        result["geom_qc"] = await asyncio.to_thread(geom_qc.run_geom_qc, case_id)
        return result
    except PipelineError as exc:
        _mark_stage_error(case_id, "surface", exc)
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    except Exception as exc:
        _mark_stage_error(case_id, "surface", exc)
        raise


@app.post("/api/cases/{case_id}/cfd-prep")
async def api_cfd_prep(case_id: str, body: StageBody):
    _require_case(case_id)
    _require_confirmed_mask(case_id)
    if get_case(case_id).get("stages", {}).get("cfd_prep", {}).get("status") == "running":
        raise HTTPException(status_code=409, detail={"status": "blocked", "reason": "该病例正在生成 CFD 算例，请等待当前任务结束"})
    update_stage(case_id, "cfd_prep", "running", message="正在生成 CFD 算例与边界条件，请保持页面打开查看进度")
    try:
        return await asyncio.to_thread(cfd_prep.prepare_case, case_id, body.params or {})
    except PipelineError as exc:
        _mark_stage_error(case_id, "cfd_prep", exc)
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    except Exception as exc:
        _mark_stage_error(case_id, "cfd_prep", exc)
        raise


@app.post("/api/cases/{case_id}/cfd-run")
async def api_cfd_run(case_id: str, body: StageBody):
    _require_case(case_id)
    _require_confirmed_mask(case_id)
    if get_case(case_id).get("stages", {}).get("cfd_run", {}).get("status") == "running":
        raise HTTPException(status_code=409, detail={"status": "blocked", "reason": "该病例已有气流求解在运行，请查看当前进度"})
    update_stage(case_id, "cfd_run", "running", message="正在准备气流求解，请保持页面打开查看进度")
    try:
        result = await asyncio.to_thread(openfoam_runner.run_case, case_id, backend=body.backend)
        result["gate"] = await asyncio.to_thread(openfoam_runner.check_result_gate, case_id)
        return result
    except PipelineError as exc:
        _mark_stage_error(case_id, "cfd_run", exc)
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    except Exception as exc:
        _mark_stage_error(case_id, "cfd_run", exc)
        raise


@app.get("/api/cases/{case_id}/gate")
async def api_gate(case_id: str):
    _require_case(case_id)
    _require_confirmed_mask(case_id)
    return await asyncio.to_thread(_cfd_validation, case_id)


@app.post("/api/cases/{case_id}/cfd-postprocess", status_code=202)
async def api_cfd_postprocess(case_id: str):
    _require_case(case_id)
    _require_confirmed_mask(case_id)
    task = cfd_postprocess_jobs.start(case_id)
    return {"status": task["status"], "task": task}


@app.post("/api/cases/{case_id}/cfd-postprocess/cancel")
async def api_cancel_cfd_postprocess(case_id: str):
    _require_case(case_id)
    task = cfd_postprocess_jobs.cancel(case_id)
    return {"status": task["status"] if task else "missing", "task": task}


@app.get("/api/cases/{case_id}/cfd-results")
async def api_cfd_results(case_id: str):
    from pipeline import cfd_results
    case = _require_case(case_id)
    _require_confirmed_mask(case_id)
    result = await asyncio.to_thread(cfd_results.get_results, case_id)
    task = cfd_postprocess_jobs.get_task(case_id)
    result["task"] = task
    if task and task.get("status") in {"running", "queued"}:
        result["status"] = task["status"]
    elif result["status"] != "ready" and task and task.get("status") in {"failed", "cancelled"}:
        result.update(status=task["status"], reason=task["message"])
    if result["status"] == "ready":
        result["viewer_url"] = f"/api/cases/{case_id}/cfd-results/viewer"
    # 浏览器后处理读取最终 U/p 与网格，既不依赖附加 VTK 导出，
    # 也不把数值/工程复核当作观察真实场的前置条件。
    upstream_running = case.get("job", {}).get("status") == "running" or any(
        stage.get("status") == "running" for stage in case.get("stages", {}).values()
    )
    if (task and task.get("status") in {"running", "queued"}) or upstream_running:
        result["generation"] = {
            "can_generate": False,
            "reason": "该病例正在处理，请等待当前任务完成或先取消后处理",
        }
    else:
        try:
            _foam, _signature, time_name = await asyncio.to_thread(cfd_results._source, case_dir(case_id))
            result["generation"] = {
                "can_generate": True,
                "timestep": time_name,
                "reason": f"最终时间步 {time_name} 的求解场已存在，可直接生成浏览器流场，无需重新求解",
            }
        except PipelineError as exc:
            result["generation"] = {
                "can_generate": False, "reason": exc.reason, "remediation": exc.remediation,
            }
        except (OSError, UnicodeError, ValueError) as exc:
            result["generation"] = {
                "can_generate": False,
                "reason": f"无法核对最终求解场：{exc}",
                "remediation": ["检查最终时间步与网格文件是否可读取后刷新"],
            }
    result["validation"] = await asyncio.to_thread(_cfd_validation, case_id)
    return result


@app.get("/api/cases/{case_id}/cfd-results/viewer")
async def api_cfd_results_viewer(case_id: str):
    from pipeline import cfd_results
    _require_case(case_id)
    _require_confirmed_mask(case_id)
    result = await asyncio.to_thread(cfd_results.get_results, case_id)
    if result["status"] != "ready":
        raise HTTPException(status_code=409, detail={
            "error": True, "status": result["status"],
            "reason": result.get("reason", "没有与当前算例匹配的可用流场"),
            "remediation": ["在结果页单独生成后处理"],
        })
    payload = result.get("payload")
    if not isinstance(payload, str) or not payload:
        raise HTTPException(status_code=409, detail={
            "error": True, "status": "failed",
            "reason": "结果清单缺少可读取的流场文件路径",
            "remediation": ["重新生成可视化结果"],
        })
    path = artifact_path(case_id, payload)
    if not path.is_file():
        raise HTTPException(status_code=409, detail={
            "error": True, "status": "failed",
            "reason": "流场结果文件不存在，不能加载旧结果",
            "remediation": ["重新生成可视化结果"],
        })
    return FileResponse(path, media_type="application/json")


def _require_section_idle(case_id: str) -> None:
    case = _require_case(case_id)
    _require_confirmed_mask(case_id)
    task = cfd_postprocess_jobs.get_task(case_id)
    if (case.get("job", {}).get("status") == "running" or any(
        stage.get("status") == "running" for stage in case.get("stages", {}).values()
    ) or task and task.get("status") in {"running", "queued"}):
        raise HTTPException(409, detail={
            "reason": "病例正在处理，完成后再读取分析截面",
            "remediation": ["等待当前任务完成后刷新"],
        })


@app.get("/api/cases/{case_id}/cfd-sections")
async def api_cfd_sections(case_id: str):
    from pipeline import cfd_sections
    _require_section_idle(case_id)
    return await asyncio.to_thread(cfd_sections.get_sections, case_dir(case_id))


@app.post("/api/cases/{case_id}/cfd-sections/preview")
async def api_cfd_section_preview(case_id: str, body: CfdSectionPreviewBody):
    from pipeline import cfd_sections
    _require_section_idle(case_id)
    return await asyncio.to_thread(cfd_sections.preview_section, case_dir(case_id), body.position_fraction)


@app.post("/api/cases/{case_id}/cfd-sections/analyze")
async def api_analyze_cfd_section(case_id: str, body: CfdSectionAnalysisBody):
    from pipeline import cfd_sections
    _require_section_idle(case_id)
    return await asyncio.to_thread(cfd_sections.analyze_section, case_dir(case_id), body.position_fraction,
                                   body.source_signature, body.geometry_signature)


@app.post("/api/cases/{case_id}/cfd-sections")
async def api_compute_cfd_sections(case_id: str, body: CfdSectionBody):
    raise HTTPException(status_code=410, detail={
        "reason": "旧版六区人工标注已停用，请使用整截面分析接口",
        "remediation": ["拖动模型上的截面或调整滑杆，然后重新计算整截面气流"],
    })


@app.get("/api/cases/{case_id}/report")
async def api_report(case_id: str):
    _require_case(case_id)
    _require_confirmed_mask(case_id)
    update_stage(case_id, "report", "running", message="正在生成患者可读报告，请保持页面打开查看进度")
    try:
        return await asyncio.to_thread(report.build_report, case_id)
    except PipelineError as exc:
        _mark_stage_error(case_id, "report", exc)
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    except Exception as exc:
        _mark_stage_error(case_id, "report", exc)
        raise


@app.get("/api/ai-report/config")
async def api_ai_report_config():
    """返回 AI 报告解读的非敏感配置，不返回 API key。"""
    return ai_report.get_config()


@app.put("/api/ai-report/config")
async def api_set_ai_report_config(body: AiReportConfigBody):
    """在本进程设置 API URL/模型；密钥只保留在当前服务进程内存。"""
    try:
        # 配置页可能只提交被编辑的字段。沿用当前非敏感配置补齐另一个字段，
        # 但没有任何可用 URL 时仍明确阻止保存，避免写入不可用配置。
        current = ai_report.get_config()
        base_url = (body.base_url or current.get("base_url") or "").strip()
        model = body.model if body.model is not None else current.get("model")
        if not base_url:
            raise ai_report.AIReportError("API URL 不能为空", 400, ["填写 OpenAI 兼容 API URL，例如 https://api.openai.com/v1"])
        return ai_report.save_config(base_url, model, body.api_key)
    except ai_report.AIReportError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.to_dict()) from exc


@app.post("/api/ai-report/models")
async def api_ai_report_models(body: AiReportModelsBody):
    """使用临时密钥读取 OpenAI 兼容模型列表，不保存密钥。"""
    try:
        return await asyncio.to_thread(ai_report.list_models, body.base_url, body.api_key)
    except ai_report.AIReportError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.to_dict()) from exc


@app.post("/api/cases/{case_id}/ai-report")
async def api_generate_ai_report(case_id: str):
    """根据已生成的事实报告请求可选 AI 解读。"""
    _require_case(case_id)
    _require_confirmed_mask(case_id)
    report_path = case_dir(case_id) / "report" / "report.json"
    try:
        source = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=409, detail={
            "error": True, "status": "blocked",
            "reason": "事实报告不存在或格式无效，不能生成 AI 解读",
            "remediation": ["先点击“重新生成事实报告”", str(exc)],
        }) from exc
    try:
        return await asyncio.to_thread(ai_report.generate, case_id, source, "observations")
    except ai_report.AIReportError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.to_dict()) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail={
            "error": True,
            "status": "failed",
            "reason": f"生成 AI 解读失败：{exc}",
            "remediation": ["检查 AI 服务配置后重试"],
        }) from exc


@app.get("/api/cases/{case_id}/ai-report")
async def api_get_ai_report(case_id: str):
    """读取已保存的 AI 解读；不存在时返回明确的 missing 状态。"""
    _require_case(case_id)
    _require_confirmed_mask(case_id)
    result = await asyncio.to_thread(ai_report.load, case_id)
    if result is None:
        raise HTTPException(status_code=404, detail={
            "error": True, "status": "missing", "reason": "该病例还没有 AI 解读",
            "remediation": ["先在环境页配置 AI 服务，再点击“生成 AI 解读”"],
        })
    return result


@app.get("/api/cases/{case_id}/preview/{view}")
async def api_preview(case_id: str, view: str):
    _require_case(case_id)
    # 静态预览可能是在分割前生成的；每次请求都按当前 mask 重绘，避免显示旧缓存。
    try:
        path = await asyncio.to_thread(preview.render_view, case_id, view)
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={
            "error": True,
            "status": "failed",
            "reason": str(exc),
            "remediation": ["视图只能是 axial、coronal 或 sagittal"],
        }) from exc
    return FileResponse(path, media_type="image/png")


@app.get("/api/cases/{case_id}/viewer")
async def api_viewer_metadata(case_id: str):
    """返回 CT 浏览器的安全元信息，不把体数据整体发送到浏览器。"""
    _require_case(case_id)
    try:
        return await asyncio.to_thread(preview.viewer_metadata, case_id)
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc


@app.post("/api/cases/{case_id}/open-slicer")
async def api_open_slicer(case_id: str):
    """用本机 Slicer 打开当前病例的高级复核副本；原始路径不会传给外部程序。"""
    _require_case(case_id)
    slicer_path = capabilities.find_slicer()
    if not slicer_path:
        raise HTTPException(status_code=404, detail={
            "error": True,
            "status": "missing",
            "reason": "没有检测到 3D Slicer",
            "remediation": ["到环境页确认 Slicer.exe 路径", "也可以继续使用网页内 CT 复核视窗"],
        })
    try:
        return await asyncio.to_thread(slicer_review.open_case, case_id, slicer_path)
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc


@app.get("/api/cases/{case_id}/slice/{view}")
async def api_slice(
    case_id: str,
    view: str,
    index: int | None = None,
    wl: float = preview.WINDOW_LEVEL,
    ww: float = preview.WINDOW_WIDTH,
    overlay: bool = True,
):
    """按平面和层号返回一张 CT 切片，避免传输整个 NPY 体数据。"""
    _require_case(case_id)
    try:
        metadata = await asyncio.to_thread(preview.viewer_metadata, case_id)
        view_info = metadata["views"].get(view)
        if view_info is None:
            raise ValueError(f"未知视图：{view}")
        current_index = view_info["default_index"] if index is None else index
        image = await asyncio.to_thread(
            preview.render_image,
            case_id,
            view,
            current_index,
            float(wl),
            float(ww),
            bool(overlay),
        )
        output = BytesIO()
        image.save(output, format="PNG", optimize=True)
        return Response(
            content=output.getvalue(),
            media_type="image/png",
            headers={
                "X-Slice-Index": str(current_index),
                "X-Slice-Count": str(view_info["count"]),
                "X-Mask-Available": "1" if metadata["mask_available"] else "0",
            },
        )
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail={
            "error": True,
            "status": "failed",
            "reason": str(exc),
            "remediation": ["检查视图、层号、窗宽和窗位参数"],
        }) from exc


@app.get("/api/cases/{case_id}/model")
async def api_model(case_id: str):
    _require_case(case_id)
    _require_confirmed_mask(case_id)
    path = case_dir(case_id) / "surface" / "airway.stl"
    if not path.exists():
        raise HTTPException(status_code=404, detail={
            "error": True, "status": "missing",
            "reason": "该病例还没有气道 STL",
            "remediation": ["先完成分割，再生成表面"],
        })
    return FileResponse(path, media_type="model/stl", filename="airway.stl")


@app.get("/api/cases/{case_id}/report-file")
async def api_report_file(case_id: str):
    _require_case(case_id)
    _require_confirmed_mask(case_id)
    path = case_dir(case_id) / "report" / "report.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail={
            "error": True, "status": "missing",
            "reason": "还没有生成报告",
            "remediation": ["点击“生成报告”"],
        })
    return FileResponse(path, media_type="text/markdown", filename=f"{case_id}-report.md")


@app.get("/api/cases/{case_id}/artifact")
async def api_artifact(case_id: str, rel: str):
    _require_case(case_id)
    normalized_rel = rel.replace("\\", "/").lower()
    if normalized_rel.startswith(("surface/", "cfd/", "report/")):
        _require_confirmed_mask(case_id)
    if normalized_rel.startswith("cfd/results/generations/"):
        from pipeline import cfd_results
        result = await asyncio.to_thread(cfd_results.get_results, case_id)
        payload = result.get("payload")
        if result["status"] != "ready" or not isinstance(payload, str) or normalized_rel != payload.lower():
            raise HTTPException(status_code=409, detail={"reason": "该流场资源已过期，请读取当前病例结果"})
    try:
        path = artifact_path(case_id, rel)
    except PipelineError as exc:
        raise HTTPException(status_code=403, detail=exc.to_dict()) from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail={
            "error": True, "status": "missing", "reason": f"文件不存在：{rel}",
            "remediation": ["先完成对应阶段"],
        })
    return FileResponse(path, filename=path.name)


@app.post("/api/fetch")
async def api_fetch(body: FetchBody):
    dest = APP_DIR / "cases" / "_downloads"
    result = fetch_direct(body.url, dest, authorized=body.authorized, allowed_domains=body.allowed_domains)
    return result


@app.get("/reference-result")
async def reference_result():
    """项目提供的他人参考结果样本，与病例结果严格隔离。"""
    return FileResponse(REFERENCE_HTML, media_type="text/html")
