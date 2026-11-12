"""Publish an allowlisted, history-free source copy to a ModelScope Studio.

Credentials are read only from the two explicitly named .env files. Tokens are
passed to Git through process environment, never URLs, arguments or files.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://modelscope.cn/openapi/v1"
SECRET_NAMES = ("WINDCATCHER_CLOUD_USERNAME", "WINDCATCHER_CLOUD_PASSWORD", "WINDCATCHER_CLOUD_SESSION_SECRET")
FILES = (
    "Dockerfile", ".dockerignore", "requirements.txt", "requirements-cloud.txt",
    "server.py", "cloud.py", "index.html", "LICENSE", "ms_deploy.json",
    "deploy/login.html", "deploy/patient-view.html", "deploy/reference-view.html",
    "deploy/start.sh", "scripts/backup_identity.py",
)


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        name, sep, value = line.partition("=")
        if sep and not name.strip().startswith("#"):
            values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def request(token: str, path: str, method: str = "GET", body=None):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        # Do not print request/response bodies: secret operations can echo values.
        raise RuntimeError(f"ModelScope {method} {path}: HTTP {exc.code}") from None
    if not result.get("success"):
        raise RuntimeError(f"ModelScope {method} {path}: {result.get('code', 'unsuccessful')}")
    return result.get("data", {})


def git(directory: Path, args: list[str], environment: dict):
    result = subprocess.run(["git", *args], cwd=directory, env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        # Git diagnostics contain no credential-bearing URL or command argument.
        raise RuntimeError(f"Git {args[0]} failed: {result.stderr[-1500:]}")
    return result.stdout.strip()


def copy_source(destination: Path):
    sources = [ROOT / name for name in FILES]
    sources += sorted((ROOT / "pipeline").glob("*.py"))
    sources += sorted(path for path in (ROOT / "static").rglob("*") if path.is_file())
    for source in sources:
        relative = source.relative_to(ROOT)
        if not source.is_file():
            raise RuntimeError(f"发布包缺少必需文件：{relative}")
        if source.is_symlink() or source.suffix.lower() in {".dcm", ".ima", ".stl", ".nii", ".vtu", ".vtk", ".sqlite3", ".env"}:
            raise RuntimeError(f"发布包中存在不允许的文件：{relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    (destination / "README.md").write_text("""---
license: mit
sdk: docker
app_port: 7860
---
# 风见 Windcatcher · 鼻腔气流工作台

参赛队「空穴来风」的研究预览版。公开页介绍项目，研究工作台需密码登录。
支持 DICOM 质控、阈值与连通域分割、人工复核、气道几何、OpenFOAM 11 稳态 CFD 与流场展示。

研究原型，不用于诊断、治疗或手术决策。计算完成不等于数值验证通过。
只上传已授权、已去标识数据。所有登录者共用病例，非多租户系统。
本创空间不携带任何本机病例、DICOM、STL、流场、日志或凭证。

## 运行

Docker 服务监听 0.0.0.0:7860。平台密文变量必须设置：
- WINDCATCHER_CLOUD_USERNAME
- WINDCATCHER_CLOUD_PASSWORD（至少 16 位）
- WINDCATCHER_CLOUD_SESSION_SECRET（至少 32 位）

反向代理部署须设置明文变量 WINDCATCHER_CLOUD_PUBLIC_ORIGIN 为实际公开 HTTPS 源（例如 https://owner-repo.ms.show），用于校验登录与写操作的 Origin。发布脚本自动按创空间 host 设置。

