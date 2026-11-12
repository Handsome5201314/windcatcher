"""Worker boundary rejects sensitive inputs and forged model provenance."""

import copy
import json
import subprocess
import sys

import pytest

from pipeline import harness_advice
from scripts import harness_action_selector
from scripts.harness_sdk_worker import extract_metering, validate_model_output


def projection(**overrides):
    value = {
        "schema_version": 1, "case_id": "synthetic-harness-gateway",
        "state_sha256": "a" * 64,
        "stages": {name: "ok" for name in (
            "import", "anonymize", "qc", "segment", "surface", "cfd_prep", "cfd_run", "report",
        )},
        "baseline_ready": True, "numeric_ok": True, "eligible": True,
    }
    value.update(overrides)
    return value


def output(value):
    model = json.dumps({
        "next_step": {"code": "review_report", "stage": "report"},
        "failure_recap": [],
        "skill_draft": {"trigger_condition": "manual", "suggested_steps": ["report"],
                        "allowed_tools": []},
        "human_review_required": True, "automated_action": False,
    })
    return validate_model_output(model, value)


def worker_output(value):
    events = [
        {"type": "turn/start", "data": {"turn": 1}},
        {"type": "step/start", "data": {"turn": 1, "step": 1}},
        {"type": "assistant/message", "data": {"turn": 1, "step": 1, "usage": {
            "inputTokens": 12, "outputTokens": 8, "totalTokens": 20,
        }}},
        {"type": "step/end", "data": {"turn": 1, "step": 1}},
        {"type": "turn/end", "data": {"turn": 1}},
    ]
    return {"advice": output(value),
            "metering": extract_metering(events, "gpt-6.1-sol")}


def action_worker_output(request, action="wait_for_review"):
    return {
        "selection": harness_action_selector.validate_model_output(
            json.dumps({"action": action}), request,
        ),
        "metering": worker_output(projection())["metering"],
    }


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("WINDCATCHER_HARNESS_ADVICE", "1")
    monkeypatch.delenv("WINDCATCHER_HARNESS_EGRESS_NETWORK_ID", raising=False)
    monkeypatch.delenv("WINDCATCHER_HARNESS_EGRESS_PROXY_IMAGE", raising=False)
    monkeypatch.delenv("WINDCATCHER_HARNESS_BASE_URL", raising=False)
    monkeypatch.setenv("WINDCATCHER_HARNESS_BASE_URL",
                       "https://tongyimohe.aitoken.credit/v1")
    monkeypatch.setenv("WINDCATCHER_HARNESS_MODEL", "gpt-6.1-sol")
    monkeypatch.setenv("WINDCATCHER_HARNESS_IMAGE", "sha256:" + "a" * 64)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("WINDCATCHER_PUBLIC_PASSWORD", "PRIVATE_PUBLIC_SECRET")
    monkeypatch.setattr(harness_advice.shutil, "which", lambda _name: "docker")
    monkeypatch.setattr(harness_advice, "_communicate_bounded",
                        lambda process, payload, timeout: process.communicate(payload, timeout))
    harness_advice._CALLS.clear()
    return monkeypatch


def test_gateway_passes_only_whitelisted_summary_and_scoped_environment(configured):
    value = projection()
    captured = {}

    class Worker:
        returncode = 0

        def __init__(self, args, **kwargs):
            captured["args"] = args
            captured["env"] = kwargs["env"]

        def communicate(self, payload, timeout):
            captured["payload"] = json.loads(payload)
            return json.dumps(worker_output(value)).encode()

    configured.setattr(harness_advice.subprocess, "Popen", Worker)
    result = harness_advice.request_advice(value, "actor-one")
    assert result["source"] == "deepseek-harness"
    assert result["metering"]["total_tokens"] == 20
    assert result["metering"]["cost_usd"] is None
    assert captured["payload"]["summary"] == value
    assert captured["payload"]["api_key"] == "synthetic-test-key"
    assert captured["payload"]["base_url"] == "https://tongyimohe.aitoken.credit/v1"
    assert "WINDCATCHER_PUBLIC_PASSWORD" not in captured["env"]
    assert "DEEPSEEK_API_KEY" not in captured["env"]
    assert not any("synthetic-test-key" in arg for arg in captured["args"])
    assert captured["args"][-1] == "sha256:" + "a" * 64
    assert captured["args"][1:3] == ["--host", harness_advice.DOCKER_HOST]
    for guard in ("--network=none", "--read-only", "--user=10001:10001",
                  "--cap-drop=ALL", "--security-opt=no-new-privileges",
                  "--pids-limit=64", "--memory=512m", "--cpus=1"):
        assert guard in captured["args"]
    assert not any(arg.startswith(("-v", "--volume", "--mount", "--env"))
                   for arg in captured["args"])


