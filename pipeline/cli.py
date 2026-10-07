"""风见命令行入口：每个阶段都能单独跑，失败给出原因与建议。"""

from __future__ import annotations

import argparse
import json
import sys

from . import capabilities
from .anonymize import anonymize_series
from .cases import case_dir, create_case, get_case, list_cases
from .cfd_prep import prepare_case
from .dicom_qc import run_qc, save_qc
from .errors import PipelineError
from .fetch_direct import fetch_direct
from .geom_qc import run_geom_qc
from .openfoam_runner import check_result_gate, detect_backends, run_case
from .report import build_report
from .segmentation import run_segment
from .surface import run_surface
from .volume import load_series, save_volume


def _print(title: str, payload) -> None:
    print(f"\n=== {title} ===")
    if isinstance(payload, (dict, list)):
        print(json.dumps(payload, ensure_ascii=False, indent=2)[:4000])
    else:
        print(payload)


def _stage_guard(case_id: str, stage: str) -> None:
    case = get_case(case_id)
    state = case["stages"][stage]
    if state["status"] in ("failed", "blocked"):
        raise PipelineError("blocked", f"阶段 {stage} 当前为 {state['status']}：{state['message']}",
                            ["修复该阶段后重跑"], {"stage": stage})


def cmd_capabilities(_args) -> int:
    _print("本机能力", capabilities.probe_capabilities())
    _print("求解后端", detect_backends())
    return 0


def cmd_new_case(args) -> int:
    case_id = create_case("synthetic" if args.synthetic else "local_path", args.path, synthetic=args.synthetic)
    print(f"已创建病例：{case_id}")
    print(f"病例目录：{case_dir(case_id)}")
    return 0


def cmd_list_cases(_args) -> int:
    cases = list_cases()
    if not cases:
        print("暂无病例")
        return 0
    for item in cases:
        print(f"{item['case_id']} | synthetic={item['synthetic']} | {item['stages']}")
    return 0


def cmd_fetch(args) -> int:
    result = fetch_direct(
        args.url,
        args.dest,
        authorized=args.authorized,
        allowed_domains=args.allowed_domains.split(",") if args.allowed_domains else None,
    )
    _print("直链下载", result)
    return 0 if result["status"] == "ok" else 2


def cmd_anonymize(args) -> int:
    result = anonymize_series(args.path, case_dir(args.case_id) / "anonymized", args.case_id)
    _print("去标识", {k: v for k, v in result.items() if k != "geometry_tags_preserved"})
    return 0


def cmd_qc(args) -> int:
    result = run_qc(args.path)
    save_qc(args.case_id, result)
    volume = load_series(args.path)
    save_volume(args.case_id, volume)
    _print("输入质控", {
        "verdict": result["verdict"],
        "blocking": result["blocking"],
        "reasons": result["reasons"],
        "privacy": result["privacy"],
        "anatomical_coverage": result["anatomical_coverage"],
    })
    return 0 if not result["blocking"] else 2


def cmd_segment(args) -> int:
    params = {
        "hu_low": args.hu_low,
        "hu_high": args.hu_high,
        "connectivity": args.connectivity,
    }
    result = run_segment(args.case_id, params)
    _print("分割", {"metrics": result["metrics"], "candidates": result["candidates"][:3],
                    "need_human_confirm": result["need_human_confirm"]})
    return 0


def cmd_surface(args) -> int:
    result = run_surface(args.case_id, {"smooth": args.smooth, "model_scope": args.model_scope})
    _print("表面", result)
    return 0


def cmd_geom_qc(args) -> int:
    result = run_geom_qc(args.case_id)
    _print("几何质检", {"verdict": result["verdict"], "openings_by_end": result["openings_by_end"],
                        "reasons": result["reasons"], "volume_check": result["volume_check"]})
    return 0 if result["verdict"] != "fail" else 2


def cmd_cfd_prep(args) -> int:
    result = prepare_case(args.case_id, {
        "openfoam_flavour": args.flavour,
        "turbulence_model": args.turbulence,
        "total_flow_lpm": args.flow,
    })
    _print("算例生成", result)
    return 0


def cmd_cfd_run(args) -> int:
    result = run_case(args.case_id, backend=args.backend)
    _print("求解", result)
    _print("结果闸门", check_result_gate(args.case_id))
    return 0 if result.get("status") == "ran" else 2


def cmd_report(args) -> int:
    result = build_report(args.case_id)
    print(f"报告已生成：{case_dir(args.case_id) / 'report' / 'report.md'}")
    if not result["cfd_eligible"]:
        print("注意：CFD 结果未通过闸门，报告中 CFD 部分写的是“未生成/未通过”，没有数值")
    if args.print_markdown:
        print()
        print(result["markdown"])
    return 0


