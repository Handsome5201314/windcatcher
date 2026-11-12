"""Physical-space segmentation metrics. No case access or reference promotion."""
from __future__ import annotations

import numpy as np
from scipy import ndimage


def binary_mask(value) -> np.ndarray:
    values = np.asarray(value)
    if values.ndim != 3 or not all(values.shape) or not np.isin(values, (0, 1)).all():
        raise ValueError('掩膜必须是非空三维二值数组')
    return values.astype(bool, copy=False)


def valid_spacing(value) -> tuple[float, float, float]:
    spacing = np.asarray(value, dtype=float)
    if spacing.shape != (3,) or not np.isfinite(spacing).all() or np.any(spacing <= 0):
        raise ValueError('体素间距必须是三个有限正数，单位 mm')
    return tuple(float(x) for x in spacing)


def mask_metrics(candidate, reference, spacing) -> dict:
    candidate, reference = binary_mask(candidate), binary_mask(reference)
    spacing = valid_spacing(spacing)
    if candidate.shape != reference.shape or not reference.any():
        raise ValueError('参考掩膜为空或与候选尺寸不一致')
    tp = int(np.count_nonzero(candidate & reference))
    fp = int(np.count_nonzero(candidate & ~reference))
    fn = int(np.count_nonzero(~candidate & reference))
    voxel_mm3 = float(np.prod(spacing))
    structure = ndimage.generate_binary_structure(3, 1)
    # Face connectivity catches a broken narrow passage hidden by high Dice.
    cl, nc = ndimage.label(candidate, structure)
    rl, nr = ndimage.label(reference, structure)
    overlap = candidate & reference
    pairs = np.unique(np.stack((cl[overlap], rl[overlap]), axis=1), axis=0)
    merges = int(np.count_nonzero(np.bincount(pairs[:, 0], minlength=nc + 1) > 1)) if pairs.size else 0
    splits = int(np.count_nonzero(np.bincount(pairs[:, 1], minlength=nr + 1) > 1)) if pairs.size else 0
    distances = None
    if candidate.any():
        coords = np.nonzero(candidate | reference)
        bbox = tuple(slice(max(0, int(c.min()) - 1), min(candidate.shape[i], int(c.max()) + 2))
                     for i, c in enumerate(coords))
        c, r = candidate[bbox], reference[bbox]
        cb = c & ~ndimage.binary_erosion(c, structure, border_value=0)
        rb = r & ~ndimage.binary_erosion(r, structure, border_value=0)
        distances = np.concatenate((
            ndimage.distance_transform_edt(~rb, sampling=spacing)[cb],
            ndimage.distance_transform_edt(~cb, sampling=spacing)[rb],
        ))
    return {
        'dice': 2 * tp / (2 * tp + fp + fn),
        'false_positive_voxels': fp, 'false_negative_voxels': fn,
        'false_positive_mm3': fp * voxel_mm3, 'false_negative_mm3': fn * voxel_mm3,
        'false_positive_fraction': fp / int(reference.sum()),
        'false_negative_fraction': fn / int(reference.sum()),
        'candidate_components': nc, 'reference_components': nr,
        'component_count_error': abs(nc - nr),
        'reference_components_split': splits, 'candidate_components_merged': merges,
        'empty_candidate': not bool(candidate.any()),
        'surface_hd95_mm': None if distances is None else float(np.percentile(distances, 95)),
        'max_surface_distance_mm': None if distances is None else float(distances.max()),
        'changed_voxels': fp + fn,
    }