def test_action_gateway_sends_only_anonymous_status_and_never_executes(configured):
    configured.setenv("WINDCATCHER_HARNESS_ACTIONS", "1")
    value = projection()
    captured = {}

    class Worker:
        returncode = 0

        def __init__(self, args, **kwargs):
            captured["args"] = args
            captured["env"] = kwargs["env"]

        def communicate(self, payload, timeout):
            captured["payload"] = json.loads(payload)
            return json.dumps(action_worker_output(captured["payload"]["request"])).encode()

    configured.setattr(harness_advice.subprocess, "Popen", Worker)
    result = harness_advice.request_action(value, ["wait_for_review"], "actor-one")
    assert result["status"] == "selected_not_executed"
    assert result["action"] == "wait_for_review"
    assert result["case_id"] == value["case_id"]
    assert result["state_sha256"] == value["state_sha256"]
    assert result["metering"]["total_tokens"] == 20
    assert captured["payload"]["task"] == "select_next_action"
    assert captured["payload"]["request"]["allowed_actions"] == ["wait_for_review"]
    assert "case_id" not in captured["payload"]["request"]
    assert "state_sha256" not in captured["payload"]["request"]
    assert "DEEPSEEK_API_KEY" not in captured["env"]
    assert "WINDCATCHER_PUBLIC_PASSWORD" not in captured["env"]
    assert not any("synthetic-test-key" in arg for arg in captured["args"])


def test_action_gateway_requires_separate_enable_flag(configured):
    configured.delenv("WINDCATCHER_HARNESS_ACTIONS", raising=False)
    configured.setattr(harness_advice.subprocess, "Popen",
                       lambda *_a, **_k: pytest.fail("worker must not start"))
    with pytest.raises(harness_advice.AdviceUnavailable, match="尚未启用"):
        harness_advice.request_action(projection(), ["wait_for_review"], "actor-one")


@pytest.mark.parametrize("allowed", [
    ["confirm_mask"], ["submit_ready_cfd"], [],
])
def test_action_gateway_rejects_unapproved_allowlist_before_model(configured, allowed):
    configured.setenv("WINDCATCHER_HARNESS_ACTIONS", "1")
    configured.setattr(harness_advice.subprocess, "Popen",
                       lambda *_a, **_k: pytest.fail("worker must not start"))
    with pytest.raises(harness_advice.AdviceUnavailable, match="允许动作无效"):
        harness_advice.request_action(projection(baseline_ready=False,
                                                 numeric_ok=None, eligible=None), allowed,
                                      "actor-one")


@pytest.mark.parametrize("mutate", [
    lambda selection: selection.update(action="submit_ready_cfd"),
    lambda selection: selection.update(source="forged-source"),
    lambda selection: selection.update(input_sha256="0" * 64),
    lambda selection: selection.update(approved=True),
])
def test_action_gateway_rejects_forged_selection(configured, mutate):
    configured.setenv("WINDCATCHER_HARNESS_ACTIONS", "1")

    class Worker:
        returncode = 0

        def __init__(self, *_args, **_kwargs):
            pass

        def communicate(self, payload, timeout):
            wire = action_worker_output(json.loads(payload)["request"])
            mutate(wire["selection"])
            return json.dumps(wire).encode()

    configured.setattr(harness_advice.subprocess, "Popen", Worker)
    with pytest.raises(harness_advice.AdviceUnavailable):
        harness_advice.request_action(projection(), ["wait_for_review"], "actor-one")


