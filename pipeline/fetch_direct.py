"""授权直链本机下载。

- 只处理经用户确认授权、无需登录的 http/https 直链
- 需要登录/会话凭据的链接 → 提示手动下载后用本地目录导入
- 下载落到本机病例目录，影像不会上传到任何外部服务
- 日志中的 URL 会剥离 query/token，只记 host + path
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import requests

DEFAULT_MAX_BYTES = 2 * 1024 ** 3      # 2 GB
LOGIN_HINTS = ("login", "sso", "signin", "auth", "passport", "oauth", "session")


def sanitize_url(url: str) -> str:
    """日志用：剥离查询串与令牌，只保留 host + path。"""
    try:
        parts = urlparse(url)
        path = parts.path if len(parts.path) <= 120 else parts.path[:120] + "..."
        return f"{parts.scheme}://{parts.netloc}{path}"
    except Exception:
        return "<无法解析的 URL>"


def _looks_like_login(status_code: int, content_type: str, body_head: bytes) -> bool:
    if status_code in (401, 403):
        return True
    if "text/html" in (content_type or "").lower():
        head = body_head[:4000].decode("utf-8", errors="ignore").lower()
        if any(hint in head for hint in ("password", "登录", "验证码", "sign in", "登录后才")):
            return True
    return False


def fetch_direct(
    url: str,
    dest_dir: str | Path,
    authorized: bool = False,
    allowed_domains: list[str] | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    connect_timeout: int = 15,
    read_timeout: int = 120,
) -> dict:
    """下载经授权的直链到本机目录；返回状态与产物清单。"""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    log_lines: list[str] = []
    safe_url = sanitize_url(url)

    def log(message: str) -> None:
        log_lines.append(message)

    if not authorized:
        return {
            "status": "need_authorization",
            "message": "请先确认该影像链接已获得患者授权，并勾选授权确认后再下载",
            "url": safe_url,
        }

    parts = urlparse(url)
    if parts.scheme not in ("http", "https"):
        return {
            "status": "unsupported_scheme",
            "message": f"只支持 http/https 直链，当前为 {parts.scheme or '未知'}",
            "url": safe_url,
        }

    if allowed_domains:
        host = (parts.hostname or "").lower()
        if not any(host == d.lower() or host.endswith("." + d.lower()) for d in allowed_domains):
            return {
                "status": "domain_not_allowed",
                "message": f"域名 {host} 不在白名单内；如需下载请先把它加入允许域名",
                "url": safe_url,
            }

    log(f"开始下载 {safe_url}（体积上限 {max_bytes} 字节）")
    try:
        response = requests.get(
            url, stream=True, timeout=(connect_timeout, read_timeout), allow_redirects=True
        )
    except requests.RequestException as exc:
        return {
            "status": "network_error",
            "message": f"下载请求失败：{exc}",
            "url": safe_url,
            "log": log_lines,
        }

    status_code = response.status_code
    content_type = response.headers.get("Content-Type", "")
    if status_code in (401, 403) or any(hint in (parts.path or "").lower() for hint in LOGIN_HINTS):
        response.close()
        return {
            "status": "auth_required",
            "message": "该链接需要登录或会话凭据。请手动下载后，用本地目录导入（风见不会代为登录）",
            "url": safe_url,
        }

    first_chunk = b""
    total = 0
    temp_path = dest / "_download.tmp"
    with open(temp_path, "wb") as handle:
        for chunk in response.iter_content(chunk_size=1024 * 256):
            if not chunk:
                continue
            if not first_chunk:
                first_chunk = chunk
                if _looks_like_login(status_code, content_type, chunk):
                    handle.close()
                    response.close()
                    temp_path.unlink(missing_ok=True)
                    return {
                        "status": "auth_required",
                        "message": "该链接返回的是登录/中转页面，需要登录或会话凭据。请手动下载后用本地目录导入",
                        "url": safe_url,
                    }
            total += len(chunk)
            if total > max_bytes:
                handle.close()
                response.close()
                temp_path.unlink(missing_ok=True)
                return {
                    "status": "too_large",
                    "message": f"文件超过体积上限 {max_bytes} 字节，已中止并清理临时文件",
                    "url": safe_url,
                }
            handle.write(chunk)
    response.close()

    if total == 0:
        temp_path.unlink(missing_ok=True)
        return {"status": "empty_response", "message": "下载内容为空", "url": safe_url}

    name_hint = Path(parts.path).name or "download"
    if zipfile.is_zipfile(temp_path):
        unpack_dir = dest / "unpacked"
        unpack_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(temp_path) as archive:
            for member in archive.namelist():
                target = (unpack_dir / member).resolve()
                if not str(target).startswith(str(unpack_dir.resolve())):
                    raise ValueError(f"压缩包内存在非法路径：{member}")
            archive.extractall(unpack_dir)
        temp_path.unlink(missing_ok=True)
        files = [p for p in unpack_dir.rglob("*") if p.is_file()]
        log(f"已解压 {len(files)} 个文件到 {unpack_dir}")
        result = {
            "status": "ok",
            "format": "zip",
            "output_dir": str(unpack_dir),
            "files": len(files),
            "bytes": total,
            "url": safe_url,
            "log": log_lines,
            "notice": "下载的是经授权的影像；进入处理前仍需完成本地匿名化与授权核验",
        }
        (dest / ".download.log").write_text("\n".join(log_lines), encoding="utf-8")
        return result

    suffix = Path(name_hint).suffix.lower()
    if suffix in (".7z", ".rar"):
        temp_path.unlink(missing_ok=True)
        return {
            "status": "unsupported_format",
            "message": f"暂不支持 {suffix} 格式，请手动解压后导入本地目录",
            "url": safe_url,
        }

    final_path = dest / (name_hint or "download.bin")
    temp_path.replace(final_path)
    log(f"已保存单文件 {final_path.name}（{total} 字节）")
    (dest / ".download.log").write_text("\n".join(log_lines), encoding="utf-8")
    return {
        "status": "ok",
        "format": "single_file",
        "output_dir": str(dest),
        "files": 1,
        "bytes": total,
        "file_name": final_path.name,
        "url": safe_url,
        "log": log_lines,
        "notice": "下载的是经授权的影像；进入处理前仍需完成本地匿名化与授权核验",
    }