def freeze_cohort(entries: list[dict]) -> dict:
    """Freeze explicit references and patient-level splits, never infer labels."""
    from . import segmentation_learning as learning
    from .cases import get_case
    if not isinstance(entries, list) or not 1 <= len(entries) <= 500:
        raise learning.LearningError('评测集必须显式指定 1–500 个样本')
    seen, subjects, frozen = set(), {}, []
    for item in entries:
        if (not isinstance(item, dict) or set(item) != {'case_id', 'sample_id', 'split'}
                or item['split'] not in {'development', 'holdout'}):
            raise learning.LearningError('评测集字段或分组无效')
        key = (item['case_id'], item['sample_id'])
        if key in seen:
            raise learning.LearningError('评测集含重复样本')
        seen.add(key)
        before, reference, record = learning.load_pair(*key)
        review = learning._review(item['case_id'], record)
        if not review or review['disposition'] != 'approved':
            raise learning.LearningError('样本没有经过显式参考审核')
        subject = learning.digest(review['subject_group'])
        if subject in subjects and subjects[subject] != item['split']:
            raise learning.LearningError('同一患者不能同时进入开发集和留出集')
        subjects[subject] = item['split']
        # Validate nonempty reference and physical dimensions before freezing.
        mask_metrics(before, reference, record['geometry']['spacing_mm'])
        frozen.append({**item, 'subject_group_sha256': subject,
            'record_sha256': record['record_sha256'], 'review_sha256': review['review_sha256'],
            'synthetic': get_case(item['case_id']).get('synthetic') is True})
    frozen.sort(key=lambda row: (row['case_id'], row['sample_id']))
    cohort = {'schema': 'segmentation-cohort-v1', 'entries': frozen}
    return {**cohort, 'dataset_sha256': learning.digest(cohort)}


def evaluate_cohort(cohort: dict, strategy_id: str) -> dict:
    """Reverify all bindings, execute on source snapshots, compare held-out data."""
    from . import segmentation_learning as learning
    if (not isinstance(cohort, dict) or set(cohort) != {'schema', 'entries', 'dataset_sha256'}
            or cohort['schema'] != 'segmentation-cohort-v1'
            or cohort['dataset_sha256'] != learning.digest({k: v for k, v in cohort.items() if k != 'dataset_sha256'})):
        raise learning.LearningError('冻结评测集校验失败')
    fresh = freeze_cohort([{k: item[k] for k in ('case_id', 'sample_id', 'split')} for item in cohort['entries']])
    if fresh != cohort:
        raise learning.LearningError('样本、审核或患者分组与冻结记录不同')
    if strategy_id not in learning.STRATEGIES:
        raise learning.LearningError('不支持的分割策略')
    rows = []
    fields = ('false_negative_voxels', 'false_positive_voxels', 'component_count_error',
              'reference_components_split', 'candidate_components_merged',
              'surface_hd95_mm', 'max_surface_distance_mm')
    for item in cohort['entries']:
        run = learning.run_candidate(item['case_id'], item['sample_id'], strategy_id)
        if run['evaluation_status'] != 'evaluated':
            raise learning.LearningError('冻结参考不再可用')
        baseline, candidate = run['baseline_metrics'], run['metrics']
        regressions = [key for key in fields if candidate[key] is None or baseline[key] is None
                       or candidate[key] > baseline[key] + 1e-9]
        improved = not regressions and any(candidate[k] < baseline[k] - 1e-9 for k in fields)
        rows.append({**item, 'run_id': run['run_id'], 'strategy_source_sha256': run['strategy_source_sha256'],
            'evaluator_sha256': run['evaluator_sha256'], 'baseline_metrics': baseline, 'metrics': candidate,
            'regressed_metrics': regressions, 'improved': improved})
    def summary(split):
        selected = [row for row in rows if row['split'] == split]
        return {'samples': len(selected), 'subjects': len({r['subject_group_sha256'] for r in selected}),
            'regressions': sum(bool(row['regressed_metrics']) for row in selected),
            'improved_samples': sum(row['improved'] for row in selected)}
    report = {'schema': 'segmentation-evaluation-v1', 'dataset_sha256': cohort['dataset_sha256'],
        'strategy_id': strategy_id, 'rows': rows, 'development': summary('development'),
        'holdout': summary('holdout'), 'synthetic_only': all(row['synthetic'] for row in rows),
        'auto_promotion': False, 'clinical_validation': False,
        'reference_provenance_independently_verified': False}
    return {**report, 'report_sha256': learning.digest(report)}