def test_gateway_passes_only_approved_optional_relay_url(configured):
    configured.setenv("WINDCATCHER_HARNESS_BASE_URL",
                      "https://tongyimohe.aitoken.credit/v1")
    captured = {}

    class Worker:
        returncode = 0

        def __init__(self, _args, **_kwargs):
            pass

        def communicate(self, payload, timeout):
            captured.update(json.loads(payload))
            return json.dumps(worker_output(captured["summary"])).encode()

    configured.setattr(harness_advice.subprocess, "Popen", Worker)
    assert harness_advice.request_advice(projection(), "actor-one")["source"] == "deepseek-harness"
    assert captured["base_url"] == "https://tongyimohe.aitoken.credit/v1"

    configured.setenv("WINDCATCHER_HARNESS_BASE_URL", "https://unapproved.example")
    configured.setattr(harness_advice.subprocess, "Popen",
                       lambda *_a, **_k: pytest.fail("worker must not start"))
    with pytest.raises(harness_advice.AdviceUnavailable, match="隔离运行依赖"):
        harness_advice.request_advice(projection(), "actor-two")


def test_sensitive_field_is_rejected_before_worker_starts(configured):
    configured.setattr(harness_advice.subprocess, "Popen",
                       lambda *_a, **_k: pytest.fail("worker must not start"))
    with pytest.raises(harness_advice.AdviceUnavailable, match="白名单"):
        harness_advice.request_advice(projection(patient_name="PRIVATE_PATIENT_NAME"), "actor-one")


def test_forged_source_cannot_be_returned_as_deepseek_advice(configured):
    value = projection()
    forged = worker_output(value)
    forged["advice"]["source"] = "windcatcher-rules"

    class Worker:
        returncode = 0

        def __init__(self, *_args, **_kwargs):
            pass

        def communicate(self, _payload, timeout):
            return json.dumps(forged).encode()

    configured.setattr(harness_advice.subprocess, "Popen", Worker)
    with pytest.raises(harness_advice.AdviceUnavailable, match="不匹配"):
        harness_advice.request_advice(value, "actor-one")


def test_duplicate_output_key_is_rejected_at_container_boundary(configured):
    value = projection()

    class Worker:
        returncode = 0

        def __init__(self, *_args, **_kwargs):
            pass

        def communicate(self, _payload, timeout):
            raw = json.dumps(worker_output(value))
            raw = raw.replace('"source": "deepseek-harness"',
                              '"source": "deepseek-harness", '
                              '"source": "deepseek-harness"')
            return raw.encode()

    configured.setattr(harness_advice.subprocess, "Popen", Worker)
    with pytest.raises(harness_advice.AdviceUnavailable, match="未通过校验"):
        harness_advice.request_advice(value, "actor-one")


@pytest.mark.parametrize("change", [
    {"cost_usd": 0},
    {"model": "other-model"},
    {"total_tokens": -1},
    {"status": "unavailable"},
    {"reasoning_tokens": 9},
    {"extra": "PRIVATE_FREE_TEXT"},
])
def test_forged_or_inconsistent_metering_is_rejected(configured, change):
    value = projection()
    forged = worker_output(value)
    forged["metering"].update(change)

    class Worker:
        returncode = 0

        def __init__(self, *_args, **_kwargs):
            pass

        def communicate(self, _payload, timeout):
            return json.dumps(forged).encode()

    configured.setattr(harness_advice.subprocess, "Popen", Worker)
    with pytest.raises(harness_advice.AdviceUnavailable, match="未通过校验"):
        harness_advice.request_advice(value, "actor-one")


def test_per_account_hourly_cap_prevents_repeat_external_calls(configured):
    class Worker:
        returncode = 0

        def __init__(self, *_args, **_kwargs):
            pass

        def communicate(self, payload, timeout):
            return json.dumps(worker_output(json.loads(payload)["summary"])).encode()

    configured.setattr(harness_advice.subprocess, "Popen", Worker)
    for _ in range(harness_advice.MAX_PER_HOUR):
        harness_advice.request_advice(projection(), "actor-one")
    with pytest.raises(harness_advice.AdviceUnavailable) as caught:
        harness_advice.request_advice(projection(), "actor-one")
    assert caught.value.status == 429


