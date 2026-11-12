"""Bounded, fail-closed gateway to the official DeepSeek Harness SDK worker."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import defaultdict, deque

from scripts import harness_action_selector
from scripts.harness_readonly_sidecar import SummaryError, _unique_pairs, _validate
from scripts.harness_sdk_worker import validate_metering, validate_model_output


_SLOTS = threading.BoundedSemaphore(2)
_BUDGET_LOCK = threading.Lock()
_CALLS: dict[str, deque[float]] = defaultdict(deque)
MAX_PER_HOUR = 5
TIMEOUT_SECONDS = 60
DOCKER_HOST = ("npipe:////./pipe/dockerDesktopLinuxEngine" if sys.platform == "win32"
               else "unix:///var/run/docker.sock")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z", re.ASCII)
_NETWORK_ID = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_PROXY_NAME = re.compile(r"windcatcher-harness-proxy-[0-9a-f]{12}\Z", re.ASCII)
_WORKER_NAME = re.compile(r"windcatcher-harness-[0-9a-f]{32}\Z", re.ASCII)
_INSTANCE = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z", re.ASCII)
_OFFICIAL_HOST = "api.deepseek.com"
_RELAY_HOST = "tongyimohe.aitoken.credit"
_RELAY_BASE_URL = "https://tongyimohe.aitoken.credit/v1"
_RELAY_MODEL = "gpt-6.1-sol"
_PROXY_ALIAS = "windcatcher-harness-proxy"
_PROXY_URL = f"http://{_PROXY_ALIAS}:3128"
_LOGGER = logging.getLogger(__name__)


class AdviceUnavailable(RuntimeError):
    def __init__(self, reason: str, status: int = 503):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def enabled() -> bool:
    return os.environ.get("WINDCATCHER_HARNESS_ADVICE") == "1"


def actions_enabled() -> bool:
    """Keep action selection off even when the legacy advice pilot is on."""
    return enabled() and os.environ.get("WINDCATCHER_HARNESS_ACTIONS") == "1"


def _configuration() -> tuple[str, str, str, str, str]:
    if not enabled():
        raise AdviceUnavailable("DeepSeek Harness 只读试点尚未启用")
    docker = shutil.which("docker")
    image = os.environ.get("WINDCATCHER_HARNESS_IMAGE", "")
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    model = os.environ.get("WINDCATCHER_HARNESS_MODEL", "")
    base_url = os.environ.get("WINDCATCHER_HARNESS_BASE_URL", "")
    if (not docker or not _IMAGE_ID.fullmatch(image) or not 1 <= len(key) <= 512
            or not key.isascii() or any(ord(char) < 33 or ord(char) > 126 for char in key)
            or not _MODEL.fullmatch(model) or model != _RELAY_MODEL
            or base_url != _RELAY_BASE_URL):
        raise AdviceUnavailable("DeepSeek Harness 隔离运行依赖尚未配置")
    return docker, image, key, model, base_url


def _charge(actor_id: str) -> None:
    now = time.monotonic()
    with _BUDGET_LOCK:
        recent = _CALLS[actor_id]
        while recent and now - recent[0] >= 3600:
            recent.popleft()
        if len(recent) >= MAX_PER_HOUR:
            raise AdviceUnavailable("本账号一小时内的只读建议次数已用完", 429)
        recent.append(now)


def _docker_env() -> dict[str, str]:
    # These values are for the host Docker CLI only; docker run passes none on.
    allowed = {"systemroot", "windir", "path", "pathext", "userprofile",
               "appdata", "localappdata", "programdata"}
    env = {name: value for name, value in os.environ.items() if name.lower() in allowed}
    return env


def _docker_inspect(docker: str, env: dict[str, str], kind: str,
                    identifier: str) -> dict:
    result = subprocess.run(
        [docker, "--host", DOCKER_HOST, kind, "inspect", identifier,
         "--format={{json .}}"], capture_output=True, timeout=10,
        check=False, env=env,
    )
    if result.returncode or len(result.stdout) > 65536:
        raise ValueError("Docker inspect unavailable")
    value = json.loads(result.stdout, object_pairs_hook=_unique_pairs)
    if not isinstance(value, dict):
        raise ValueError("Docker inspect invalid")
    return value


def _verified_egress_network(docker: str, env: dict[str, str],
                             worker_image: str, base_url: str) -> str | None:
    """Accept only the dedicated isolated network and a running labeled proxy."""
    network_id = os.environ.get("WINDCATCHER_HARNESS_EGRESS_NETWORK_ID", "")
    if not network_id:
        return None
    proxy_image = os.environ.get("WINDCATCHER_HARNESS_EGRESS_PROXY_IMAGE", "")
    if _NETWORK_ID.fullmatch(network_id) is None or _IMAGE_ID.fullmatch(proxy_image) is None:
        raise AdviceUnavailable("DeepSeek Harness 出站网络配置无效")
    try:
        network = _docker_inspect(docker, env, "network", network_id)
        instance = network.get("Labels", {}).get("org.windcatcher.harness.instance")
        if (not isinstance(network, dict) or network.get("Id") != network_id
                or network.get("Driver") != "bridge" or network.get("Internal") is not True
                or network.get("EnableIPv6") is not False
                or not isinstance(instance, str) or _INSTANCE.fullmatch(instance) is None
                or network.get("Name") != f"windcatcher-harness-internal-{instance[:12]}"
                or not isinstance(network.get("Options"), dict)
                or network["Options"].get(
                    "com.docker.network.bridge.gateway_mode_ipv4") != "isolated"
                or not isinstance(network.get("Containers"), dict)
                or not 1 <= len(network["Containers"]) <= 3):
            raise ValueError("Network is not isolated")
        proxies = [(container_id, details) for container_id, details
                   in network["Containers"].items()
                   if isinstance(details, dict)
                   and _PROXY_NAME.fullmatch(details.get("Name", ""))]
        if len(proxies) != 1:
            raise ValueError("Dedicated proxy is unavailable")
        proxy_id, _ = proxies[0]
        proxy = _docker_inspect(docker, env, "container", proxy_id)
        proxy_env = proxy.get("Config", {}).get("Env")
        target_env = ([value for value in proxy_env
                       if isinstance(value, str) and value.startswith(
                           "WINDCATCHER_HARNESS_TARGET_HOST=")]
                      if isinstance(proxy_env, list) else [])
        expected_host = _RELAY_HOST
        networks = proxy.get("NetworkSettings", {}).get("Networks", {})
        if not isinstance(networks, dict) or len(networks) != 2:
            raise ValueError("Proxy network count invalid")
        matching = [entry for entry in networks.values()
                    if isinstance(entry, dict) and entry.get("NetworkID") == network_id]
        if (not isinstance(proxy, dict) or proxy.get("Id") != proxy_id
                or proxy.get("Name") != "/" + network["Containers"][proxy_id]["Name"]
                or proxy.get("Image") != proxy_image
                or proxy.get("State", {}).get("Running") is not True
                or proxy.get("Config", {}).get("Labels", {}).get("windcatcher.role")
                != "harness-egress-proxy"
                or proxy.get("Config", {}).get("Labels", {}).get(
                    "org.windcatcher.harness.instance") != instance
                or proxy.get("Config", {}).get("User") != "10001:10001"
                 or target_env != [f"WINDCATCHER_HARNESS_TARGET_HOST={expected_host}"]
                or proxy.get("Path") != "node"
                or proxy.get("Args") != ["/opt/windcatcher/proxy.js"]
                or proxy.get("HostConfig", {}).get("ReadonlyRootfs") is not True
                or proxy.get("HostConfig", {}).get("CapDrop") != ["ALL"]
                or proxy.get("HostConfig", {}).get("PortBindings")
                or proxy.get("Mounts")
                or len(matching) != 1
                or _PROXY_ALIAS not in (matching[0].get("Aliases") or [])):
            raise ValueError("Proxy identity or topology invalid")
        outbound = [(name, entry) for name, entry in networks.items()
                    if isinstance(entry, dict) and entry.get("NetworkID") != network_id]
        if (len(outbound) != 1
                or outbound[0][0] != f"windcatcher-harness-outbound-{instance[:12]}"):
            raise ValueError("Proxy outbound network invalid")
        outbound_id = outbound[0][1].get("NetworkID")
        if not isinstance(outbound_id, str) or _NETWORK_ID.fullmatch(outbound_id) is None:
            raise ValueError("Proxy outbound network invalid")
        egress = _docker_inspect(docker, env, "network", outbound_id)
        if (egress.get("Id") != outbound_id or egress.get("Name") != outbound[0][0]
                or egress.get("Driver") != "bridge" or egress.get("Internal") is not False
                or egress.get("EnableIPv6") is not False
                or egress.get("Labels", {}).get("org.windcatcher.harness.instance") != instance
                or not isinstance(egress.get("Containers"), dict)
                or set(egress["Containers"]) != {proxy_id}):
            raise ValueError("Proxy outbound network invalid")
        for container_id, details in network["Containers"].items():
            if container_id == proxy_id:
                continue
            name = details.get("Name") if isinstance(details, dict) else None
            if (not isinstance(name, str) or _WORKER_NAME.fullmatch(name) is None
                    or _NETWORK_ID.fullmatch(container_id) is None):
                raise ValueError("Unexpected container on isolated network")
            worker = _docker_inspect(docker, env, "container", container_id)
            worker_networks = worker.get("NetworkSettings", {}).get("Networks", {})
            if (worker.get("Id") != container_id or worker.get("Name") != "/" + name
                    or worker.get("Image") != worker_image
                    or worker.get("State", {}).get("Running") is not True
                    or worker.get("Path") != "/opt/harness-venv/bin/python"
                    or worker.get("Args") != [
                        "-s", "/opt/windcatcher/scripts/harness_container_worker.py"]
                    or worker.get("Config", {}).get("Labels", {}).get("windcatcher.role")
                    != "harness-worker"
                    or worker.get("Config", {}).get("User") != "10001:10001"
                    or worker.get("HostConfig", {}).get("ReadonlyRootfs") is not True
                    or worker.get("HostConfig", {}).get("CapDrop") != ["ALL"]
                    or worker.get("HostConfig", {}).get("PortBindings")
                    or worker.get("Mounts")
                    or not isinstance(worker_networks, dict) or len(worker_networks) != 1
                    or next(iter(worker_networks.values())).get("NetworkID") != network_id):
                raise ValueError("Unexpected worker topology")
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError,
            AttributeError, SummaryError) as exc:
        raise AdviceUnavailable("DeepSeek Harness 受控出站未就绪") from exc
    return network_id


def _docker_command(docker: str, image: str, name: str,
                    network_id: str | None = None) -> list[str]:
    network = (["--network", network_id,
                "--env", f"HTTPS_PROXY={_PROXY_URL}",
                "--env", f"HTTP_PROXY={_PROXY_URL}"]
               if network_id is not None else ["--network=none"])
    return [
        docker, "--host", DOCKER_HOST, "run", "--rm", "-i", "--pull=never",
        "--name", name, "--label", "windcatcher.role=harness-worker",
        *network, "--user=10001:10001",
        "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", "--pids-limit=64",
        "--memory=512m", "--memory-swap=512m", "--cpus=1",
        "--log-driver=none",
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=1777",
        "--tmpfs=/home/harness:rw,noexec,nosuid,nodev,size=32m,uid=10001,gid=10001,mode=700",
        "--workdir=/tmp", image,
    ]


def _remove_container(docker: str, name: str, env: dict[str, str]) -> bool:
    try:
        removed = subprocess.run([docker, "--host", DOCKER_HOST, "rm", "-f", name],
                                 capture_output=True, timeout=10, check=False, env=env)
        if removed.returncode == 0:
            return True
        existing = subprocess.run([docker, "--host", DOCKER_HOST, "container",
                                   "inspect", name], capture_output=True, timeout=10,
                                  check=False, env=env)
        if existing.returncode == 0:
            _LOGGER.error("Harness container cleanup unconfirmed: %s", name)
            return False
        remaining = subprocess.run(
            [docker, "--host", DOCKER_HOST, "container", "ls", "-a", "-q",
             "--no-trunc", "--filter", f"name=^/{name}$"],
            capture_output=True, timeout=10, check=False, env=env,
        )
        if remaining.returncode == 0 and not remaining.stdout.strip():
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    _LOGGER.error("Harness container cleanup unconfirmed: %s", name)
    return False


def _stop_container(process: subprocess.Popen, docker: str, name: str,
                    env: dict[str, str]) -> None:
    try:
        _kill_tree(process)
    except (OSError, subprocess.SubprocessError):
        _LOGGER.error("Harness Docker client termination failed: %s", name)
    if not _remove_container(docker, name, env):
        raise AdviceUnavailable("DeepSeek Harness 容器清理状态待核查")


def _reject_json_constant(_value: str) -> None:
    raise ValueError("Non-standard JSON constant")


def _communicate_bounded(process: subprocess.Popen, payload: bytes, timeout: int) -> bytes:
    """Drain at most 8193 stdout bytes; never spool untrusted output to host disk."""
    if process.stdin is None or process.stdout is None:
        raise AdviceUnavailable("DeepSeek Harness 输出通道无效")
    output = bytearray()
    overflow = threading.Event()
    reader_done = threading.Event()
    errors: list[Exception] = []

    def feed() -> None:
        try:
            process.stdin.write(payload)
            process.stdin.flush()
        except (OSError, ValueError):
            pass  # Docker may exit before accepting stdin; its status is checked below.
        finally:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass

    def drain() -> None:
        try:
            while len(output) <= 8192:
                chunk = process.stdout.read(min(4096, 8193 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > 8192:
                    overflow.set()
                    break
        except (OSError, ValueError) as exc:
            errors.append(exc)
        finally:
            reader_done.set()

    writer = threading.Thread(target=feed, daemon=True)
    reader = threading.Thread(target=drain, daemon=True)
    writer.start()
    reader.start()
    deadline = time.monotonic() + timeout
    while True:
        if overflow.is_set():
            raise AdviceUnavailable("DeepSeek Harness 输出过长")
        if process.poll() is not None and reader_done.is_set():
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        overflow.wait(min(0.1, remaining))
    writer.join(timeout=1)
    if writer.is_alive() or errors:
        raise AdviceUnavailable("DeepSeek Harness 输出通道无效")
    return bytes(output)


def _kill_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       capture_output=True, timeout=10, check=False, env=_docker_env())
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _validated_projection(projection: dict) -> dict:
    try:
        summary = json.dumps(projection, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True, allow_nan=False).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise AdviceUnavailable("病例状态摘要无效") from exc
    if len(summary) > 4096:
        raise AdviceUnavailable("病例状态摘要过长")
    try:
        return _validate(summary)
    except SummaryError as exc:
        raise AdviceUnavailable("病例状态摘要不符合白名单") from exc


def _run_container(envelope: dict, actor_id: str, configuration: tuple,
                   expected_keys: set[str], purpose: str) -> dict:
    """Run the same bounded Docker worker for one validated task envelope."""
    docker, image, _key, _model, base_url = configuration
    if not isinstance(actor_id, str) or not actor_id:
        raise AdviceUnavailable("操作者身份无效")
    payload = json.dumps(envelope,
                         sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    if len(payload) > 8192:
        raise AdviceUnavailable("DeepSeek Harness 输入过长")
    env = _docker_env()
    network_id = _verified_egress_network(docker, env, image, base_url)
    if not _SLOTS.acquire(blocking=False):
        raise AdviceUnavailable(f"{purpose}服务正在处理其他请求，请稍后重试", 429)
    try:
        _charge(actor_id)
        name = f"windcatcher-harness-{uuid.uuid4().hex}"
        try:
            process = subprocess.Popen(
                _docker_command(docker, image, name, network_id), env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                start_new_session=(sys.platform != "win32"),
            )
        except OSError as exc:
            raise AdviceUnavailable("DeepSeek Harness 进程无法启动") from exc
        try:
            stdout = _communicate_bounded(process, payload, TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            _stop_container(process, docker, name, env)
            raise AdviceUnavailable(f"DeepSeek Harness {purpose}超时，请稍后重试") from exc
        except BaseException:
            _stop_container(process, docker, name, env)
            raise
        finally:
            for stream in (getattr(process, "stdin", None), getattr(process, "stdout", None)):
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
        if process.returncode != 0 and not _remove_container(docker, name, env):
            raise AdviceUnavailable("DeepSeek Harness 容器清理状态待核查")
        if process.returncode != 0 or len(stdout) > 8192:
            raise AdviceUnavailable(f"DeepSeek Harness {purpose}未通过校验")
        try:
            wire = json.loads(stdout, object_pairs_hook=_unique_pairs,
                              parse_constant=_reject_json_constant)
            if not isinstance(wire, dict) or set(wire) != expected_keys:
                raise ValueError("Harness worker response envelope invalid")
            return wire
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise AdviceUnavailable(f"DeepSeek Harness {purpose}未通过校验") from exc
    finally:
        _SLOTS.release()


def request_advice(projection: dict, actor_id: str) -> dict:
    """Run one advisory worker after the gateway has authorized the case."""
    configuration = _configuration()
    validated = _validated_projection(projection)
    envelope = {"summary": validated, "api_key": configuration[2],
                "model": configuration[3], "base_url": configuration[4]}
    wire = _run_container(envelope, actor_id, configuration,
                          {"advice", "metering"}, "建议")
    try:
        result = wire["advice"]
        if not isinstance(result, dict):
            raise ValueError("Harness worker advice invalid")
        metering = validate_metering(wire["metering"], configuration[3])
        model_fields = {name: result[name] for name in (
            "next_step", "failure_recap", "skill_draft",
            "human_review_required", "automated_action",
        )}
        expected = validate_model_output(json.dumps(model_fields, ensure_ascii=True), validated)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise AdviceUnavailable("DeepSeek Harness 建议未通过校验") from exc
    if result != expected:
        raise AdviceUnavailable("DeepSeek Harness 建议与当前状态不匹配")
    return {**result, "metering": metering}


def request_action(projection: dict, allowed_actions: list[str], actor_id: str) -> dict:
    """Select, but never execute, one server-allowlisted next action.

    The caller must authorize the case before this call and recheck its state,
    permission, version, and action-specific gates after the model returns.
    """
    if not actions_enabled():
        raise AdviceUnavailable("DeepSeek Harness 动作选择试点尚未启用")
    configuration = _configuration()
    validated = _validated_projection(projection)
    try:
        request = harness_action_selector.validate_request({
            "schema_version": validated["schema_version"],
            "stages": validated["stages"],
            "baseline_ready": validated["baseline_ready"],
            "numeric_ok": validated["numeric_ok"],
            "eligible": validated["eligible"],
            "allowed_actions": allowed_actions,
        })
    except harness_action_selector.ActionError as exc:
        raise AdviceUnavailable("DeepSeek Harness 允许动作无效") from exc
    envelope = {"task": harness_action_selector.TASK,
                "request": request, "api_key": configuration[2],
                "model": configuration[3], "base_url": configuration[4]}
    wire = _run_container(envelope, actor_id, configuration,
                          {"selection", "metering"}, "动作选择")
    try:
        selection = wire["selection"]
        if not isinstance(selection, dict):
            raise ValueError("Harness worker selection invalid")
        metering = validate_metering(wire["metering"], configuration[3])
        expected = harness_action_selector.validate_model_output(
            json.dumps({"action": selection["action"]}, ensure_ascii=True), request,
        )
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise AdviceUnavailable("DeepSeek Harness 动作选择未通过校验") from exc
    if selection != expected:
        raise AdviceUnavailable("DeepSeek Harness 动作选择与当前状态不匹配")
    return {**selection, "case_id": validated["case_id"],
            "state_sha256": validated["state_sha256"], "metering": metering}
