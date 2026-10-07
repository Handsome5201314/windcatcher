"""OpenFOAM 求解后端探测与运行。

铁律：**没有可用求解后端就不产生任何结果**。
本机未安装 OpenFOAM 时返回 missing + 中文安装建议，绝不写出假的场数据或指标。
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from . import preview
from .cfd_validation import (
    inspect_latest_fields, parse_mesh_checks, parse_residuals, pressure_metadata, read_json,
)
from .errors import PipelineError


def configured_image() -> str:
    """Docker OpenFOAM 镜像：环境变量优先，其次项目配置文件 .openfoam_image。"""
    image = os.environ.get("OPENFOAM_IMAGE") or os.environ.get("IMAGE") or ""
    if image:
        return image.strip()
    marker = Path(__file__).resolve().parent.parent / ".openfoam_image"
    if marker.exists():
        value = marker.read_text(encoding="utf-8").strip()
        if value:
            return value
    return ""


def remember_image(image: str) -> None:
    marker = Path(__file__).resolve().parent.parent / ".openfoam_image"
    marker.write_text(image.strip(), encoding="utf-8")


def _run(cmd: list[str], timeout: int) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=False)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except PermissionError as exc:
        return 126, "", str(exc)
    except subprocess.TimeoutExpired:
        return 124, "", "执行超时"
    except OSError as exc:
        return 125, "", str(exc)


def detect_backends() -> dict:
    """探测可用的求解后端：本机 / WSL / Docker。"""
    backends = []

    native = shutil.which("simpleFoam") or shutil.which("blockMesh")
    if native:
        code, out, _ = _run(["simpleFoam", "-help"], timeout=20)
        backends.append(
            {
                "name": "native",
                "status": "available",
                "detail": native,
                "remediation": [],
            }
        )
    else:
        backends.append(
            {
                "name": "native",
                "status": "missing",
                "detail": None,
                "remediation": [
                    "Windows 原生没有官方 OpenFOAM 发行版，推荐在 WSL2（Ubuntu）中安装",
                    "或准备本地 Docker OpenFOAM 镜像（镜像拉取需用户明确同意）",
                ],
            }
        )

    code, out, err = _run(["wsl.exe", "bash", "-lc", "command -v simpleFoam"], timeout=25)
    if code == 0 and out:
        backends.append({"name": "wsl", "status": "available", "detail": out, "remediation": []})
    elif code in (124, 125, 126, 127):
        backends.append(
            {
                "name": "wsl",
                "status": "unknown",
                "detail": None,
                "remediation": [
                    "本机执行 wsl.exe 被环境策略/权限拦截，无法判定是否已安装 OpenFOAM",
                    "请在允许执行 wsl.exe 的环境手动运行 `wsl bash -lc 'command -v simpleFoam'` 确认",
                ],
            }
        )
    else:
        backends.append(
            {
                "name": "wsl",
                "status": "missing",
                "detail": None,
                "remediation": [
                    f"WSL 中未找到 simpleFoam（退出码 {code}）",
                    "安装 WSL2 + Ubuntu 后按 OpenFOAM 官方步骤安装，再重试 cfd-run",
                ],
            }
        )

    docker = shutil.which("docker")
    if not docker:
        backends.append(
            {
                "name": "docker",
                "status": "missing",
                "detail": None,
                "remediation": ["安装并启动 Docker Desktop 后再试"],
            }
        )
    else:
        code, out, err = _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=20)
        if code != 0 or not out:
            backends.append(
                {
                    "name": "docker",
                    "status": "unknown",
                    "detail": None,
                    "remediation": [f"docker 存在但守护进程未响应（退出码 {code}）：{err[:200]}"],
                }
            )
        else:
            # 守护进程在跑 ≠ 有 OpenFOAM：必须存在可用镜像才算后端可用
            image = configured_image()
            if not image:
                backends.append(
                    {
                        "name": "docker",
                        "status": "missing",
                        "detail": f"Docker 守护进程已运行（ServerVersion {out}），但未配置 OpenFOAM 镜像",
                        "remediation": [
                            "设置环境变量 OPENFOAM_IMAGE，或在 windcatcher 目录写入 .openfoam_image 文件（内容为镜像名）",
                            "镜像拉取涉及网络与大体积下载，需用户明确同意，本工具不会自动执行",
                        ],
                    }
                )
            else:
                code_img, out_img, err_img = _run(["docker", "image", "inspect", image], timeout=30)
                if code_img == 0:
                    backends.append(
                        {
                            "name": "docker",
                            "status": "available",
                            "detail": f"镜像 {image}",
                            "remediation": [],
                        }
                    )
                else:
                    backends.append(
                        {
                            "name": "docker",
                            "status": "missing",
                            "detail": f"镜像 {image} 不存在",
                            "remediation": [
                                f"先准备镜像：`docker pull {image}`（需用户同意，本工具不会自动拉取）",
                                "或改用 WSL 中的 OpenFOAM",
                            ],
                        }
                    )

    available = [b for b in backends if b["status"] == "available"]
    return {
        "backends": backends,
        "available": [b["name"] for b in available],
        "remediation": available[0]["remediation"] if available else [
            "本机未检测到任何可用 OpenFOAM 后端：simpleFoam 不在 PATH，WSL 无法探测，Docker 未就绪",
            "推荐顺序：① 安装 WSL2 + Ubuntu + OpenFOAM；② 或启动 Docker 并准备 OpenFOAM 镜像",
            "在求解后端就绪前，报告只会写“未生成 CFD 结果”，不会给出任何流场数值",
        ],
    }


def _read_postprocessing(case_dir: Path, fields: dict | None = None) -> dict:
    """读取 postProcessing 下的流量与压力采样；没有就报缺失，绝不猜数。"""
    base = case_dir / "postProcessing"
    if not base.exists():
        return {"status": "missing", "reason": "没有 postProcessing 目录（求解尚未运行或未输出）"}
    values: dict[str, float] = {}
    times: dict[str, float] = {}
    errors = []
    selected = {}
    for path in sorted(base.rglob("*.dat")):
        if not path.is_file():
            continue
        try:
            lines = [
                line for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
                if line.strip() and not line.startswith("#")
            ]
        except OSError as exc:
            errors.append(f"{path.relative_to(base)}: {exc}")
            continue
        if not lines:
            continue
        # v11 的函数对象目录名（inletFlowRate 等）才是稳定键；文件名可能是 surfaceFieldValue.dat
        key = path.parent.parent.name.lower()
        try:
            row = lines[-1].split()
            sampled_time = float(row[0])
            last_value = float(row[-1])
        except (ValueError, IndexError):
            errors.append(f"{path.relative_to(base)}: 最后数据行无法解析")
            continue
        if not math.isfinite(last_value) or not math.isfinite(sampled_time):
            errors.append(f"{path.relative_to(base)}: 数据包含非有限值")
            continue
        sample_name = next((name for token, name in (
            ("inletflowrate", "inlet_flow_m3s"), ("outletflowrate", "outlet_flow_m3s"),
            ("wallflowrate", "wall_flow_m3s"), ("inletpressure", "inlet_pressure_field"),
            ("outletpressure", "outlet_pressure_field"),
        ) if token in key), None)
        if sample_name and (sample_name not in times or sampled_time > times[sample_name]):
            values[sample_name] = last_value
            times[sample_name] = sampled_time
            selected[sample_name] = str(path.relative_to(case_dir))
    if not values:
        return {"status": "missing", "reason": "postProcessing 里没有可解析的流量/压力数据"}
    fields = fields if fields is not None else inspect_latest_fields(case_dir)
    phi = fields.get("fields", {}).get("phi", {})
    patch_fluxes = phi.get("patch_fluxes_m3s") or {}
    if phi.get("complete") and "wall" in patch_fluxes and "wall_flow_m3s" not in values:
        values["wall_flow_m3s"] = patch_fluxes["wall"]
        selected["wall_flow_m3s"] = f"{fields['latest_time']}/phi:wall"
    p_dimensions = fields.get("fields", {}).get("p", {}).get("dimensions")
    pressure = pressure_metadata(p_dimensions, read_json(case_dir / "case_meta.json"))
    result = {"status": "parsed", **values, "sample_times": times, "sources": selected, "errors": errors, "pressure": pressure}
    expected_time = fields.get("latest_time")
    result["samples_current"] = expected_time is not None and all(time == float(expected_time) for time in times.values())
    for end in ("inlet", "outlet"):
        value = values.get(f"{end}_pressure_field")
        if value is not None:
            if pressure.get("field_unit") == "m2/s2":
                result[f"{end}_pressure_m2s2"] = value
            if pressure.get("pa_multiplier") is not None:
                result[f"{end}_pressure_pa"] = value * pressure["pa_multiplier"]
    inlet = values.get("inlet_flow_m3s")
    outlet = values.get("outlet_flow_m3s")
    if inlet is not None and outlet is not None:
        result["flow_direction_ok"] = inlet < 0 and outlet > 0
        wall = values.get("wall_flow_m3s")
        result["wall_flow_checked"] = wall is not None
        all_patches_checked = bool(phi.get("complete")) and set(patch_fluxes) == set(fields.get("mesh", {}).get("patch_faces", {}))
        result["all_boundary_fluxes_checked"] = all_patches_checked
        result["patch_fluxes_m3s"] = patch_fluxes
        result["net_boundary_flow_m3s"] = sum(patch_fluxes.values()) if all_patches_checked else None
        result["inlet_outlet_imbalance_m3s"] = inlet + outlet
        if abs(inlet) > 1e-12:
            result["mass_conservation_error_percent"] = round(
                abs(inlet + outlet) / abs(inlet) * 100, 6
            )
            if wall is not None:
                result["wall_leakage_percent"] = abs(wall) / abs(inlet) * 100
            if all_patches_checked:
                result["net_mass_conservation_error_percent"] = abs(result["net_boundary_flow_m3s"]) / abs(inlet) * 100
        else:
            result["mass_conservation_error_percent"] = None
            result["mass_conservation_note"] = "入口流量接近 0，无法计算守恒误差"
    return result


def run_case(case_id: str, backend: str | None = None, timeout_s: int = 7200) -> dict:
    """尝试运行求解；没有后端就返回 missing，不写任何结果文件。"""
    from .cases import case_dir, update_stage

    root = case_dir(case_id)
    cfd_dir = root / "cfd"
    seg_path = root / "segment" / "segmentation.json"
    if not seg_path.exists():
        raise PipelineError("blocked", "还没有分割记录，不能开始气流求解", ["先完成气道分割并人工确认"])
    segmentation = json.loads(seg_path.read_text(encoding="utf-8"))
    if segmentation.get("need_human_confirm", True):
        raise PipelineError(
            "blocked",
            "气道掩膜尚未人工确认，不能开始气流求解",
            ["先在 CT 复核视窗确认气道范围", "确认后重新生成 STL、几何质检和 CFD 算例"],
            {"stage": "segment", "human_review": segmentation.get("human_review", {})},
        )
    review = preview.viewer_metadata(case_id)
    if review.get("mask_external_air_suspected"):
        raise PipelineError(
            "blocked",
            "候选掩膜疑似包含体外空气，不能开始气流求解",
            [
                "先在 CT 复核视窗逐层确认并修正气道掩膜",
                "确认后重新执行气道分割、STL 与几何质检、CFD 算例",
            ],
            {"stage": "segment", "review": review},
        )
    if not (cfd_dir / "system" / "controlDict").exists():
        raise PipelineError(
            "blocked", "还没有算例文件", ["先执行 cfd-prep 生成算例"], {"stage": "cfd_prep"}
        )

    detection = detect_backends()
    if not detection["available"]:
        payload = {
            "status": "missing",
            "backend": None,
            "detection": detection,
            "remediation": detection["remediation"],
            "message": "本机未检测到可用的 OpenFOAM 后端，未执行求解，也没有生成任何 CFD 结果",
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        (cfd_dir / "run_status.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        update_stage(
            case_id,
            "cfd_run",
            "blocked",
            message="未检测到 OpenFOAM 后端：未生成 CFD 结果",
            artifacts=["cfd/run_status.json"],
            data={"solved": False, "backend": None},
        )
        return payload

    chosen = backend or detection["available"][0]
    (cfd_dir / "logs").mkdir(parents=True, exist_ok=True)

    if chosen == "docker":
        image = configured_image()
        volume_arg = f"{cfd_dir.resolve()}:/case"
        env_source = ". /opt/openfoam11/etc/bashrc >/dev/null 2>&1; "

        def docker_cmd(inner: str) -> list[str]:
            return ["docker", "run", "--rm", "--entrypoint", "bash", "-v", volume_arg, "-w", "/case", image, "-c", env_source + inner]

        commands = [
            (docker_cmd("blockMesh"), "blockMesh"),
            (docker_cmd("snappyHexMesh -overwrite"), "snappyHexMesh"),
            (docker_cmd("checkMesh"), "checkMesh"),
            (docker_cmd("simpleFoam"), "simpleFoam"),
            (docker_cmd("foamToVTK -ascii"), "foamToVTK"),
        ]
    elif chosen == "wsl":
        commands = [
            (["wsl.exe", "bash", "-lc", f"cd '{cfd_dir.as_posix()}' && blockMesh"], "blockMesh"),
            (["wsl.exe", "bash", "-lc", f"cd '{cfd_dir.as_posix()}' && snappyHexMesh -overwrite"], "snappyHexMesh"),
            (["wsl.exe", "bash", "-lc", f"cd '{cfd_dir.as_posix()}' && checkMesh"], "checkMesh"),
            (["wsl.exe", "bash", "-lc", f"cd '{cfd_dir.as_posix()}' && simpleFoam"], "simpleFoam"),
        ]
    else:  # native
        commands = [
            (["blockMesh"], "blockMesh"),
            (["snappyHexMesh", "-overwrite"], "snappyHexMesh"),
            (["checkMesh"], "checkMesh"),
            (["simpleFoam"], "simpleFoam"),
        ]

    update_stage(
        case_id,
        "cfd_run",
        "running",
        message=f"已选择 {chosen} 后端，准备执行 CFD 求解",
        data={"solved": False, "backend": chosen},
    )
    steps = []
    for cmd, name in commands:
        update_stage(
            case_id,
            "cfd_run",
            "running",
            message=f"正在执行 {name}，请保持页面打开查看进度",
            data={"backend": chosen, "current_step": name, "solved": False},
        )
        code, out, err = _run(cmd, timeout=timeout_s)
        log_path = cfd_dir / "logs" / f"{name}.log"
        log_path.write_text(f"$ {' '.join(cmd)}\n\n{out}\n{err}\n", encoding="utf-8")
        steps.append({"name": name, "returncode": code, "log": f"cfd/logs/{name}.log"})
        if code != 0 and name != "foamToVTK":  # 可视化转换失败不阻断求解状态
            payload = {
                "status": "failed",
                "backend": chosen,
                "failed_step": name,
                "steps": steps,
                "remediation": [
                    f"步骤 {name} 退出码 {code}，请查看 cfd/logs/{name}.log",
                    "常见原因：几何有自交/破洞导致网格失败、边界条件不一致、求解不收敛",
                    "不要降低网格精度后直接把结果标成合格；先定位原因",
                ],
            }
            (cfd_dir / "run_status.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            update_stage(
                case_id,
                "cfd_run",
                "failed",
                message=f"求解失败于 {name}",
                artifacts=["cfd/run_status.json"],
                data={"solved": False, "backend": chosen, "failed_step": name},
            )
            return payload

    log_path = cfd_dir / "logs" / "simpleFoam.log"
    log_text = log_path.read_text(encoding="utf-8", errors="ignore") if log_path.exists() else ""
    residuals = _case_residuals(cfd_dir)
    fields = inspect_latest_fields(cfd_dir)
    post = _read_postprocessing(cfd_dir, fields)

    payload = {
        "status": "ran",
        "backend": chosen,
        "steps": steps,
        "residuals": residuals,
        "postprocessing": post,
        "field_validation": fields,
        "mesh_validation": parse_mesh_checks(cfd_dir),
        "report_eligible": False,
        "validation": {
            "status": "待 CFD 工程师复核",
            "note": "即使求解结束，也需要残差/质量守恒/网格敏感性复核后才能进入患者报告",
        },
    }
    (cfd_dir / "run_status.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    update_stage(
        case_id,
        "cfd_run",
        "warn",
        message=f"求解已在 {chosen} 后端执行完毕，等待 CFD 工程师复核",
        artifacts=["cfd/run_status.json"],
        data={"solved": True, "backend": chosen, "report_eligible": False},
    )
    return payload


def _case_residuals(cfd_dir: Path) -> dict:
    def text(path: Path) -> str:
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    return parse_residuals(
        text(cfd_dir / "logs" / "simpleFoam.log"),
        text(cfd_dir / "system" / "fvSolution"),
        text(cfd_dir / "system" / "controlDict"),
    )


def check_result_gate(case_id: str) -> dict:
    """判断 CFD 结果是否够格进入患者报告。"""
    from .cases import case_dir

    cfd_dir = case_dir(case_id) / "cfd"
    status_path = cfd_dir / "run_status.json"
    checks = []

    if not status_path.exists():
        return {
            "eligible": False, "numeric_ok": False,
            "checks": [{"name": "求解状态文件", "status": "missing", "detail": "cfd/run_status.json 不存在"}],
            "reasons": ["尚未执行求解（或本机没有 OpenFOAM 后端）"],
        }

    try:
        status = read_json(status_path)
    except (OSError, ValueError) as exc:
        return {"eligible": False, "numeric_ok": False, "checks": [{"name": "求解状态文件", "status": "invalid", "detail": str(exc)}], "reasons": ["求解状态文件无法读取"]}
    if status.get("status") != "ran":
        return {
            "eligible": False, "numeric_ok": False,
            "checks": [{"name": "求解状态", "status": status.get("status", "unknown"), "detail": status.get("message", "")}],
            "reasons": ["求解未成功完成"] + status.get("remediation", []),
        }

    # 现场重读日志与 postProcessing（run_status.json 可能是早前写入的快照）
    residuals = _case_residuals(cfd_dir)
    fields = inspect_latest_fields(cfd_dir)
    post = _read_postprocessing(cfd_dir, fields)
    mesh = parse_mesh_checks(cfd_dir)
    residual_ok = residuals.get("status") == "parsed" and residuals.get("converged")
    checks.append(
        {
            "name": "残差收敛",
            "status": "ok" if residual_ok else "pending",
            "detail": {"initial_residual": residuals.get("initial_residual"), "residual_control": residuals.get("residual_control"), "stop_reason": residuals.get("stop_reason"), "reason": residuals.get("reason")},
        }
    )

    mass_error = post.get("mass_conservation_error_percent")
    mass_ok = (
        mass_error is not None and mass_error < 2.0 and post.get("flow_direction_ok")
        and post.get("samples_current") and post.get("all_boundary_fluxes_checked")
        and post.get("net_mass_conservation_error_percent", math.inf) < 2.0
        and post.get("wall_leakage_percent", math.inf) < 0.1 and not post.get("errors")
    )
    checks.append(
        {
            "name": "质量守恒",
            "status": "ok" if mass_ok else "pending",
            "detail": {"inlet_outlet_error_percent": mass_error, "flow_direction_ok": post.get("flow_direction_ok"), "net_boundary_flow_m3s": post.get("net_boundary_flow_m3s"), "wall_leakage_percent": post.get("wall_leakage_percent"), "samples_current": post.get("samples_current"), "errors": post.get("errors")},
        }
    )

    field_ok = fields.get("complete") and residuals.get("last_time") == float(fields["latest_time"])
    checks.append({"name": "最终场完整性", "status": "ok" if field_ok else "pending", "detail": fields})
    pressure_ok = post.get("pressure", {}).get("status") == "checked"
    checks.append({"name": "压力维度与单位", "status": "ok" if pressure_ok else "pending", "detail": post.get("pressure", {})})
    mesh_ok = mesh.get("clean") is True
    checks.append({"name": "网格与封闭壳", "status": "ok" if mesh_ok else "pending", "detail": mesh})

    validation = status.get("validation", {})
    sensitivity = validation.get("mesh_sensitivity", {})
    sensitivity_ok = (
        isinstance(sensitivity, dict) and sensitivity.get("status") == "passed"
        and bool(sensitivity.get("evidence"))
    )

    checks.append(
        {
            "name": "网格敏感性",
            "status": "ok" if sensitivity_ok else "pending",
            "detail": sensitivity if sensitivity else "尚未记录粗/中/细网格敏感性检查证据",
        }
    )
    review = validation.get("engineering_review", {})
    review_ok = (
        isinstance(review, dict) and review.get("status") == "approved"
        and bool(review.get("reviewed_at")) and bool(review.get("reviewer")) and bool(review.get("evidence"))
    )
    checks.append(
        {
            "name": "CFD 工程师复核",
            "status": "ok" if review_ok else "pending",
            "detail": review if review else validation.get("status", "待复核"),
        }
    )

    numeric_ok = bool(residual_ok and mass_ok and field_ok and pressure_ok and mesh_ok)
    eligible = numeric_ok and sensitivity_ok and review_ok
    metrics = {key: post.get(key) for key in (
        "inlet_flow_m3s", "outlet_flow_m3s", "wall_flow_m3s", "net_boundary_flow_m3s",
        "mass_conservation_error_percent", "net_mass_conservation_error_percent", "wall_leakage_percent",
        "inlet_pressure_pa", "outlet_pressure_pa", "inlet_pressure_m2s2", "outlet_pressure_m2s2",
    )}
    for unit in ("pa", "m2s2"):
        inlet = post.get(f"inlet_pressure_{unit}")
        outlet = post.get(f"outlet_pressure_{unit}")
        metrics[f"delta_p_{unit}"] = inlet - outlet if inlet is not None and outlet is not None else None
    metrics["pressure_unit"] = post.get("pressure", {}).get("display_unit")
    return {
        "eligible": eligible, "numeric_ok": numeric_ok,
        "checks": checks,
        "reasons": [f"{check['name']}尚未通过" for check in checks if check["status"] != "ok"],
        "metrics": metrics, "convergence": residuals, "fields": fields, "mesh": mesh, "postprocessing": post,
    }
