"""Private, version-pinned patient views of an approved CFD result.

Only the cloud gateway calls this module. The public JSON is a strict view of
the browser result, never a path into cases/ or a live research API response.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from . import automation, cases, cfd_results, openfoam_runner
from .config import APP_DIR
from .cfd_validation import KINEMATIC_PRESSURE_DIMENSIONS, pressure_metadata

SHARE_DIR = Path(os.environ.get("WINDCATCHER_SHARE_DIR", str(APP_DIR / ".local-shares")))
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PUBLIC_FIELDS = ("units", "bounds_m", "wall", "patches", "streamlines", "slices", "ranges")
MESH_FIELDS = ("axis", "positions", "indices", "speeds", "pressure")
LINE_FIELDS = ("positions", "speeds", "pressure", "integration_time_s")
PARAMETER_FIELDS = ("solver", "turbulence_model", "inlet_mode", "total_flow_lpm", "nu",
                    "rho_kg_m3", "background_cell_mm", "add_layers", "pressure_field_unit")
INPUT_FILES = ("segment/segmentation.json", "segment/mask.npy", "segment/candidate_mask.npy",
               "segment/endplanes.json",
               "surface/airway.stl", "cfd/case_meta.json")


class ShareError(Exception):
    def __init__(self, reason: str, status: int = 409):
        super().__init__(reason)
        self.reason = reason
        self.status = status


@contextmanager
def _db():
    SHARE_DIR.mkdir(parents=True, exist_ok=True)
    (SHARE_DIR / "snapshots").mkdir(exist_ok=True)
    connection = sqlite3.connect(SHARE_DIR / "shares.sqlite3", timeout=10)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS shares (
          id TEXT PRIMARY KEY, case_id TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
          code_salt BLOB NOT NULL, code_hash BLOB NOT NULL, created_at INTEGER NOT NULL,
          expires_at INTEGER NOT NULL, revoked_at INTEGER, reviewer TEXT NOT NULL,
          review_note TEXT NOT NULL, source_signature TEXT NOT NULL,
          payload_sha256 TEXT NOT NULL, input_hashes TEXT NOT NULL,
          parameters TEXT NOT NULL, gate_summary TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS shares_case_id ON shares(case_id);
        CREATE TABLE IF NOT EXISTS review_events (
          id INTEGER PRIMARY KEY, case_id TEXT NOT NULL, created_at INTEGER NOT NULL,
          operator TEXT NOT NULL, stage TEXT NOT NULL, action TEXT NOT NULL,
          reason TEXT NOT NULL, input_hashes TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS access_events (
          id INTEGER PRIMARY KEY, share_id TEXT NOT NULL, created_at INTEGER NOT NULL,
          action TEXT NOT NULL, outcome TEXT NOT NULL, client_hash TEXT NOT NULL,
          FOREIGN KEY (share_id) REFERENCES shares(id)
        );
    """)
        with connection:
            yield connection
    finally:
        connection.close()


def _numbers(values, *, minimum: int = 0, exact: int | None = None) -> bool:
    return (isinstance(values, list) and (len(values) == exact if exact is not None else len(values) >= minimum)
            and all(type(value) in (int, float) and abs(value) < 1e300 and math.isfinite(value) for value in values))


def _mesh(value: dict, *, axis: bool = False) -> dict:
    if not isinstance(value, dict) or not _numbers(value.get("positions"), minimum=9) \
            or len(value["positions"]) % 3:
        raise ValueError("三维网格无效")
    if axis and value.get("axis") not in {"x", "y", "z"}:
        raise ValueError("切片方向无效")
    result = {key: value[key] for key in MESH_FIELDS if key in value and (key != "axis" or axis)}
    count = len(result["positions"]) // 3
    if "indices" in result and (not _numbers(result["indices"]) or len(result["indices"]) % 3
                                or any(type(i) is not int or i < 0 or i >= count for i in result["indices"])):
        raise ValueError("网格索引无效")
    for key in ("speeds", "pressure"):
        if key in result and not _numbers(result[key], exact=count):
            raise ValueError("网格采样无效")
    return result