没有完整访问配置时拒绝启动。会话 8 小时有效，密码变更会使旧会话失效。
免费 2 核、8GB CPU 资源仅适合小规模研究体验，大型算例需评估内存与耗时。
程序默认把工作数据写入容器 /data，当前没有配置长期存储；重建前请导出研究产物。
本机目录导入、桌面工具启动、软件安装、原始参考病例和本机自动化接口不会公开。
""", encoding="utf-8")
    return sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--secrets-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--publish", action="store_true", help="Create/update Studio, push source and trigger deployment")
    args = parser.parse_args()
    config = json.loads((ROOT / "ms_deploy.json").read_text(encoding="utf-8"))
    token = read_env(args.env_file)["MODELSCOPE_API_TOKEN"]
    owner = request(token, "/users/me")["username"]
    repo = config["repo_name"]
    path = f"/studios/{owner}/{repo}"
    directory = ROOT / ".local-modelscope" / ("repo" if args.publish else "source")
    directory.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_TRACE") and not k.startswith("GIT_CONFIG_")}
    auth = base64.b64encode(("oauth2:" + token).encode()).decode()
    env.update({"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "http.extraHeader", "GIT_CONFIG_VALUE_0": "Authorization: Basic " + auth,
                "GIT_CONFIG_KEY_1": "credential.helper", "GIT_CONFIG_VALUE_1": "", "GIT_TERMINAL_PROMPT": "0"})
    if args.publish:
        secrets = read_env(args.secrets_file)
        if any(not secrets.get(name) for name in SECRET_NAMES):
            raise RuntimeError("本地 .env 尚未配置云端访问的三个变量")
        try:
            studio = request(token, path)
        except RuntimeError as exc:
            if "HTTP 404" not in str(exc):
                raise
            studio = request(token, "/studios", "POST", {"owner": owner, **{k: v for k, v in config.items() if k != "port"}})
            studio = request(token, path)
        if studio.get("repo_name") != repo:
            raise RuntimeError("创空间身份不匹配")
        changes = {k: v for k, v in config.items() if k not in {"repo_name", "port"} and studio.get(k) != v}
        if changes:
            request(token, path + "/settings", "PATCH", changes)
        existing = request(token, path + "/secrets")
        names = {item["key"] for item in existing.get("secrets", [])}
        for name in SECRET_NAMES:
            request(token, path + "/secrets", "PUT" if name in names else "POST", {"key": name, "value": secrets[name]})
        variables = request(token, path + "/variables")
        origin_key = "WINDCATCHER_CLOUD_PUBLIC_ORIGIN"
        public_origin = studio.get("host") or f"https://{owner}-{repo}.ms.show"
        current = {item["key"]: item.get("value") for item in variables.get("variables", [])}
        if current.get(origin_key) != public_origin:
            request(token, path + "/variables", "PUT" if origin_key in current else "POST", {"key": origin_key, "value": public_origin})
        remote = f"https://modelscope.cn/studios/{owner}/{repo}.git"
        if not (directory / ".git").exists():
            if any(directory.iterdir()):
                raise RuntimeError("发布目录非空，拒绝覆盖未登记文件")
            git(directory, ["clone", remote, "."], env)
        elif git(directory, ["remote", "get-url", "origin"], env) != remote:
            raise RuntimeError("发布目录的 Git 远端不匹配")
        # Fetch rather than force-push; an existing Studio's history is retained.
        git(directory, ["fetch", "origin"], env)
        heads = git(directory, ["ls-remote", "--heads", "origin"], env)
        if heads:
            git(directory, ["merge", "--ff-only", "origin/master"], env)
    sources = copy_source(directory)
    print(json.dumps({"source_files": len(sources) + 1, "source_bytes": sum(p.stat().st_size for p in sources), "source_dir": str(directory), "studio": f"https://modelscope.cn/studios/{owner}/{repo}"}, ensure_ascii=False))
    if args.publish:
        git(directory, ["config", "user.name", "Windcatcher deployment"], env)
        git(directory, ["config", "user.email", "deployment@windcatcher.invalid"], env)
        git(directory, ["add", "--all"], env)
        if git(directory, ["status", "--porcelain"], env):
            git(directory, ["commit", "-m", "Deploy Windcatcher research preview with cloud access control"], env)
        git(directory, ["push", "origin", "HEAD:master"], env)
        runtime = request(token, path + "/deploy", "POST")
        print(json.dumps({"commit": git(directory, ["rev-parse", "HEAD"], env), "runtime": runtime}, ensure_ascii=False))


if __name__ == "__main__":
    main()