def cmd_run_all(args) -> int:
    from .cases import update_stage

    case_id = create_case("synthetic" if args.synthetic else "local_path", args.path, synthetic=args.synthetic)
    print(f"病例：{case_id}")
    source = args.path
    if args.anonymize:
        result = anonymize_series(source, case_dir(case_id) / "anonymized", case_id)
        update_stage(case_id, "anonymize", "ok", message=f"已生成匿名化副本 {result['files_written']} 个文件",
                     artifacts=["anonymized/"], data={"files": result["files_written"]})
        source = str(case_dir(case_id) / "anonymized")
    qc = run_qc(source)
    save_qc(case_id, qc)
    save_volume(case_id, load_series(source))
    update_stage(case_id, "qc", "failed" if qc["blocking"] else ("warn" if qc["verdict"] == "warn" else "ok"),
                 message="；".join(qc["reasons"])[:200], artifacts=["volume/qc.json"],
                 data={"slice_thickness_mm": qc["primary_series"].get("slice_thickness_mm"),
                       "pixel_spacing_mm": qc["primary_series"].get("pixel_spacing_mm"),
                       "files": qc["primary_series"].get("files"),
                       "contains_phi": qc["privacy"]["contains_phi"]})
    print(f"质控：{qc['verdict']}" + ("（阻断，停止后续）" if qc["blocking"] else ""))
    if qc["blocking"]:
        for reason in qc["reasons"]:
            print(f"  - {reason}")
        return 2
    seg = run_segment(case_id)
    print(f"分割：{seg['metrics']['volume_ml']} mL / {seg['metrics']['ap_length_mm']} mm（待人工复核）")
    surf = run_surface(case_id)
    print(f"表面：{surf['faces']} 面")
    geom = run_geom_qc(case_id)
    print(f"几何质检：{geom['verdict']}（开口 前{geom['openings_by_end']['anterior']} 后{geom['openings_by_end']['posterior']}）")
    if geom["verdict"] == "fail":
        for reason in geom["reasons"]:
            print(f"  - {reason}")
        build_report(case_id)
        print("几何质检未通过：不生成 CFD 算例，报告已记录阻塞原因")
        return 2
    meta = prepare_case(case_id)
    print(f"算例：入口面积 {meta['inlet_area_mm2']} mm²，平均速度 {meta['mean_inlet_velocity_mps']:.3f} m/s")
    run_status = run_case(case_id)
    print(f"求解：{run_status.get('status')}")
    if run_status.get("status") == "missing":
        for item in run_status.get("remediation", []):
            print(f"  - {item}")
    report = build_report(case_id)
    print(f"报告：{case_dir(case_id) / 'report' / 'report.md'}")
    print(f"CFD 是否进入指标区：{report['cfd_eligible']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="风见", description="风见（Windcatcher）本地鼻腔气流管线")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("capabilities", help="探测本机能力与求解后端").set_defaults(func=cmd_capabilities)

    p_new = sub.add_parser("new-case", help="新建本地病例")
    p_new.add_argument("--path", required=True)
    p_new.add_argument("--synthetic", action="store_true", help="标记为合成/公开测试数据")
    p_new.set_defaults(func=cmd_new_case)

    sub.add_parser("list-cases", help="列出本机病例").set_defaults(func=cmd_list_cases)

    p_fetch = sub.add_parser("fetch", help="授权直链本机下载")
    p_fetch.add_argument("--url", required=True)
    p_fetch.add_argument("--dest", required=True)
    p_fetch.add_argument("--authorized", action="store_true")
    p_fetch.add_argument("--allowed-domains", default="")
    p_fetch.set_defaults(func=cmd_fetch)

    p_anon = sub.add_parser("anonymize", help="生成本地匿名化副本")
    p_anon.add_argument("case_id")
    p_anon.add_argument("--path", required=True)
    p_anon.set_defaults(func=cmd_anonymize)

    p_qc = sub.add_parser("qc", help="输入质控并缓存体数据")
    p_qc.add_argument("case_id")
    p_qc.add_argument("--path", required=True)
    p_qc.set_defaults(func=cmd_qc)

    p_seg = sub.add_parser("segment", help="阈值 + 连通域分割")
    p_seg.add_argument("case_id")
    p_seg.add_argument("--hu-low", type=float, default=-1024)
    p_seg.add_argument("--hu-high", type=float, default=-300)
    p_seg.add_argument("--connectivity", type=int, default=3)
    p_seg.set_defaults(func=cmd_segment)

    p_surf = sub.add_parser("surface", help="生成气道 STL")
    p_surf.add_argument("case_id")
    p_surf.add_argument("--smooth", action="store_true")
    p_surf.add_argument(
        "--model-scope",
        default="main_airway",
        choices=["main_airway", "full_candidate", "review"],
        help="建模范围：主气道，或保留全部阈值候选供复核",
    )
    p_surf.set_defaults(func=cmd_surface)

    p_geom = sub.add_parser("geom-qc", help="几何质检")
    p_geom.add_argument("case_id")
    p_geom.set_defaults(func=cmd_geom_qc)

    p_prep = sub.add_parser("cfd-prep", help="生成 OpenFOAM 算例")
    p_prep.add_argument("case_id")
    p_prep.add_argument("--flavour", default="org", choices=["org", "esi"])
    p_prep.add_argument("--turbulence", default="laminar", choices=["laminar", "kOmegaSST"])
    p_prep.add_argument("--flow", type=float, default=15.0)
    p_prep.set_defaults(func=cmd_cfd_prep)

    p_run = sub.add_parser("cfd-run", help="尝试求解（无后端即报缺失）")
    p_run.add_argument("case_id")
    p_run.add_argument("--backend", default=None, choices=["native", "wsl", "docker"])
    p_run.set_defaults(func=cmd_cfd_run)

    p_report = sub.add_parser("report", help="生成患者可读报告")
    p_report.add_argument("case_id")
    p_report.add_argument("--print-markdown", action="store_true")
    p_report.set_defaults(func=cmd_report)

    p_all = sub.add_parser("run-all", help="从 CT 目录一路跑到报告")
    p_all.add_argument("--path", required=True)
    p_all.add_argument("--synthetic", action="store_true")
    p_all.add_argument("--anonymize", action="store_true")
    p_all.set_defaults(func=cmd_run_all)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except PipelineError as exc:
        print(f"\n[失败] {exc.reason}", file=sys.stderr)
        for item in exc.remediation:
            print(f"  - {item}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
