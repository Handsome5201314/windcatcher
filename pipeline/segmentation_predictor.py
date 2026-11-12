"""Pinned, local-only CT candidate contract. Never changes a formal mask."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from scipy import ndimage

from .cases import case_dir
from .config import APP_DIR
from .segmentation_learning import digest

MODEL_ID = 'upper_airway_fink_v1'
MODEL_ROOT = APP_DIR / '.local-models' / 'upper-airway-fink-v1'
PYTHON = APP_DIR / '.venv-segmentation' / 'Scripts' / 'python.exe'
ARCHIVE_SHA256 = 'a58b20100db3a99a6e7a630c9655aa4ee9ffaddbda77a8c0649e311f0092a8f1'


class CandidateError(ValueError):
    pass


def airway_labels(labels):
    """The pinned Fink model's class 4 is tissue, never airway."""
    labels = np.asarray(labels)
    if not np.all(np.isfinite(labels)) or not np.all(np.isin(labels, [0, 1, 2, 3, 4])):
        raise CandidateError('模型输出未知解剖标签')
    return np.isin(labels, [1, 2, 3]).astype(np.uint8)


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def image_geometry(volume) -> dict:
    spacing = np.asarray(volume.spacing, dtype=float)
    origin = np.asarray(volume.origin, dtype=float)
    axes = np.asarray(volume.axis_dirs, dtype=float)
    if (spacing.shape != (3,) or origin.shape != (3,) or axes.shape != (3, 3)
            or not all(np.all(np.isfinite(x)) for x in (spacing, origin, axes))
            or np.any(spacing <= 0) or not np.allclose(axes @ axes.T, np.eye(3), atol=1e-4)):
        raise CandidateError('CT 几何无效，拒绝生成模型候选')
    return {'spacing': spacing[::-1].tolist(), 'origin': origin.tolist(),
            'direction': axes[::-1].T.reshape(-1).tolist(), 'size': list(volume.shape[::-1])}


def validate_prediction(mask, volume, geometry) -> np.ndarray:
    expected = image_geometry(volume)
    try:
        same = all(np.asarray(geometry[k]).shape == np.asarray(expected[k]).shape
                   and np.allclose(geometry[k], expected[k], atol=1e-4, rtol=0) for k in expected)
    except (KeyError, TypeError, ValueError) as exc:
        raise CandidateError('候选几何缺失') from exc
    if not same or mask.shape != volume.shape:
        raise CandidateError('候选与原 CT 几何不一致，拒绝错位对照')
    if not np.all(np.isfinite(mask)) or not np.all((mask == 0) | (mask == 1)):
        raise CandidateError('候选包含未知标签或无效数值')
    if not mask.any():
        raise CandidateError('模型返回空分割，不能作为可用候选')
    return mask.astype(np.uint8)


def input_binding(case_id: str) -> dict:
    root = case_dir(case_id)
    paths = ['volume/hu.npy', 'volume/meta.json', 'segment/mask.npy', 'segment/segmentation.json']
    if not all((root / name).is_file() for name in paths):
        raise CandidateError('先完成 CT 导入和基线分割，再运行 AI 候选')
    seg = json.loads((root / paths[-1]).read_text(encoding='utf-8'))
    version = seg.get('mask_version')
    if type(version) is not int or version < 0:
        raise CandidateError('当前掩膜版本无效')
    files = {name: file_hash(root / name) for name in paths}
    signatures = {name: [int((root / name).stat().st_size), int((root / name).stat().st_mtime_ns)] for name in paths}
    return {'mask_version': version, 'source_sha256': digest(files), 'files': files, 'signatures': signatures}


def binding_is_current(case_id, binding, *, full=False):
    root = case_dir(case_id)
    try:
        if full:
            return input_binding(case_id)['source_sha256'] == binding['source_sha256']
        return all((root / name).is_file() and
            [int((root / name).stat().st_size), int((root / name).stat().st_mtime_ns)] == signature
            for name, signature in binding['signatures'].items())
    except (OSError, ValueError, KeyError):
        return False


def model_manifest(model_id=MODEL_ID, *, verify=True):
    if model_id != MODEL_ID:
        raise CandidateError('未知模型，必须使用已登记的本机候选')
    path = MODEL_ROOT / 'manifest.json'
    if not path.is_file():
        raise CandidateError('本机候选权重尚未准备完成')
    manifest = json.loads(path.read_text(encoding='utf-8'))
    if manifest.get('model_id') != MODEL_ID or manifest.get('archive_sha256') != ARCHIVE_SHA256:
        raise CandidateError('模型来源与锁定版本不一致')
    folder = (MODEL_ROOT / manifest['model_folder']).resolve()
    if not folder.is_relative_to(MODEL_ROOT.resolve()):
        raise CandidateError('模型目录无效')
    if verify:
        for name, expected in manifest['files'].items():
            p = (MODEL_ROOT / name).resolve()
            if not p.is_relative_to(MODEL_ROOT.resolve()) or not p.is_file() or file_hash(p) != expected:
                raise CandidateError('模型文件完整性校验失败')
    return manifest, folder


def runtime_ready():
    try:
        marker = json.loads((MODEL_ROOT/'runtime-verified.json').read_text(encoding='utf-8'))
        return (PYTHON.is_file() and marker.get('model_id') == MODEL_ID
            and marker.get('archive_sha256') == ARCHIVE_SHA256
            and marker.get('outcome') in {'synthetic_empty_rejected','synthetic_candidate_generated'})
    except (OSError, ValueError, TypeError):
        return False


def comparison_summary(baseline, candidate, spacing):
    baseline, candidate = np.asarray(baseline), np.asarray(candidate)
    if baseline.ndim != 3 or baseline.shape != candidate.shape:
        raise CandidateError('新旧分割尺寸不同')
    if not all(np.all((m == 0) | (m == 1)) for m in (baseline, candidate)):
        raise CandidateError('对照输入必须是二值掩膜')
    old, new = baseline.astype(bool), candidate.astype(bool)
    added, removed = new & ~old, old & ~new
    changed = added | removed
    cc, count = ndimage.label(new, structure=np.ones((3, 3, 3), dtype=bool))
    del cc
    voxel = float(np.prod(spacing))
    jump = {view: int(np.argmax(np.count_nonzero(changed, axis=tuple(a for a in range(3) if a != axis))))
            for view, axis in {'axial': 0, 'coronal': 1, 'sagittal': 2}.items()}
    return {'added_voxels': int(added.sum()), 'removed_voxels': int(removed.sum()),
        'baseline_voxels': int(old.sum()), 'candidate_voxels': int(new.sum()),
        'baseline_volume_ml': round(float(old.sum()) * voxel / 1000, 3),
        'candidate_volume_ml': round(float(new.sum()) * voxel / 1000, 3),
        'candidate_components': int(count), 'jump_slices': jump,
        'accuracy_status': 'reference_required', 'production_eligible': False,
        'note': '增删差异不代表对错；候选未套用旧体外空气包络清理。'}