def test_missing_isolated_image_or_key_never_starts_host_worker(configured):
    configured.setattr(harness_advice.subprocess, "Popen",
                       lambda *_a, **_k: pytest.fail("worker must not start"))
    configured.delenv("WINDCATCHER_HARNESS_IMAGE")
    with pytest.raises(harness_advice.AdviceUnavailable, match="隔离运行依赖"):
        harness_advice.request_advice(projection(), "actor-one")
    configured.setenv("WINDCATCHER_HARNESS_IMAGE", "sha256:" + "a" * 64)
    configured.delenv("DEEPSEEK_API_KEY")
    with pytest.raises(harness_advice.AdviceUnavailable, match="隔离运行依赖"):
        harness_advice.request_advice(projection(), "actor-one")


def test_egress_network_rejects_invalid_id_before_docker_run(configured):
    configured.setenv("WINDCATCHER_HARNESS_EGRESS_NETWORK_ID", "bridge")
    configured.setenv("WINDCATCHER_HARNESS_EGRESS_PROXY_IMAGE", "sha256:" + "d" * 64)
    configured.setattr(harness_advice.subprocess, "Popen",
                       lambda *_a, **_k: pytest.fail("worker must not start"))
    with pytest.raises(harness_advice.AdviceUnavailable, match="网络配置无效"):
        harness_advice.request_advice(projection(), "actor-one")
    configured.setenv("WINDCATCHER_HARNESS_EGRESS_NETWORK_ID", "b" * 64)
    configured.delenv("WINDCATCHER_HARNESS_EGRESS_PROXY_IMAGE")
    with pytest.raises(harness_advice.AdviceUnavailable, match="网络配置无效"):
        harness_advice.request_advice(projection(), "actor-one")


