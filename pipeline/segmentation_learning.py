"""Private correction snapshots, bounded candidates, explicit reference review.

Private learning artifacts expire after 180 days or with case deletion. No CT,
arbitrary model code or inferred human labels enter MCP.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from functools import wraps
from pathlib import Path

import numpy as np
from scipy import ndimage

from .cases import case_dir
from .endplane_history import CURRENT_ACTOR_ID
from .segmentation_evaluation import binary_mask, mask_metrics, valid_spacing

SCHEMA_VERSION = 1
RETENTION_DAYS = 180
_LOCKS = defaultdict(threading.RLock)
STRATEGIES = {'identity_v1': 0., 'remove_tiny_islands_v1': 2., 'remove_small_islands_v1': 5.}
REASON_CODES = {'external_residual', 'missing_airway', 'sinus_residual', 'broken_connection', 'boundary_error', 'mixed'}
_SAMPLE = re.compile(r'seg-[0-9a-f]{32}\Z', re.ASCII)
_HASH = re.compile(r'[0-9a-f]{64}\Z', re.ASCII)
_SUBJECT = re.compile(r'subject-[A-Za-z0-9_-]{3,64}\Z', re.ASCII)


class LearningError(ValueError):
    pass


def case_operation(function):
    @wraps(function)
    def locked(case_id, *args, **kwargs):
        with _LOCKS[case_id]:
            return function(case_id, *args, **kwargs)
    return locked


def mask_transaction(function):
    @wraps(function)
    def locked(mask_path, *args, **kwargs):
        with _LOCKS[mask_path.parent.parent.name]:
            return function(mask_path, *args, **kwargs)
    return locked


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode('ascii')


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def _directory(case_id: str) -> Path:
    case_root = case_dir(case_id).resolve()
    root = case_root / 'learning' / 'segmentation'
    if not root.resolve().is_relative_to(case_root):
        raise LearningError('学习目录超出当前病例范围')
    return root


def _expired(record: dict) -> bool:
    try:
        expiry = datetime.fromisoformat(record['created_at']) + timedelta(days=RETENTION_DAYS)
        return expiry <= datetime.fromisoformat(_now())
    except (KeyError, ValueError, TypeError) as exc:
        raise LearningError('学习记录时间无效') from exc


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError('not an object')
        return value
    except (OSError, ValueError) as exc:
        raise LearningError('学习记录不存在或损坏') from exc


def _json_temp(path: Path, value: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + secrets.token_hex(8) + '.tmp')
    try:
        with temp.open('xb') as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return temp


def _write_once(path: Path, value: dict) -> None:
    temp = _json_temp(path, value)
    try:
        # Atomic publication without overwriting another review/run.
        os.link(temp, path)
    except FileExistsError as exc:
        raise LearningError('该记录已经存在，请刷新后查看') from exc
    finally:
        temp.unlink(missing_ok=True)


def mask_hash(mask) -> str:
    mask = binary_mask(mask)
    return hashlib.sha256(canonical(list(mask.shape)) + np.packbits(mask.reshape(-1)).tobytes()).hexdigest()


def _save_blob(root: Path, mask) -> str:
    mask = binary_mask(mask)
    sha = mask_hash(mask)
    path = root / 'blobs' / (sha + '.npz')
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _load_blob(root, sha)
        return sha
    temp = path.with_name(path.name + '.' + secrets.token_hex(8) + '.tmp')
    try:
        with temp.open('xb') as stream:
            np.savez_compressed(stream, shape=np.asarray(mask.shape), bits=np.packbits(mask.reshape(-1)))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            _load_blob(root, sha)
    finally:
        temp.unlink(missing_ok=True)
    return sha


def _load_blob(root: Path, sha: str) -> np.ndarray:
    if not isinstance(sha, str) or not _HASH.fullmatch(sha):
        raise LearningError('掩膜快照 hash 无效')
    try:
        with np.load(root / 'blobs' / (sha + '.npz'), allow_pickle=False) as blob:
            shape, bits = blob['shape'], blob['bits']
            if (shape.shape != (3,) or shape.dtype.kind not in 'iu' or np.any(shape <= 0)
                    or np.prod(shape, dtype=np.float64) > 1_000_000_000
                    or bits.ndim != 1 or bits.dtype != np.uint8
                    or bits.size != (int(np.prod(shape)) + 7) // 8):
                raise LearningError('掩膜快照尺寸无效')
            mask = np.unpackbits(bits, count=int(np.prod(shape))).reshape(tuple(shape)).astype(bool)
    except (OSError, ValueError, KeyError) as exc:
        raise LearningError('掩膜快照损坏或不可读取') from exc
    if mask_hash(mask) != sha:
        raise LearningError('掩膜快照校验失败')
    return mask


def prepare_change(mask_path: Path, mask_temp: Path, seg_path: Path, seg_temp: Path) -> tuple[Path, Path] | None:
    """Stage a manifest to join the EXISTING mask/metadata rollback transaction."""
    if not mask_path.exists():
        return None
    before = binary_mask(np.load(mask_path, allow_pickle=False))
    after = binary_mask(np.load(mask_temp, allow_pickle=False))
    old, new = _json(seg_path), _json(seg_temp)
    if before.shape != after.shape:
        raise LearningError('修正前后掩膜尺寸不一致，无法记录差异')
    added, removed = int(np.count_nonzero(after & ~before)), int(np.count_nonzero(before & ~after))
    if not added and not removed:
        return None
    bv, av = old.get('mask_version', 0), new.get('mask_version')
    if type(bv) is not int or type(av) is not int or av <= bv:
        raise LearningError('修正掩膜版本必须递增')
    case_root = mask_path.parent.parent
    meta = _json(case_root / 'volume/meta.json')
    geometry = {name: meta[name] for name in ('spacing_mm', 'origin_mm', 'axis_dirs')}
    valid_spacing(geometry['spacing_mm'])
    geometry['shape'] = list(before.shape)
    root = _directory(case_root.name)
    params = old.get('params', {})
    safe_params = {key: value for key, value in params.items()
                   if key in {'hu_low', 'hu_high', 'connectivity', 'ap_axis', 'min_component_mm3'}
                   and type(value) in (int, float) and np.isfinite(value)}
    version_hash = hashlib.sha256(
        Path(__file__).read_bytes() + Path(__file__).with_name('mask_review.py').read_bytes()
        + Path(__file__).with_name('segmentation.py').read_bytes()).hexdigest()
    binding = {'schema_version': SCHEMA_VERSION, 'case_id': case_root.name, 'scope': 'main_airway',
        'before_version': bv, 'after_version': av, 'before_hash': _save_blob(root, before),
        'after_hash': _save_blob(root, after), 'geometry': geometry, 'source_params': safe_params,
        'implementation_sha256': version_hash, 'source_record_sha256': hashlib.sha256(seg_path.read_bytes()).hexdigest()}
    sid = 'seg-' + digest(binding)[:32]
    source = new.get('mask_source')
    action = source if source in {'browser_manual_edit', 'browser_external_air_cleanup',
        'slicer_manual_correction', 'browser_mask_reset', 'browser_external_air_undo'} else 'mask_change'
    record = {**binding, 'sample_id': sid, 'created_at': _now(),
        'actor_id': CURRENT_ACTOR_ID.get() or 'local-operator', 'action': action,
        'added_voxels': added, 'removed_voxels': removed}
    record['record_sha256'] = digest(record)
    path = root / 'records' / (sid + '.json')
    if path.exists():
        raise LearningError('此修正记录已经存在，拒绝覆盖')
    return path, _json_temp(path, record)


def _record(case_id: str, sample_id: str, *, allow_expired: bool = False) -> dict:
    if not isinstance(sample_id, str) or not _SAMPLE.fullmatch(sample_id):
        raise LearningError('样本编号无效')
    record = _json(_directory(case_id) / 'records' / (sample_id + '.json'))
    saved_hash = record.get('record_sha256')
    if (record.get('case_id') != case_id or record.get('sample_id') != sample_id
            or saved_hash != digest({k: v for k, v in record.items() if k != 'record_sha256'})):
        raise LearningError('样本记录校验失败')
    if not allow_expired and _expired(record):
        raise LearningError('学习样本已超过 180 天保留期')
    return record


def _review(case_id: str, record: dict) -> dict | None:
    path = _directory(case_id) / 'reviews' / (record['sample_id'] + '.json')
    if not path.exists():
        return None
    review = _json(path)
    if (review.get('record_sha256') != record['record_sha256']
            or review.get('review_sha256') != digest({k: v for k, v in review.items() if k != 'review_sha256'})):
        raise LearningError('参考审核绑定失效')
    return review


@case_operation
def list_changes(case_id: str, limit: int = 50) -> list[dict]:
    paths = sorted((_directory(case_id) / 'records').glob('seg-*.json'), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    result = []
    for path in paths[:max(1, min(int(limit), 200))]:
        record = _record(case_id, path.stem, allow_expired=True)
        if _expired(record):
            continue
        review = _review(case_id, record)
        item = {key: record[key] for key in ('sample_id', 'case_id', 'scope', 'before_version',
            'after_version', 'created_at', 'action', 'added_voxels', 'removed_voxels', 'record_sha256')}
        item['reference_status'] = review['disposition'] if review else 'unreviewed'
        result.append(item)
    return result


@case_operation
def load_pair(case_id: str, sample_id: str) -> tuple[np.ndarray, np.ndarray, dict]:
    record = _record(case_id, sample_id)
    root = _directory(case_id)
    return _load_blob(root, record['before_hash']), _load_blob(root, record['after_hash']), record


@case_operation
def review_reference(case_id: str, sample_id: str, mask_version: int, *, reviewer_id: str,
                     disposition: str, reason_code: str, reference_checked: bool,
                     use_authorized: bool, subject_group: str) -> dict:
    record = _record(case_id, sample_id)
    if (disposition not in {'approved', 'rejected'} or reason_code not in REASON_CODES
            or not isinstance(reviewer_id, str) or not reviewer_id
            or reference_checked is not True or use_authorized is not True
            or not isinstance(subject_group, str) or not _SUBJECT.fullmatch(subject_group)):
        raise LearningError('必须核对修正与使用授权，并填写匿名患者分组和问题类型')
    seg = _json(case_dir(case_id) / 'segment/segmentation.json')
    if (type(mask_version) is not int or seg.get('mask_version') != mask_version
            or record['after_version'] != mask_version):
        raise LearningError('掩膜版本已变化，不能审核旧修正为当前参考')
    current = np.load(case_dir(case_id) / 'segment/mask.npy', allow_pickle=False)
    if mask_hash(current) != record['after_hash']:
        raise LearningError('当前掩膜内容与修正快照不同')
    _before, reference, _record_data = load_pair(case_id, sample_id)
    if disposition == 'approved' and not reference.any():
        raise LearningError('主鼻腔参考掩膜不能为空')
    for path in (_directory(case_id) / 'reviews').glob('seg-*.json'):
        prior = _json(path)
        if prior.get('subject_group') != subject_group:
            raise LearningError('同一病例的参考样本必须使用同一个匿名患者分组')
    review = {'sample_id': sample_id, 'record_sha256': record['record_sha256'],
        'disposition': disposition, 'reason_code': reason_code, 'reviewer_id': reviewer_id,
        'reference_checked': True, 'use_authorized': True, 'subject_group': subject_group,
        'created_at': _now()}
    review['review_sha256'] = digest(review)
    _write_once(_directory(case_id) / 'reviews' / (sample_id + '.json'), review)
    return {'sample_id': sample_id, 'reference_status': disposition, 'committed': False}


def execute_strategy(before, spacing, strategy_id: str) -> np.ndarray:
    """Only the input mask is available here: no corrected reference or case IO."""
    if strategy_id not in STRATEGIES:
        raise LearningError('不支持的分割策略')
    mask = binary_mask(before)
    voxel_mm3 = float(np.prod(valid_spacing(spacing)))
    threshold = STRATEGIES[strategy_id]
    if not threshold:
        return mask.copy()
    labels, count = ndimage.label(mask, ndimage.generate_binary_structure(3, 1))
    sizes = np.bincount(labels.ravel(), minlength=count + 1)
    keep = sizes * voxel_mm3 >= threshold
    keep[0] = False
    return keep[labels]


@case_operation
def run_candidate(case_id: str, sample_id: str, strategy_id: str) -> dict:
    record = _record(case_id, sample_id)
    root = _directory(case_id)
    before = _load_blob(root, record['before_hash'])
    spacing = record['geometry']['spacing_mm']
    # Generate before even loading the reference-review record.
    candidate = execute_strategy(before, spacing, strategy_id)
    candidate_hash = _save_blob(root, candidate)
    source_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    result = {'schema': 'segmentation-candidate-v1', 'sample_id': sample_id, 'case_id': case_id,
        'record_sha256': record['record_sha256'], 'strategy_id': strategy_id,
        'strategy_source_sha256': source_hash, 'candidate_sha256': candidate_hash,
        'changed_voxels': int(np.count_nonzero(candidate != before)),
        'committed': False, 'auto_promotion': False, 'evaluation_status': 'reference_required'}
    review = _review(case_id, record)
    if review and review['disposition'] == 'approved':
        reference = _load_blob(root, record['after_hash'])
        result.update(evaluation_status='evaluated', review_sha256=review['review_sha256'],
            metrics=mask_metrics(candidate, reference, spacing),
            baseline_metrics=mask_metrics(before, reference, spacing),
            evaluator_sha256=hashlib.sha256(Path(__file__).with_name('segmentation_evaluation.py').read_bytes()).hexdigest())
    result['run_id'] = 'run-' + digest(result)[:32]
    path = root / 'runs' / (result['run_id'] + '.json')
    if not path.exists():
        try:
            _write_once(path, result)
        except LearningError:
            if not path.exists() or _json(path) != result:
                raise
    elif _json(path) != result:
        raise LearningError('候选结果 hash 冲突')
    return result


def _comparison_context(case_id: str) -> tuple[dict, Path] | None:
    records = list_changes(case_id)
    if not records:
        return None
    segmentation = _json(case_dir(case_id) / 'segment/segmentation.json')
    item = next((r for r in records if r['after_version'] == segmentation.get('mask_version')), None)
    if item is None:
        return None
    record = _record(case_id, item['sample_id'])
    review = _review(case_id, record)
    binding = {'record_sha256': record['record_sha256'],
        'review_sha256': review['review_sha256'] if review else None,
        'implementation_sha256': hashlib.sha256(Path(__file__).read_bytes() +
            Path(__file__).with_name('segmentation_evaluation.py').read_bytes()).hexdigest()}
    path = _directory(case_id) / 'comparisons' / ('comparison-' + digest(binding) + '.json')
    return record, path


@case_operation
def latest_comparison(case_id: str) -> dict | None:
    context = _comparison_context(case_id)
    if context is None or not context[1].exists():
        return None
    result = _json(context[1])
    if (result.get('sample_id') != context[0]['sample_id'] or result.get('comparison_sha256')
            != digest({k: v for k, v in result.items() if k != 'comparison_sha256'})):
        raise LearningError('分割对照记录校验失败')
    return result


@case_operation
def compare_current_if_pending(case_id: str, *, progress=None) -> dict | None:
    """Harness controller's deterministic step. Repeat only on new input/review/code."""
    context = _comparison_context(case_id)
    if context is None or latest_comparison(case_id) is not None:
        return None
    record, path = context
    current = np.load(case_dir(case_id) / 'segment/mask.npy', allow_pickle=False)
    if mask_hash(current) != record['after_hash']:
        raise LearningError('当前掩膜与学习记录不同，拒绝自动对照')
    if progress is not None:
        progress('compare_segmentation_candidates')
    runs = [run_candidate(case_id, record['sample_id'], strategy) for strategy in STRATEGIES]
    result = {'schema': 'segmentation-comparison-v1', 'case_id': case_id,
        'sample_id': record['sample_id'], 'after_version': record['after_version'],
        'reference_available': all(run['evaluation_status'] == 'evaluated' for run in runs),
        'runs': runs, 'committed': False, 'auto_promotion': False}
    result['comparison_sha256'] = digest(result)
    _write_once(path, result)
    return result


