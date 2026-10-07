"""为本机 3D Slicer 准备并打开一份病例复核副本。

网页工作台仍是主流程入口；这里把已落盘的 HU 体数据和算法候选掩膜
转换成 Slicer 能直接打开的 NRRD，避免把原始 DICOM 路径或患者标识传给外部程序。
"""

from __future__ import annotations

import os
import subprocess
import json
from pathlib import Path

import numpy as np

from .cases import case_dir
from .errors import PipelineError
from .volume import load_volume


def _direction_for_sitk(axis_dirs) -> tuple[float, ...]:
    """把 numpy (z,y,x) 轴方向转换成 SimpleITK x/y/z 列方向矩阵。"""
    x_axis, y_axis, z_axis = axis_dirs[2], axis_dirs[1], axis_dirs[0]
    return tuple(float(axis[row]) for row in range(3) for axis in (x_axis, y_axis, z_axis))


def _write_nrrd(case_id: str) -> tuple[Path, Path | None]:
    try:
        import SimpleITK as sitk
    except Exception as exc:  # pragma: no cover - 环境页会先探测依赖
        raise PipelineError("failed", f"无法准备 Slicer 复核文件：SimpleITK 不可用（{exc}）", ["在风见项目虚拟环境中安装 SimpleITK"]) from exc

    root = case_dir(case_id)
    volume = load_volume(case_id)
    review_dir = root / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    geometry = {
        "spacing": (float(volume.spacing[2]), float(volume.spacing[1]), float(volume.spacing[0])),
        "origin": tuple(float(v) for v in volume.origin),
        "direction": _direction_for_sitk(volume.axis_dirs),
    }

    image = sitk.GetImageFromArray(np.asarray(volume.array, dtype=np.float32))
    image.SetSpacing(geometry["spacing"])
    image.SetOrigin(geometry["origin"])
    image.SetDirection(geometry["direction"])
    volume_path = review_dir / "ct_hu.nrrd"
    sitk.WriteImage(image, str(volume_path), useCompression=True)

    mask_path: Path | None = None
    source_mask = root / "segment" / "mask.npy"
    candidate_mask = root / "segment" / "candidate_mask.npy"
    seg_path = root / "segment" / "segmentation.json"
    if candidate_mask.exists() and seg_path.exists():
        try:
            segmentation = json.loads(seg_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            segmentation = {}
        if segmentation.get("candidate_mask_available") is True:
            source_mask = candidate_mask
    if source_mask.exists():
        mask = np.load(source_mask, mmap_mode="r")
        label = sitk.GetImageFromArray(np.asarray(mask, dtype=np.uint8))
        label.SetSpacing(geometry["spacing"])
        label.SetOrigin(geometry["origin"])
        label.SetDirection(geometry["direction"])
        mask_path = review_dir / "airway_candidate_mask.nrrd"
        sitk.WriteImage(label, str(mask_path), useCompression=True)
    return volume_path, mask_path


def _write_startup_script(review_dir: Path, volume_path: Path, mask_path: Path | None) -> Path:
    """写入一个只负责初始化界面的 Slicer 脚本。

    Slicer 的命令行位置参数在不同版本中不一定会自动加载 NRRD，导致用户看到
    英文 Welcome 页面和空视窗。显式调用 loadVolume/loadLabelVolume 后，再切到
    四视图，用户打开高级工具时至少能看到当前病例和中文复核说明。
    """
    volume_literal = json.dumps(str(volume_path))
    mask_literal = json.dumps(str(mask_path)) if mask_path else "None"
    corrected_mask_literal = json.dumps(str(review_dir / "airway_corrected_mask.nrrd"))
    script = f'''# -*- coding: utf-8 -*-
import slicer
import qt

CT_PATH = {volume_literal}
MASK_PATH = {mask_literal}
CORRECTED_MASK_PATH = {corrected_mask_literal}

def _configure_windcatcher_review():
    ct = slicer.util.loadVolume(CT_PATH, {{"name": "风见 CT"}})
    label = None
    segmentation_node = None
    if MASK_PATH:
        label = slicer.util.loadLabelVolume(MASK_PATH, {{"name": "气道候选掩膜"}})
    if ct:
        slicer.util.setSliceViewerLayers(background=ct, label=label)
    if ct and label:
        try:
            segmentation_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "气道修正掩膜")
            segmentation_node.CreateDefaultDisplayNodes()
            segmentation_node.SetReferenceImageGeometryParameterFromVolumeNode(ct)
            slicer.modules.segmentations.logic().ImportLabelmapToSegmentationNode(label, segmentation_node)
            label.SetDisplayVisibility(False)
        except Exception:
            segmentation_node = None
    try:
        slicer.app.layoutManager().setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
    except Exception:
        pass
    try:
        slicer.util.selectModule("SegmentEditor")
    except Exception:
        pass
    if segmentation_node:
        try:
            editor_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentEditorNode", "风见复核编辑器")
            editor = slicer.modules.segmenteditor.widgetRepresentation().self().editor
            editor.setMRMLScene(slicer.mrmlScene)
            editor.setMRMLSegmentEditorNode(editor_node)
            editor.setSegmentationNode(segmentation_node)
            editor.setMasterVolumeNode(ct)
        except Exception:
            pass
    try:
        main = slicer.util.mainWindow()
        main.setWindowTitle("风见 · CT 复核（高级工具）")
        main.statusBar().showMessage("风见复核：拖动三视图滑块逐层查看；青色只应出现在鼻腔内。不要直接使用英文菜单完成流程。")
        if not main.findChild(qt.QDockWidget, "WindcatcherReviewGuide"):
            dock = qt.QDockWidget("风见复核说明", main)
            dock.setObjectName("WindcatcherReviewGuide")
            dock.setAllowedAreas(qt.Qt.RightDockWidgetArea | qt.Qt.LeftDockWidgetArea)
            guide = qt.QTextBrowser()
            guide.setOpenExternalLinks(False)
            guide.setHtml("""
              <div style='font-family:Microsoft YaHei, sans-serif; font-size:13px; line-height:1.6'>
                <b>当前已加载</b><br>
                灰度：CT 原始图像<br>
                彩色：风见生成的气道候选掩膜<br><br>
                <b>只需检查三件事</b><br>
                1. 拖动三视图滑块，确认鼻孔到鼻咽都连续。<br>
                2. 青色区域只能落在鼻腔内，不能铺到身体外或贴住图像边缘。<br>
                3. 发现异常时，在 Segment Editor 中修正“气道候选掩膜”。<br>
                4. 点击下方“导出修正掩膜到风见”，再回到网页读取。<br><br>
                这是高级复核工具，常规查看请使用风见中文页面。
              </div>
            """)
            container = qt.QWidget()
            layout = qt.QVBoxLayout(container)
            layout.setContentsMargins(8, 8, 8, 8)
            layout.addWidget(guide)
            export_button = qt.QPushButton("导出修正掩膜到风见")
            export_button.setToolTip("先在 Segment Editor 中编辑“气道候选掩膜”，再点击此按钮")
            def _export_mask():
                if not label:
                    main.statusBar().showMessage("当前没有可导出的气道标签")
                    return
                try:
                    export_node = label
                    if segmentation_node:
                        export_node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode", "风见修正掩膜导出")
                        slicer.modules.segmentations.logic().ExportAllSegmentsToLabelmapNode(segmentation_node, export_node, ct)
                    saved = slicer.util.saveNode(export_node, CORRECTED_MASK_PATH)
                    export_button.setText("已导出修正掩膜")
                    main.statusBar().showMessage("已导出修正掩膜；请回到风见网页点击“读取修正掩膜”")
                    if not saved:
                        export_button.setText("导出失败，请重试")
                except Exception as exc:
                    main.statusBar().showMessage("导出失败：" + str(exc))
            export_button.connect("clicked()", _export_mask)
            layout.addWidget(export_button)
            dock.setWidget(container)
            main.addDockWidget(qt.Qt.RightDockWidgetArea, dock)
    except Exception:
        pass

qt.QTimer.singleShot(300, _configure_windcatcher_review)
'''
    startup_path = review_dir / "windcatcher_review_startup.py"
    startup_path.write_text(script, encoding="utf-8")
    return startup_path


def open_case(case_id: str, slicer_path: str | Path) -> dict:
    """生成复核副本并启动 Slicer；不阻塞当前服务。"""
    path = Path(slicer_path)
    if not path.is_file():
        raise PipelineError("failed", f"找不到 3D Slicer：{path}", ["到环境页重新检测 3D Slicer 安装路径"])
    volume_path, mask_path = _write_nrrd(case_id)
    startup_path = _write_startup_script(volume_path.parent, volume_path, mask_path)
    creation_flags = 0
    if os.name == "nt":
        creation_flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        subprocess.Popen(
            [str(path), "--no-splash", "--python-script", str(startup_path)],
            cwd=str(path.parent),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
    except OSError as exc:
        raise PipelineError("failed", f"无法启动 3D Slicer：{exc}", ["确认 Slicer.exe 可以手动启动", "也可以继续使用网页内 CT 复核视窗"]) from exc
    return {
        "status": "started",
        "message": "已打开高级 CT 复核工具；修正后请导出掩膜，再回到风见网页读取",
        "files": [str(p.relative_to(case_dir(case_id))) for p in (volume_path, mask_path, startup_path) if p],
    }
