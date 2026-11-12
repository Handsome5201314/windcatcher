"""Bounded, retry-safe upload staging; only finalized streams become cases."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import shutil
import threading
import time
from pathlib import Path

CHUNK_BYTES = 4 * 1024 * 1024
SESSION_TTL = 3600
LOCK = threading.Lock()  # One cloud worker; disk state survives separate requests.
LOGGER = logging.getLogger(__name__)


class UploadError(Exception):
    def __init__(self, status: int, reason: str):
        self.status = status
        self.reason = reason
        super().__init__(reason)


def _folder(root: Path, upload_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        raise UploadError(404, "上传任务不存在，请重新选择文件夹")
    path = root / (".windcatcher-chunks-" + upload_id)
    if path.is_symlink():
        raise UploadError(400, "上传暂存目录无效")
    return path


def _save(folder: Path, manifest: dict) -> None:
    temporary = folder / "manifest.tmp"
    temporary.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    temporary.replace(folder / "manifest.json")


def _load(folder: Path) -> dict:
    if not (folder / "manifest.json").is_file():
        raise UploadError(404, "上传任务不存在或已过期，请重新选择文件夹")
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if time.time() - manifest["updated_at"] > SESSION_TTL:
        shutil.rmtree(folder)
        raise UploadError(410, "上传任务已过期，请重新选择文件夹")
    return manifest


def create(root: Path, files: list[dict], synthetic: bool, anonymize: bool) -> dict:
    with LOCK:
        root.mkdir(parents=True, exist_ok=True)
        active = []
        for path in root.glob(".windcatcher-chunks-*"):
            if (not re.fullmatch(r"\.windcatcher-chunks-[0-9a-f]{32}", path.name)
                    or path.is_symlink() or not path.is_dir()):
                continue
            try:
                manifest = _load(path)
            except UploadError as exc:
                if exc.status == 410:
                    continue
                if exc.status == 404:
                    # A crash between mkdir and the first manifest commit can
                    # leave an unusable folder; it must not block every upload.
                    try:
                        shutil.rmtree(path)
                    except OSError as cleanup_exc:
                        LOGGER.warning("Orphaned upload staging cleanup failed: %s",
                                       type(cleanup_exc).__name__)
                    continue
                raise
            if "result" not in manifest:
                active.append(manifest)
        if len(active) >= 4:
            raise UploadError(429, "已有多个上传任务，请等待完成或一小时后重试")
        total = sum(item["size"] for item in files)
        reserved = sum(item["total_bytes"] - item["received_bytes"] for item in active)
        if shutil.disk_usage(root).free < total + reserved + 64 * 1024 * 1024:
            raise UploadError(507, "服务器暂存空间不足，请先导出并清理不再需要的病例")
        upload_id = secrets.token_hex(16)
        folder = _folder(root, upload_id)
        folder.mkdir()
        (folder / "files").mkdir()
        manifest = {"files": files, "synthetic": synthetic, "anonymize": anonymize,
                    "total_bytes": total, "received_bytes": 0, "updated_at": time.time()}
        try:
            _save(folder, manifest)
        except OSError:
            shutil.rmtree(folder)
            raise
        return {"upload_id": upload_id, "chunk_size": CHUNK_BYTES, "total_bytes": total}


def append(root: Path, upload_id: str, offset: int, body: bytes) -> dict:
    if not body or len(body) > CHUNK_BYTES:
        raise UploadError(413, "上传分块大小无效，请刷新页面后重试")
    with LOCK:
        folder = _folder(root, upload_id)
        manifest = _load(folder)
        received = manifest["received_bytes"]
        digest = hashlib.sha256(body).hexdigest()
        if manifest.get("last_chunk") == [offset, len(body), digest]:
            return {"received_bytes": received}
        if "result" in manifest or offset != received:
            raise UploadError(409, "上传位置不一致，请重新选择文件夹后重试")
        if offset + len(body) > manifest["total_bytes"]:
            raise UploadError(413, "上传内容超过文件清单声明的大小")
        if shutil.disk_usage(root).free < len(body) + 16 * 1024 * 1024:
            raise UploadError(507, "服务器暂存空间不足")
        stream_start = 0
        cursor = 0
        for item in manifest["files"]:
            file_end = stream_start + item["size"]
            if offset < file_end and cursor < len(body):
                position = max(offset - stream_start, 0)
                take = min(item["size"] - position, len(body) - cursor)
                target = folder / "files" / item["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("r+b" if target.exists() else "wb") as output:
                    # Discard a partial write if the previous request stopped
                    # before its manifest commit, then retry from committed data.
                    output.truncate(position)
                    output.seek(position)
                    output.write(body[cursor:cursor + take])
                cursor += take
                offset += take
            stream_start = file_end
            if cursor == len(body):
                break
        manifest.update(received_bytes=received + len(body), updated_at=time.time(),
                        last_chunk=[received, len(body), digest])
        _save(folder, manifest)
        return {"received_bytes": manifest["received_bytes"]}


def complete(root: Path, upload_id: str, register, recover=None) -> dict:
    with LOCK:
        folder = _folder(root, upload_id)
        try:
            manifest = _load(folder)
        except UploadError as exc:
            if exc.status in {404, 410} and recover is not None:
                result = recover()
                if result is not None:
                    return result
            raise
        if "result" in manifest:
            return manifest["result"]
        if manifest["received_bytes"] != manifest["total_bytes"]:
            raise UploadError(409, "文件尚未全部上传，不能开始导入")
        staging = folder / "files"
        for item in manifest["files"]:
            path = staging / item["path"]
            if not path.is_file() or path.stat().st_size != item["size"]:
                # Registration may have moved the files before its final
                # manifest commit. Resolve the durable case receipt first.
                result = recover() if recover is not None else None
                if result is not None:
                    manifest.update(result=result, updated_at=time.time())
                    try:
                        _save(folder, manifest)
                    except OSError:
                        LOGGER.warning("Upload %s manifest repair failed; using committed case receipt", upload_id)
                    return result
                raise UploadError(409, "暂存文件不完整，请重新上传")
        result = register(staging, manifest["synthetic"], manifest["anonymize"])
        manifest.update(result=result, updated_at=time.time())
        try:
            _save(folder, manifest)
        except OSError:
            # A committed case must not be reported as failed: an error response
            # drops FastAPI BackgroundTasks and a retry would duplicate it.
            LOGGER.warning("Upload %s final manifest failed; using committed case receipt", upload_id)
        return result


def cancel(root: Path, upload_id: str) -> dict:
    with LOCK:
        folder = _folder(root, upload_id)
        if folder.exists():
            shutil.rmtree(folder)
        return {"status": "cancelled"}