def test_egress_network_requires_isolated_topology_and_labeled_proxy(configured):
    network_id, proxy_id = "b" * 64, "c" * 64
    outbound_id = "e" * 64
    instance = "1234abcdef00" + "0" * 20
    internal_name = f"windcatcher-harness-internal-{instance[:12]}"
    outbound_name = f"windcatcher-harness-outbound-{instance[:12]}"
    proxy_image = "sha256:" + "d" * 64
    configured.setenv("WINDCATCHER_HARNESS_EGRESS_NETWORK_ID", network_id)
    configured.setenv("WINDCATCHER_HARNESS_EGRESS_PROXY_IMAGE", proxy_image)
    network = {
        "Id": network_id, "Name": internal_name, "Driver": "bridge", "Internal": True,
        "EnableIPv6": False, "Labels": {"org.windcatcher.harness.instance": instance},
        "Options": {"com.docker.network.bridge.gateway_mode_ipv4": "isolated"},
        "Containers": {proxy_id: {"Name": "windcatcher-harness-proxy-1234abcdef00"}},
    }
    proxy = {
        "Id": proxy_id, "Name": "/windcatcher-harness-proxy-1234abcdef00",
        "Path": "node", "Args": ["/opt/windcatcher/proxy.js"],
        "Image": proxy_image, "State": {"Running": True},
        "Config": {"Labels": {"windcatcher.role": "harness-egress-proxy",
                              "org.windcatcher.harness.instance": instance},
                    "User": "10001:10001",
                    "Env": ["WINDCATCHER_HARNESS_TARGET_HOST=tongyimohe.aitoken.credit"]},
        "HostConfig": {"PortBindings": {}, "ReadonlyRootfs": True,
                       "CapDrop": ["ALL"]}, "Mounts": [],
        "NetworkSettings": {"Networks": {internal_name: {
            "NetworkID": network_id, "Aliases": ["windcatcher-harness-proxy"],
        }, outbound_name: {"NetworkID": outbound_id, "Aliases": []}}},
    }
    outbound = {
        "Id": outbound_id, "Name": outbound_name, "Driver": "bridge",
        "Internal": False, "EnableIPv6": False,
        "Labels": {"org.windcatcher.harness.instance": instance},
        "Containers": {proxy_id: {"Name": "windcatcher-harness-proxy-1234abcdef00"}},
    }
    worker_id = "f" * 64
    worker_name = "windcatcher-harness-" + "1" * 32
    active_worker = {
        "Id": worker_id, "Name": "/" + worker_name,
        "Path": "/opt/harness-venv/bin/python",
        "Args": ["-s", "/opt/windcatcher/scripts/harness_container_worker.py"],
        "Image": "sha256:" + "a" * 64, "State": {"Running": True},
        "Config": {"Labels": {"windcatcher.role": "harness-worker"},
                   "User": "10001:10001"},
        "HostConfig": {"PortBindings": {}, "ReadonlyRootfs": True,
                       "CapDrop": ["ALL"]}, "Mounts": [],
        "NetworkSettings": {"Networks": {internal_name: {"NetworkID": network_id}}},
    }
    captured = {}

    def inspect(args, **_kwargs):
        captured.setdefault("inspects", []).append(args)
        value = ({network_id: network, outbound_id: outbound}[args[5]]
                 if args[3] == "network" else
                 {proxy_id: proxy, worker_id: active_worker}[args[5]])
        return subprocess.CompletedProcess(args, 0, json.dumps(value).encode(), b"")

    class Worker:
        returncode = 0

        def __init__(self, args, **_kwargs):
            captured["run"] = args

        def communicate(self, payload, timeout):
            return json.dumps(worker_output(json.loads(payload)["summary"])).encode()

    configured.setattr(harness_advice.subprocess, "run", inspect)
    configured.setattr(harness_advice.subprocess, "Popen", Worker)
    result = harness_advice.request_advice(projection(), "actor-one")
    assert result["source"] == "deepseek-harness"
    assert captured["run"][captured["run"].index("--network") + 1] == network_id
    assert "HTTPS_PROXY=http://windcatcher-harness-proxy:3128" in captured["run"]
    assert "HTTP_PROXY=http://windcatcher-harness-proxy:3128" in captured["run"]
    assert len(captured["inspects"]) == 3

    good_network, good_proxy, good_outbound = (
        copy.deepcopy(network), copy.deepcopy(proxy), copy.deepcopy(outbound))
    network["Containers"][worker_id] = {"Name": worker_name}
    captured.clear()
    assert harness_advice.request_advice(projection(), "actor-two")["source"] == "deepseek-harness"
    assert len(captured["inspects"]) == 4
    active_worker["Path"] = "sh"
    captured.clear()
    with pytest.raises(harness_advice.AdviceUnavailable, match="受控出站未就绪"):
        harness_advice.request_advice(projection(), "actor-two")
    assert "run" not in captured
    active_worker["Path"] = "/opt/harness-venv/bin/python"

    for mutate in (
        lambda: network.update(Internal=False),
        lambda: network.update(EnableIPv6=True),
        lambda: network["Options"].update(
            {"com.docker.network.bridge.gateway_mode_ipv4": "nat"}),
        lambda: proxy["State"].update(Running=False),
        lambda: proxy.update(Image="sha256:" + "e" * 64),
        lambda: proxy["Config"]["Labels"].clear(),
        lambda: proxy["Config"].update(
            Env=["WINDCATCHER_HARNESS_TARGET_HOST=api.deepseek.com"]),
        lambda: proxy["Config"].update(
            Env=["WINDCATCHER_HARNESS_TARGET_HOST=tongyimohe.aitoken.credit"] * 2),
        lambda: proxy["HostConfig"].update(ReadonlyRootfs=False),
        lambda: proxy["NetworkSettings"]["Networks"][internal_name].update(Aliases=[]),
        lambda: proxy["HostConfig"].update(PortBindings={"3128/tcp": [{"HostPort": "3128"}]}),
        lambda: network["Containers"].update({"f" * 64: {"Name": "unrelated"}}),
        lambda: proxy["NetworkSettings"]["Networks"].update(
            {"third": {"NetworkID": "f" * 64, "Aliases": []}}),
        lambda: proxy.update(Path="sh"),
        lambda: outbound["Containers"].update({"f" * 64: {"Name": "unrelated"}}),
    ):
        network.clear(); network.update(copy.deepcopy(good_network))
        proxy.clear(); proxy.update(copy.deepcopy(good_proxy))
        outbound.clear(); outbound.update(copy.deepcopy(good_outbound))
        mutate()
        captured.clear()
        with pytest.raises(harness_advice.AdviceUnavailable, match="受控出站未就绪"):
            harness_advice.request_advice(projection(), "actor-one")
        assert "run" not in captured

    network.clear(); network.update(copy.deepcopy(good_network))
    proxy.clear(); proxy.update(copy.deepcopy(good_proxy))
    outbound.clear(); outbound.update(copy.deepcopy(good_outbound))
    proxy["Config"]["Env"] = [
        "WINDCATCHER_HARNESS_TARGET_HOST=tongyimohe.aitoken.credit"]
    captured.clear()
    assert harness_advice.request_advice(projection(), "actor-three")["source"] == "deepseek-harness"
    assert "run" in captured

    proxy["Config"]["Env"] = ["WINDCATCHER_HARNESS_TARGET_HOST=api.deepseek.com"]
    captured.clear()
    with pytest.raises(harness_advice.AdviceUnavailable, match="受控出站未就绪"):
        harness_advice.request_advice(projection(), "actor-three")
    assert "run" not in captured