@case_operation
def prune_case(case_id: str) -> dict:
    """Delete only owned, expired learning artifacts, never official case data."""
    root = _directory(case_id)
    records = [_record(case_id, p.stem, allow_expired=True) for p in (root / 'records').glob('seg-*.json')]
    expired = {r['sample_id'] for r in records if _expired(r)}
    alive = {r['sample_id'] for r in records} - expired
    referenced = {r[k] for r in records if r['sample_id'] in alive for k in ('before_hash', 'after_hash')}
    expired_blobs = {r[k] for r in records if r['sample_id'] in expired for k in ('before_hash', 'after_hash')}
    cutoff = (datetime.fromisoformat(_now()) - timedelta(days=RETENTION_DAYS)).timestamp()
    targets = []
    for sid in expired:
        targets.extend([root / 'records' / (sid + '.json'), root / 'reviews' / (sid + '.json')])
    for path in (root / 'runs').glob('run-*.json'):
        run = _json(path)
        if run.get('sample_id') in expired or (run.get('sample_id') not in alive and path.stat().st_mtime < cutoff):
            targets.append(path)
            expired_blobs.add(run.get('candidate_sha256'))
        elif isinstance(run.get('candidate_sha256'), str):
            referenced.add(run['candidate_sha256'])
    for path in (root / 'comparisons').glob('comparison-*.json'):
        comparison = _json(path)
        if comparison.get('sample_id') in expired or (comparison.get('sample_id') not in alive and path.stat().st_mtime < cutoff):
            targets.append(path)
    for path in (root / 'blobs').glob('*.npz'):
        if (_HASH.fullmatch(path.stem) and path.stem not in referenced
                and (path.stem in expired_blobs or path.stat().st_mtime < cutoff)):
            targets.append(path)
    # Resolve and validate every exact target before any deletion on Windows.
    resolved_root = root.resolve()
    for target in targets:
        if target.is_symlink() or not target.resolve().is_relative_to(resolved_root):
            raise LearningError('拒绝清理指向学习目录外的文件')
    for target in targets:
        target.unlink(missing_ok=True)
    return {'expired_samples': len(expired), 'removed_files': len(targets)}


def retention_cases() -> list[str]:
    from . import cases
    return [path.name for path in cases.CASES_DIR.glob('*') if path.is_dir()
            and not path.is_symlink() and ((path / 'learning/segmentation').is_dir()
                or (path / 'learning/segmentation_models').is_dir())
            and (path / 'case.json').is_file()]


def prune_expired_cases() -> dict:
    totals = {'expired_samples': 0, 'removed_files': 0}
    for case_id in retention_cases():
        result = prune_case(case_id)
        for key in totals:
            totals[key] += result[key]
    return totals
