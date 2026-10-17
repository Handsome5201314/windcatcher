"""stdio MCP adapter. Never starts a second web server or edits case files.

Run using the optional MCP environment; all business writes go over loopback HTTP.
"""

from __future__ import annotations

import os
from typing import Literal
from urllib.parse import urlsplit

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, model_validator


class CfdItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str = Field(pattern=r"^case-\d{8}-\d{6}-[0-9a-f]{4}$")
    scenario_id: str | None = Field(default=None, pattern=r"^plug-\d{8}-\d{6}-[0-9a-f]{8}$")
    version: int | None = Field(default=None, ge=1)
    backend: Literal["native", "wsl", "docker"] | None = None
    timeout_s: int = Field(default=7200, ge=60, le=86400)

    @model_validator(mode="after")
    def scenario_version(self):
        if (self.scenario_id is None) != (self.version is None):
            raise ValueError("scenario_id 和 version 必须同时指定")
        return self


def _base_url():
    value = os.environ.get("WINDCATCHER_URL", "http://127.0.0.1:8765").rstrip("/")
    url = urlsplit(value)
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"} or url.username or url.password or url.path or url.query or url.fragment:
        raise ToolError("WINDCATCHER_URL 必须是本机 HTTP 服务地址，例如 http://127.0.0.1:8765")
    return value


async def _request(method, path, *, body=None, params=None):
    try:
        async with httpx.AsyncClient(base_url=_base_url(), timeout=30, trust_env=False, follow_redirects=False) as client:
            response = await client.request(method, path, json=body, params=params)
    except httpx.HTTPError as exc:
        raise ToolError("无法连接风见本机服务；请启动原服务后重试，勿重复启动第二套服务") from exc
    try:
        data = response.json()
    except ValueError as exc:
        raise ToolError(f"风见服务返回非 JSON 响应，HTTP {response.status_code}") from exc
    if not response.is_success:
        error = data.get("detail", data)
        reason = error.get("reason") if isinstance(error, dict) else None
        raise ToolError(f"风见 HTTP {response.status_code}：{reason or '参数无效或服务拒绝执行，请检查请求'}")
    return data


mcp = FastMCP("windcatcher", instructions=(
    "鼻腔 CFD 本机科研计算工具。先检查病例 readiness；仅可运行已人工确认的基准算例和已审核的鼻塞版本。"
    "submit_cfd_batch 立即返回；之后按 poll_after_seconds 查询，避免频繁轮询。"
    "solved 只表示求解器执行完成，必须另外检查 gate.numeric_ok / eligible。不得伪造人工审核或医疗结论。"
    "不要读取或上传 CT、患者身份或完整计算场；这里只交换匿名编号和摘要。MCP 无法得知宿主 token 账单。"
))
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)


@mcp.tool(annotations=READ)
async def service_status() -> dict:
    """检查原服务和持久队列是否在线，不启动服务、不调用大模型。"""
    return await _request("GET", "/api/automation/status")


@mcp.tool(annotations=READ)
async def list_cfd_cases(offset: int = Field(default=0, ge=0), limit: int = Field(default=20, ge=1, le=50)) -> dict:
    """分页读取匿名病例编号和阶段状态；不返回 DICOM、源路径或患者信息。"""
    return await _request("GET", "/api/automation/cases", params={"offset": offset, "limit": limit})


@mcp.tool(annotations=READ)
async def inspect_cfd_case(case_id: str = Field(pattern=r"^case-\d{8}-\d{6}-[0-9a-f]{4}$")) -> dict:
    """读取准备状态、阻断原因和鼻塞当前版本；不会替代人工核对或审核。"""
    return await _request("GET", f"/api/automation/cases/{case_id}")


@mcp.tool(annotations=WRITE)
async def submit_cfd_batch(request_key: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$"),
                           items: list[CfdItem] = Field(min_length=1, max_length=50)) -> dict:
    """串行后台计算 1–50 个已准备的基准或审核鼻塞任务。相同幂等键及参数不会重复求解；新运行用新键。"""
    return await _request("POST", "/api/automation/batches", body={"request_key": request_key, "items": [item.model_dump() for item in items]})


@mcp.tool(annotations=READ)
async def get_cfd_batch(batch_id: str = Field(pattern=r"^batch-[0-9a-f]{16}$")) -> dict:
    """查询进度及最终指标、数值/工程闸门、鼻塞前后对比；失败和阻断分别保留。"""
    return await _request("GET", f"/api/automation/batches/{batch_id}")


@mcp.tool(annotations=WRITE)
async def cancel_queued_cfd(batch_id: str = Field(pattern=r"^batch-[0-9a-f]{16}$")) -> dict:
    """只取消尚未开始的批量项；正在运行的求解器会继续，返回其真实状态。"""
    return await _request("POST", f"/api/automation/batches/{batch_id}/cancel")


if __name__ == "__main__":
    mcp.run(transport="stdio")