def test_timeout_forces_exact_container_removal(configured):
    removed = []

    class Worker:
        def __init__(self, args, **_kwargs):
            self.name = args[args.index("--name") + 1]

        def communicate(self, _payload, timeout):
            raise subprocess.TimeoutExpired("docker run", timeout)

    configured.setattr(harness_advice.subprocess, "Popen", Worker)
    configured.setattr(harness_advice, "_kill_tree", lambda _worker: None)
    configured.setattr(harness_advice, "_remove_container",
                       lambda _docker, name, _env: removed.append(name) or True)
    with pytest.raises(harness_advice.AdviceUnavailable, match="超时"):
        harness_advice.request_advice(projection(), "actor-one")
    assert len(removed) == 1 and removed[0].startswith("windcatcher-harness-")


def test_cleanup_failure_is_visible(configured):
    class Worker:
        def __init__(self, *_args, **_kwargs):
            pass

        def communicate(self, _payload, timeout):
            raise subprocess.TimeoutExpired("docker run", timeout)

    configured.setattr(harness_advice.subprocess, "Popen", Worker)
    configured.setattr(harness_advice, "_kill_tree", lambda _worker: None)
    configured.setattr(harness_advice, "_remove_container", lambda *_args: False)
    with pytest.raises(harness_advice.AdviceUnavailable, match="清理状态待核查"):
        harness_advice.request_advice(projection(), "actor-one")


def test_failed_remove_requires_exact_absence_confirmation(monkeypatch):
    calls = []
    listing = subprocess.CompletedProcess([], 0, b"exists", b"")

    def run(args, **_kwargs):
        calls.append(args)
        if args[3:5] == ["rm", "-f"]:
            return subprocess.CompletedProcess(args, 1, b"", b"")
        if args[3:5] == ["container", "inspect"]:
            return subprocess.CompletedProcess(args, 1, b"", b"")
        assert args[3:6] == ["container", "ls", "-a"]
        assert args[-1] == "name=^/windcatcher-harness-" + "1" * 32 + "$"
        return listing

    monkeypatch.setattr(harness_advice.subprocess, "run", run)
    name = "windcatcher-harness-" + "1" * 32
    assert harness_advice._remove_container("docker", name, {}) is False
    listing = subprocess.CompletedProcess([], 1, b"", b"")
    assert harness_advice._remove_container("docker", name, {}) is False
    listing = subprocess.CompletedProcess([], 0, b"", b"")
    assert harness_advice._remove_container("docker", name, {}) is True
    assert len(calls) == 9


def test_bounded_stdout_stops_host_memory_growth():
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 20000)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    try:
        with pytest.raises(harness_advice.AdviceUnavailable, match="输出过长"):
            harness_advice._communicate_bounded(process, b"{}", 3)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        process.stdin.close()
        process.stdout.close()
