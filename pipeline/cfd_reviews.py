"""Append-only CFD review evidence bound to one immutable solver run.

These records document a human review; they never change the numerical or
patient-publication gate. A shared team login cannot authenticate an engineer.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from . import cases
from .errors import PipelineError

REVIEW_DB = "review_evidence.sqlite3"
RUN_FILES = (
    "run_status.json", "logs/simpleFoam.log", "system/fvSolution",
    "system/controlDict", "mesh_cache.json", "case_meta.json",
)
RESULT_ARTIFACTS = (
    "logs/checkMesh.log", "constant/polyMesh/*",
    "[0-9]*/U", "[0-9]*/p", "[0-9]*/phi", "[0-9]*/polyMesh/*",
    "postProcessing/**/*.dat",
)
GRID_ROLES = ("coarse", "medium", "fine")


class CfdReviewError(Exception):
    def __init__(self, reason: str, status: int = 409):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cfd_dir(case_id: str) -> Path:
    try:
        cases.get_case(case_id)
    except PipelineError as exc:
        raise CfdReviewError("病例不存在或编号无效", 404) from exc
    root = cases.case_dir(case_id)
    directory = root / "cfd"
    if root.is_symlink() or directory.is_symlink() or not directory.is_dir():
        raise CfdReviewError("病例 CFD 目录不存在或不是普通目录")
    return directory


def _run_context(case_id: str) -> dict:
    directory = _cfd_dir(case_id)
    status_path = directory / "run_status.json"
    if status_path.is_symlink() or not status_path.is_file():
        return {"status": "missing", "reason": "尚无可复核的求解记录"}
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CfdReviewError("求解状态文件无法读取") from exc
    if not isinstance(status, dict) or status.get("status") != "ran" or not isinstance(status.get("run_id"), str) or not status["run_id"]:
        return {"status": "not_ready", "reason": "求解尚未成功结束，不能绑定复核记录"}
    hashes = {}
    resolved_directory = directory.resolve()
    for relative in RUN_FILES:
        path = directory / relative
        if path.is_symlink() or not path.resolve().is_relative_to(resolved_directory) or not path.is_file():
            return {"status": "incomplete", "reason": f"缺少算例证据：cfd/{relative}"}
        hashes[relative] = _sha256_file(path)
    # Large mesh/field artifacts are fingerprinted by path, size and mtime_ns;
    # hashing hundreds of MB on each page load would stall the review UI.
    artifacts = {}
    for pattern in RESULT_ARTIFACTS:
        for path in directory.glob(pattern):
            if path.is_symlink() or not path.resolve().is_relative_to(resolved_directory):
                raise CfdReviewError("算例产物含目录外链接，不能绑定复核记录")
            if path.is_file():
                relative = path.relative_to(directory).as_posix()
                stat = path.stat()
                artifacts[relative] = [stat.st_size, stat.st_mtime_ns]
    identity = json.dumps({"run_id": status["run_id"], "hashes": hashes,
                           "artifacts": artifacts}, sort_keys=True).encode("utf-8")
    return {"status": "ready", "run_id": status["run_id"],
            "signature": hashlib.sha256(identity).hexdigest(), "hashes": hashes,
            "artifacts": artifacts}


@contextmanager
def _db(case_id: str):
    _cfd_dir(case_id)
    # The CFD directory is cleared when an upstream mask changes. Keep review
    # history at the case root so the previous run remains auditable as stale.
    path = cases.case_dir(case_id) / REVIEW_DB
    if path.is_symlink():
        raise CfdReviewError("复核记录数据库不能是符号链接")
    connection = sqlite3.connect(path, timeout=10)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("""CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY, created_at INTEGER NOT NULL, kind TEXT NOT NULL,
            reviewer TEXT NOT NULL, decision TEXT NOT NULL, note TEXT NOT NULL,
            evidence_ref TEXT NOT NULL, grids TEXT NOT NULL, run_id TEXT NOT NULL,
            run_signature TEXT NOT NULL, input_hashes TEXT NOT NULL
        )""")
        yield connection
        connection.commit()
    finally:
        connection.close()


def _text(value: object, label: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str):
        raise CfdReviewError(f"{label}必须填写", 400)
    clean = value.strip()
    if not minimum <= len(clean) <= maximum or any(ord(ch) < 32 for ch in clean):
        raise CfdReviewError(f"{label}长度须为 {minimum}–{maximum} 字且不能含控制字符", 400)
    return clean


def _reference(value: object) -> str:
    clean = _text(value, "去标识报告编号或 SHA-256", 8, 200)
    if any(ch in clean for ch in "/\\:") or "@" in clean:
        raise CfdReviewError("证据引用请填写报告编号或 SHA-256，不要填写路径、网址或邮箱", 400)
    return clean


def _grids(value: object) -> list[dict]:
    if not isinstance(value, list) or len(value) != 3:
        raise CfdReviewError("请分别填写粗、中、细三套独立网格算例", 400)
    result = []
    for expected, item in zip(GRID_ROLES, value):
        if not isinstance(item, dict) or item.get("role") != expected:
            raise CfdReviewError("网格顺序必须为粗、中、细", 400)
        run_ref = _text(item.get("run_ref"), "网格算例编号", 3, 100)
        if any(ch in run_ref for ch in "/\\:"):
            raise CfdReviewError("网格算例编号不能包含路径", 400)
        cells = item.get("cells")
        pressure_drop_pa = item.get("pressure_drop_pa")
        if isinstance(cells, bool) or not isinstance(cells, int) or cells < 1:
            raise CfdReviewError("网格单元数必须是正整数", 400)
        if isinstance(pressure_drop_pa, bool) or not isinstance(pressure_drop_pa, (int, float)) or not math.isfinite(pressure_drop_pa) or pressure_drop_pa < 0:
            raise CfdReviewError("压降必须是有限的非负 Pa 数值", 400)
        result.append({"role": expected, "run_ref": run_ref, "cells": cells,
                       "pressure_drop_pa": float(pressure_drop_pa)})
    if len({item["run_ref"] for item in result}) != 3 or not result[0]["cells"] < result[1]["cells"] < result[2]["cells"]:
        raise CfdReviewError("三套网格须有不同算例编号，且单元数依次增加", 400)
    return result


def list_reviews(case_id: str) -> dict:
    current = _run_context(case_id)
    with _db(case_id) as connection:
        rows = connection.execute("SELECT * FROM reviews ORDER BY id DESC LIMIT 200").fetchall()
    records = []
    for row in rows:
        item = dict(row)
        item["grids"] = json.loads(item["grids"])
        item["input_hashes"] = json.loads(item["input_hashes"])
        item["stale"] = current.get("status") != "ready" or item["run_signature"] != current["signature"]
        records.append(item)
    return {"run": current, "records": records,
            "release_effect": "记录仅供人工复核，不改变数值闸门或患者发布资格"}


def record_review(case_id: str, payload: dict) -> dict:
    current = _run_context(case_id)
    if current.get("status") != "ready":
        raise CfdReviewError(current.get("reason", "算例尚不可复核"))
    if payload.get("expected_signature") != current["signature"]:
        raise CfdReviewError("算例在填写期间发生变化，请刷新后重新核对")
    kind = payload.get("kind")
    if kind not in {"mesh_sensitivity", "engineering_review"}:
        raise CfdReviewError("复核记录类型无效", 400)
    reviewer = _text(payload.get("reviewer"), "记录人或工程师姓名", 2, 80)
    note = _text(payload.get("note"), "复核说明", 10, 2000)
    evidence_ref = _reference(payload.get("evidence_ref"))
    if kind == "mesh_sensitivity":
        grids = _grids(payload.get("grids"))
        decision = "recorded_unverified"
    else:
        grids = []
        decision = payload.get("decision")
        if decision not in {"changes_required", "reviewed"}:
            raise CfdReviewError("请选择工程师意见", 400)
    created_at = int(time.time())
    with _db(case_id) as connection:
        count = connection.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        if count >= 1000:
            raise CfdReviewError("此病例复核记录已达上限，请联系管理员归档")
        cursor = connection.execute("""INSERT INTO reviews
            (created_at,kind,reviewer,decision,note,evidence_ref,grids,run_id,run_signature,input_hashes)
            VALUES(?,?,?,?,?,?,?,?,?,?)""", (created_at, kind, reviewer, decision, note,
            evidence_ref, json.dumps(grids, ensure_ascii=False), current["run_id"],
            current["signature"], json.dumps({"files": current["hashes"],
                                               "artifacts": current["artifacts"]}, sort_keys=True)))
    return {"id": cursor.lastrowid, "created_at": created_at, "kind": kind,
            "run_id": current["run_id"], "run_signature": current["signature"],
            "status": "recorded_unverified", "release_effect": "none"}
