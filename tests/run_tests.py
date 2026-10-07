"""风见管线端到端自测（只跑合成 phantom，绝不碰患者数据）。

运行：`.venv/Scripts/python.exe tests/run_tests.py`
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import capabilities, cfd_prep, dicom_qc, geom_qc, mask_review, openfoam_runner, report, segmentation, surface, volume  # noqa: E402
from pipeline.anonymize import anonymize_series  # noqa: E402
from pipeline.cases import case_dir, create_case  # noqa: E402
from pipeline.config import TEST_DATA_DIR  # noqa: E402
from pipeline.errors import PipelineError  # noqa: E402
from pipeline import fetch_direct as fetch_module  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'} | {name}" + (f" | {detail}" if detail else ""))


def step_capabilities() -> None:
    caps = capabilities.probe_capabilities()
    items = {item["name"]: item for group in caps["groups"] for item in group["items"]}
    slicer = next(v for k, v in items.items() if k.startswith("3D Slicer"))
    foam = next(v for k, v in items.items() if k.startswith("OpenFOAM"))
    model = next(v for k, v in items.items() if k.startswith("AI 分割模型"))
    def valid(item: dict) -> bool:
        return item["status"] == "available" or (
            item["status"] in ("missing", "unknown") and bool(item["remediation"])
        )

    ok = all(valid(item) for item in (slicer, foam, model))
    record(
        "能力探测：已安装工具可用，缺失工具给建议",
        ok,
        f"Slicer={slicer['status']} / OpenFOAM={foam['status']} / 分割模型={model['status']}",
    )


def step_phantom() -> Path:
    from tests.make_phantom import build_phantom  # noqa: PLC0415

    if TEST_DATA_DIR.exists() and any(TEST_DATA_DIR.glob("*.dcm")):
        print(f"INFO | 复用已有 phantom：{TEST_DATA_DIR}")
        return TEST_DATA_DIR
    return build_phantom(TEST_DATA_DIR)


def step_qc(phantom_dir: Path, case_id: str) -> dict:
    result = dicom_qc.run_qc(phantom_dir)
    dicom_qc.save_qc(case_id, result)
    ok = (
        result["verdict"] == "pass"
        and result["primary_series"]["slice_thickness_mm"] <= 1.0
        and result["privacy"]["contains_phi"] is True
        and "需人工确认" in result["anatomical_coverage"]["status"]
    )
    record(
        "输入质控：层厚合格 / 隐私标记 / 解剖覆盖需人工确认",
        ok,
        f"层厚 {result['primary_series']['slice_thickness_mm']} mm，"
        f"隐私标记 {len(result['privacy']['identifying_tags_present'])} 项，"
        f"判定 {result['verdict']}",
    )
    return result


def step_anonymize(phantom_dir: Path, case_id: str) -> Path:
    dst = case_dir(case_id) / "anonymized"
    result = anonymize_series(phantom_dir, dst, case_id)
    src_count = len(list(Path(phantom_dir).glob("*.dcm")))
    ok = (
        result["files_written"] == src_count
        and "PatientName" in result["cleared_tags"]
        and result["geometry_tags_preserved"].get("ImagePositionPatient", 0) == src_count
    )
    record(
        "去标识：副本完整 / 标识符清空 / 几何标签保留",
        ok,
        f"{result['files_written']}/{src_count} 文件，清空 {len(result['cleared_tags'])} 类标签",
    )
    return dst


def step_volume(case_id: str, dicom_dir: Path) -> volume.Volume:
    vol = volume.load_series(str(dicom_dir))
    volume.save_volume(case_id, vol)
    ok = -1100.0 < vol.meta["hu_min"] < -900.0
    record(
        "体数据：HU 量纲正确",
        ok,
        f"loader={vol.meta['loader']}，HU 范围 {vol.meta['hu_min']:.0f}~{vol.meta['hu_max']:.0f}，"
        f"spacing={[round(s, 3) for s in vol.spacing]}",
    )
    return vol


def step_segment(case_id: str):
    payload = segmentation.run_segment(case_id)
    metrics = payload["metrics"]
    ok = (
        metrics["volume_ml"] > 0.3
        and metrics["ap_length_mm"] > 30.0
        and metrics["anterior_opening_area_mm2"] > 0
        and metrics["posterior_opening_area_mm2"] > 0
        and payload["components_total"] >= 3
        and payload["need_human_confirm"] is True
    )
    record(
        "分割：单一主气道贯通 + 剔除旁路/外部空气 + 标记需人工复核",
        ok,
        f"体积 {metrics['volume_ml']} mL，A-P 长度 {metrics['ap_length_mm']} mm，"
        f"连通域总数 {payload['components_total']}，"
        f"开口面积 前 {metrics['anterior_opening_area_mm2']} / 后 {metrics['posterior_opening_area_mm2']} mm²",
    )
    return payload


def step_surface(case_id: str) -> dict:
    result = surface.run_surface(case_id)
    stl_path = case_dir(case_id) / "surface" / "airway.stl"
    ok = result["faces"] > 1000 and stl_path.exists() and stl_path.stat().st_size > 0
    record(
        "STL 导出：面数与文件存在",
        ok,
        f"{result['faces']} 面 / {result['vertices']} 顶点，平滑={result['params']['smooth']}",
    )
    return result


def step_confirm_mask(case_id: str) -> None:
    """测试病例也要走人工复核闸门，不能让自测绕过生产安全约束。"""
    # 生产流程要求先由用户点击一次体外空气清理，再进入人工确认。
    mask_review.auto_remove_external_air(case_id)
    result = mask_review.confirm_mask(case_id)
    record(
        "人工复核闸门：确认无体外空气后才允许生成 STL",
        result.get("status") == "confirmed",
        result.get("message", ""),
    )


def step_geom_qc(case_id: str) -> dict:
    result = geom_qc.run_geom_qc(case_id)
    by_end = result["openings_by_end"]
    deviation = result["volume_check"].get("deviation_percent")
    ok = (
        by_end["anterior"] >= 1
        and by_end["posterior"] >= 1
        and by_end["middle"] == 0
        and result["connected_components"] == 1
        and result["verdict"] != "fail"
        and deviation is not None
        and deviation < 15.0
    )
    record(
        "几何质检：开口分组正确 / 体积与掩膜一致",
        ok,
        f"开口 前{by_end['anterior']} 后{by_end['posterior']} 中{by_end['middle']}，"
        f"连通分量 {result['connected_components']}，退化面 {result['degenerate_faces']}，"
        f"体积偏差 {deviation}%，判定 {result['verdict']}",
    )
    return result


def step_failure_path(case_id: str) -> None:
    vol = volume.load_volume(case_id)
    try:
        segmentation.segment_airway(vol, {"hu_low": -1024, "hu_high": -1015})
    except PipelineError as exc:
        ok = bool(exc.reason) and bool(exc.remediation) and exc.status == "failed"
        record("失败路径：分割失败时给出原因与建议", ok, exc.reason[:60])
        return
    record("失败路径：分割失败时给出原因与建议", False, "未抛出异常")


def step_cfd_prep(case_id: str) -> dict:
    meta = cfd_prep.prepare_case(case_id)
    root = case_dir(case_id) / "cfd"
    required = [
        "constant/triSurface/nasal_cfd.stl",
        "system/blockMeshDict",
        "system/snappyHexMeshDict",
        "0/U",
        "0/p",
        "system/controlDict",
        "system/fvSchemes",
        "system/fvSolution",
        "case_meta.json",
        "run_wsl.sh",
        "run_docker.sh",
    ]
    missing = [rel for rel in required if not (root / rel).exists()]
    expected_u = meta["total_flow_m3s"] / meta["inlet_area_m2"]
    stl_text = (root / "constant/triSurface/nasal_cfd.stl").read_text(encoding="ascii", errors="ignore")
    solids = {"wall", "inlet", "outlet"}
    ok = (
        not missing
        and abs(meta["mean_inlet_velocity_mps"] - expected_u) < 1e-9
        and all(f"solid {name}" in stl_text for name in solids)
        and meta["validation"]["status"] == "未验证"
    )
    record(
        "算例生成：文件齐全 / U=Q/A 自洽 / 三个 patch 区域 / 标记未验证",
        ok,
        f"入口面积 {meta['inlet_area_mm2']} mm²，平均速度 {meta['mean_inlet_velocity_mps']:.3f} m/s，"
        f"缺失文件 {missing or '无'}",
    )
    return meta


def step_cfd_run(case_id: str) -> dict:
    status = openfoam_runner.run_case(case_id)
    gate = openfoam_runner.check_result_gate(case_id)
    root = case_dir(case_id) / "cfd"
    result_dirs = [p for p in root.iterdir() if p.is_dir() and p.name.isdigit() and p.name != "0"]
    ok = (
        status["status"] in ("missing", "unknown")
        and bool(status.get("remediation"))
        and not gate["eligible"]
        and not result_dirs
    )
    record(
        "求解闸门：无 OpenFOAM 时只报缺失，不产出结果",
        ok,
        f"status={status['status']}，建议 {len(status.get('remediation', []))} 条，"
        f"结果目录 {len(result_dirs)} 个，eligible={gate['eligible']}",
    )
    return status


def step_report(case_id: str) -> dict:
    built = report.build_report(case_id)
    markdown = built["markdown"]
    forbidden = ("7.32", "1.61", "69/31", "69 %")
    ok = (
        "未生成 CFD 结果" in markdown
        and not built["cfd_eligible"]
        and not any(token in markdown for token in forbidden)
        and "需人工确认" in markdown
    )
    record(
        "报告：CFD 未跑通即写明未生成，且不含参考样本数值",
        ok,
        f"CFD 进入指标区={built['cfd_eligible']}，命中禁用数值={[t for t in forbidden if t in markdown] or '无'}",
    )
    return built


def step_fetch() -> None:
    """用本机临时 HTTP 服务测试直链下载（不访问外网）。"""
    import http.server
    import io
    import threading
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("slice_0001.dcm", b"not-a-real-dicom")
        archive.writestr("slice_0002.dcm", b"not-a-real-dicom")
    zip_bytes = buffer.getvalue()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/phantom.zip"):
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(zip_bytes)))
                self.end_headers()
                self.wfile.write(zip_bytes)
            else:
                self.send_response(401)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<html><body>please login</body></html>")

        def log_message(self, *_args):  # 静音
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            unauthorized = fetch_module.fetch_direct(
                f"http://127.0.0.1:{port}/phantom.zip", base / "a", authorized=False
            )
            ok_zip = fetch_module.fetch_direct(
                f"http://127.0.0.1:{port}/phantom.zip", base / "b", authorized=True
            )
            login = fetch_module.fetch_direct(
                f"http://127.0.0.1:{port}/private", base / "c", authorized=True
            )
            domain = fetch_module.fetch_direct(
                f"http://127.0.0.1:{port}/phantom.zip",
                base / "d",
                authorized=True,
                allowed_domains=["example.org"],
            )
        ok = (
            unauthorized["status"] == "need_authorization"
            and ok_zip["status"] == "ok"
            and ok_zip["files"] == 2
            and login["status"] == "auth_required"
            and domain["status"] == "domain_not_allowed"
        )
        record(
            "直链下载：授权校验 / zip 解包 / 需登录转手动 / 域名白名单",
            ok,
            f"未授权={unauthorized['status']}，zip={ok_zip['status']}({ok_zip.get('files')} 文件)，"
            f"需登录={login['status']}，白名单={domain['status']}",
        )
    finally:
        server.shutdown()
        server.server_close()


def main() -> int:
    print("=" * 72)
    print("风见（Windcatcher）管线自测 —— 仅使用合成 phantom，不涉及任何患者数据")
    print("=" * 72)

    step_capabilities()
    phantom_dir = step_phantom()
    case_id = create_case("local_path", str(phantom_dir), synthetic=True)
    print(f"INFO | 测试病例：{case_id}（synthetic=True）")
    step_qc(phantom_dir, case_id)
    step_qc(phantom_dir, case_id)
    anon_dir = step_anonymize(phantom_dir, case_id)
    step_volume(case_id, anon_dir)
    step_segment(case_id)
    step_confirm_mask(case_id)
    step_surface(case_id)
    step_geom_qc(case_id)
    step_failure_path(case_id)
    step_cfd_prep(case_id)
    step_cfd_run(case_id)
    step_report(case_id)
    step_fetch()

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("-" * 72)
    print(f"汇总：{passed}/{total} 通过")
    if passed != total:
        for name, ok, detail in RESULTS:
            if not ok:
                print(f"  未通过：{name} | {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