def _public_view(raw: dict) -> dict:
    if not isinstance(raw, dict) or any(key not in raw for key in PUBLIC_FIELDS):
        raise ValueError("缺少可视化必需字段")
    units = raw["units"]
    if not isinstance(units, dict) or units.get("coordinates") != "m" or \
            units.get("velocity") != "m/s" or units.get("pressure") not in {"Pa", "m2/s2", "m²/s²"}:
        raise ValueError("流场单位无效")
    if not _numbers(raw["bounds_m"], exact=6) or not all(
        raw["bounds_m"][i] < raw["bounds_m"][i + 1] for i in (0, 2, 4)
    ):
        raise ValueError("流场空间范围无效")
    if not isinstance(raw["patches"], dict) or not isinstance(raw["slices"], list) \
            or not isinstance(raw["streamlines"], list) or not isinstance(raw["ranges"], dict):
        raise ValueError("流场结构无效")
    if any(key not in {"inlet", "outlet"} for key in raw["patches"]):
        raise ValueError("未知边界面")
    ranges = {key: raw["ranges"].get(key) for key in ("velocity", "pressure")}
    if any(not _numbers(values, exact=2) for values in ranges.values()):
        raise ValueError("流场色阶无效")
    lines = []
    for line in raw["streamlines"]:
        if not isinstance(line, dict) or not _numbers(line.get("positions"), minimum=6) \
                or len(line["positions"]) % 3:
            raise ValueError("流线无效")
        clean = {key: line[key] for key in LINE_FIELDS if key in line}
        count = len(clean["positions"]) // 3
        if not _numbers(clean.get("speeds"), exact=count):
            raise ValueError("流线速度无效")
        for key in ("pressure", "integration_time_s"):
            if key in clean and not _numbers(clean[key], exact=count):
                raise ValueError("流线采样无效")
        lines.append(clean)
    return {
        "units": {key: units[key] for key in ("coordinates", "velocity", "pressure")},
        "bounds_m": raw["bounds_m"], "wall": _mesh(raw["wall"]),
        "patches": {key: _mesh(value) for key, value in raw["patches"].items()},
        "streamlines": lines, "slices": [_mesh(value, axis=True) for value in raw["slices"]],
        "ranges": ranges,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _input_hashes(case_id: str) -> dict:
    root = cases.case_dir(case_id)
    result = {}
    for rel in INPUT_FILES:
        path = cases.artifact_path(case_id, rel)
        result[rel] = _sha256_file(path) if path.is_file() and not path.is_symlink() else None
    return result


def record_review(case_id: str, operator: str, stage: str, action: str, reason: str) -> dict:
    cases.get_case(case_id)
    if stage not in {"import", "qc", "segment", "surface", "cfd_prep", "cfd_run", "cfd_results"}:
        raise ShareError("复核阶段无效", 400)
    if action not in {"correction", "qc", "note"} or not 4 <= len(reason.strip()) <= 1000:
        raise ShareError("请填写具体修改或质控原因（4–1000 字）", 400)
    timestamp = int(time.time())
    hashes = _input_hashes(case_id)
    snapshot_dir = None
    if stage == "segment" and action == "correction":
        root = cases.case_dir(case_id)
        source = root / "segment" / "mask.npy"
        if source.is_symlink() or not source.is_file():
            raise ShareError("当前没有可留存的修正掩膜，先完成掩膜编辑")
        version_root = root / "review_versions"
        if version_root.is_symlink():
            raise ShareError("掩膜版本目录不能是符号链接")
        snapshot_dir = version_root / secrets.token_hex(12)
        snapshot_dir.mkdir(parents=True, exist_ok=False)
        try:
            for name in ("mask.npy", "candidate_mask.npy"):
                source = root / "segment" / name
                if source.is_file() and not source.is_symlink():
                    target = snapshot_dir / name
                    shutil.copyfile(source, target)
                    digest = _sha256_file(target)
                    if digest != hashes.get("segment/" + name):
                        raise ShareError("保存复核版本期间掩膜发生变化，请重试")
                    hashes["review_snapshot/" + name] = {
                        "path": str(target.relative_to(root)).replace("\\", "/"), "sha256": digest}
        except Exception:
            shutil.rmtree(snapshot_dir)
            raise
    try:
        with _db() as db:
            cursor = db.execute("INSERT INTO review_events(case_id,created_at,operator,stage,action,reason,input_hashes) VALUES(?,?,?,?,?,?,?)",
                                (case_id, timestamp, operator, stage, action, reason.strip(),
                                 json.dumps(hashes, sort_keys=True)))
    except Exception:
        if snapshot_dir is not None:
            shutil.rmtree(snapshot_dir)
        raise
    return {"id": cursor.lastrowid, "case_id": case_id, "created_at": timestamp,
            "operator": operator, "stage": stage, "action": action, "reason": reason.strip()}


def list_reviews(case_id: str) -> list[dict]:
    cases.get_case(case_id)
    with _db() as db:
        return [dict(row) for row in db.execute(
            "SELECT id,created_at,operator,stage,action,reason,input_hashes FROM review_events WHERE case_id=? ORDER BY id",
            (case_id,))]


def publish(case_id: str, reviewer: str, note: str, days: int = 7) -> dict:
    return _publish(case_id, reviewer, note, days, test_mode=False)


def publish_test(case_id: str, reviewer: str, note: str, recipient_confirmed: bool) -> dict:
    if recipient_confirmed is not True:
        raise ShareError("请先确认患者本人同意接收未验证的测试可视化", 400)
    return _publish(case_id, reviewer, note, 1, test_mode=True)


def _publish(case_id: str, reviewer: str, note: str, days: int, *, test_mode: bool) -> dict:
    if not 10 <= len(note.strip()) <= 1000 or not 1 <= days <= 30:
        raise ShareError("审核说明至少 10 字；有效期须为 1–30 天", 400)
    case = cases.get_case(case_id)
    if case.get("job", {}).get("status") == "running" or any(
        stage.get("status") == "running" for stage in case.get("stages", {}).values()
    ):
        raise ShareError("病例正在处理，不能发布")
    automation.require_confirmed_mask(case_id)
    gate = openfoam_runner.check_result_gate(case_id)
    if not test_mode and (gate.get("eligible") is not True or gate.get("numeric_ok") is not True):
        raise ShareError("CFD 数值、网格敏感性或工程复核未通过，禁止发布")
    if test_mode:
        status_path = cases.artifact_path(case_id, "cfd/run_status.json")
        try:
            run_status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ShareError("没有已完成的 CFD 求解，不能生成测试分享") from exc
        if not isinstance(run_status, dict) or run_status.get("status") != "ran" or not run_status.get("run_id"):
            raise ShareError("CFD 求解尚未完成，不能生成测试分享")
    result = cfd_results.get_results(case_id)
    if result.get("status") != "ready" or not result.get("source_signature"):
        raise ShareError("当前 CFD 可视化不存在或已过期，不能发布")
    rel = result.get("payload")
    if not isinstance(rel, str):
        raise ShareError("CFD 结果清单缺少文件路径")
    source = cases.artifact_path(case_id, rel)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > 200 * 1024 * 1024:
        raise ShareError("CFD 可视化文件缺失或超过发布上限")
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
        view = _public_view(raw)
        snapshot = json.dumps(view, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise ShareError("CFD 可视化文件无法安全发布") from exc
    signature = result["source_signature"]
    current = cfd_results.get_results(case_id)
    if current.get("source_signature") != signature or current.get("status") != "ready":
        raise ShareError("发布期间上游结果发生变化，请重新审核")
    meta_path = cases.artifact_path(case_id, "cfd/case_meta.json")
    if not meta_path.is_file():
        raise ShareError("缺少 CFD 参数记录，不能发布")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ShareError("CFD 参数记录无法读取，不能发布") from exc
    if not isinstance(meta, dict):
        raise ShareError("CFD 参数记录无效，不能发布")
    params = {key: meta.get(key) for key in PARAMETER_FIELDS}
    params["stage_versions"] = {key: {"status": entry.get("status"), "updated_at": entry.get("updated_at")}
                                for key, entry in case.get("stages", {}).items()}
    hashes = _input_hashes(case_id)
    share_id = secrets.token_hex(16)
    token = secrets.token_urlsafe(32)
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(12))
    salt = secrets.token_bytes(16)
    code_hash = hashlib.pbkdf2_hmac("sha256", code.encode(), salt, 200_000)
    created = int(time.time())
    expires = created + days * 86400
    path = SHARE_DIR / "snapshots" / (share_id + ".json")
    temp = SHARE_DIR / "snapshots" / ("." + share_id + ".tmp")
    try:
        with _db() as db:
            temp.write_bytes(snapshot)
            temp.replace(path)
            db.execute("""INSERT INTO shares VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                share_id, case_id, hashlib.sha256(token.encode()).hexdigest(), salt, code_hash,
                created, expires, None, reviewer, note.strip(), signature,
                hashlib.sha256(snapshot).hexdigest(), json.dumps(hashes, sort_keys=True),
                json.dumps(params, sort_keys=True), json.dumps({"share_mode": "test" if test_mode else "approved",
                "recipient_confirmed": bool(test_mode), "eligible": gate.get("eligible") is True,
                "numeric_ok": gate.get("numeric_ok"), "checks": [{"name": c.get("name"), "status": c.get("status")}
                for c in gate.get("checks", [])]}, ensure_ascii=False)))
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        temp.unlink(missing_ok=True)
    return {"id": share_id, "case_id": case_id, "url": "/share/" + token,
            "access_code": code, "created_at": created, "expires_at": expires,
            "source_signature": signature, "mode": "test" if test_mode else "approved"}


def list_shares(case_id: str) -> list[dict]:
    cases.get_case(case_id)
    with _db() as db:
        records = [dict(row) for row in db.execute(
            "SELECT id,case_id,created_at,expires_at,revoked_at,reviewer,review_note,source_signature,payload_sha256,input_hashes,parameters,gate_summary FROM shares WHERE case_id=? ORDER BY created_at DESC",
            (case_id,))]
    for record in records:
        record["mode"] = json.loads(record["gate_summary"]).get("share_mode", "approved")
    return records


def share_mode(row: dict) -> str:
    return json.loads(row["gate_summary"]).get("share_mode", "approved")


def revoke(case_id: str, share_id: str) -> None:
    with _db() as db:
        row = db.execute("SELECT id FROM shares WHERE id=? AND case_id=?", (share_id, case_id)).fetchone()
        if row is None:
            raise ShareError("分享记录不存在", 404)
        db.execute("UPDATE shares SET revoked_at=COALESCE(revoked_at,?) WHERE id=?", (int(time.time()), share_id))
    (SHARE_DIR / "snapshots" / (share_id + ".json")).unlink(missing_ok=True)


def revoke_case(case_id: str) -> None:
    if not (SHARE_DIR / "shares.sqlite3").is_file():
        return
    with _db() as db:
        ids = [row[0] for row in db.execute("SELECT id FROM shares WHERE case_id=?", (case_id,))]
        db.execute("UPDATE shares SET revoked_at=COALESCE(revoked_at,?) WHERE case_id=?", (int(time.time()), case_id))
    for share_id in ids:
        (SHARE_DIR / "snapshots" / (share_id + ".json")).unlink(missing_ok=True)


def lookup(token: str) -> dict:
    if not TOKEN_PATTERN.fullmatch(token):
        raise ShareError("链接无效或已失效", 404)
    digest = hashlib.sha256(token.encode()).hexdigest()
    with _db() as db:
        row = db.execute("SELECT * FROM shares WHERE token_hash=?", (digest,)).fetchone()
    if (row is None or row["revoked_at"] is not None or row["expires_at"] <= time.time()
            or cases.deletion_pending(row["case_id"])):
        raise ShareError("链接无效或已失效", 404)
    return dict(row)


def log_access(share_id: str, action: str, outcome: str, client: str, key: bytes) -> None:
    address_hash = hmac.new(key, client.encode(), hashlib.sha256).hexdigest()[:24]
    with _db() as db:
        db.execute("INSERT INTO access_events(share_id,created_at,action,outcome,client_hash) VALUES(?,?,?,?,?)",
                   (share_id, int(time.time()), action, outcome, address_hash))


def verify_code(row: dict, code: str) -> bool:
    if len(code) > 64:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", code.strip().upper().encode(), row["code_salt"], 200_000)
    return hmac.compare_digest(digest, row["code_hash"])


def read_snapshot(row: dict) -> bytes:
    path = SHARE_DIR / "snapshots" / (row["id"] + ".json")
    if not path.is_file() or path.is_symlink():
        raise ShareError("已发布模型暂不可用", 503)
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != row["payload_sha256"]:
        raise ShareError("已发布模型校验失败", 503)
    return data


def read_patient_model(row: dict) -> bytes:
    """Derive display units from the verified snapshot and its pinned parameters."""
    data = read_snapshot(row)
    try:
        params = json.loads(row['parameters'])
    except (ValueError, TypeError) as exc:
        raise ShareError('发布时的计算参数记录无法读取，请联系团队核对', 503) from exc
    return display_model_bytes(data, params)


def display_model_bytes(data: bytes, params: dict) -> bytes:
    """Convert a sanitized, version-pinned display model without changing its source."""
    try:
        view = json.loads(data)
        if view["units"]["pressure"] == "Pa":
            return data
        factor = pressure_metadata(KINEMATIC_PRESSURE_DIMENSIONS, params)["pa_multiplier"]
        if factor is None:
            return data
        for source in [view["wall"], *view["patches"].values(), *view["slices"], *view["streamlines"]]:
            if "pressure" in source:
                source["pressure"] = [value * factor for value in source["pressure"]]
        view["ranges"]["pressure"] = [value * factor for value in view["ranges"]["pressure"]]
        view["units"]["pressure"] = "Pa"
        return json.dumps(view, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        raise ShareError("压力展示数据无法安全换算，请联系团队核对发布记录", 503) from exc
