"""Reviewed public reference snapshots, independent of private patient shares."""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import time
from contextlib import contextmanager

from . import automation, cases, cfd_results, openfoam_runner, patient_shares

ReferenceError = patient_shares.ShareError
TAGS = {
    'no_noted_structural_abnormality': {'label': '未见明显结构异常（人工标签）', 'group': '结构特征'},
    'septal_deviation': {'label': '鼻中隔偏曲', 'group': '结构特征'},
    'turbinate_hypertrophy': {'label': '下鼻甲肥大', 'group': '结构特征'},
    'sinusitis': {'label': '鼻窦炎', 'group': '临床诊断'},
    'empty_nose_syndrome': {'label': '空鼻症', 'group': '临床诊断'},
    'post_turbinate_outfracture': {'label': '下鼻甲外移术后', 'group': '术后状态'},
    'post_turbinate_surgery': {'label': '鼻甲手术后', 'group': '术后状态'},
    'post_septal_surgery': {'label': '鼻中隔手术后', 'group': '术后状态'},
}
CONDITION_FIELDS = ('solver', 'turbulence_model', 'inlet_mode', 'total_flow_lpm', 'nu', 'rho_kg_m3')
FLOW_TEST_WARNING = '测试气流 · 未复核的 CFD 结果，仅用于核对可视化，不作为正常参考或诊疗依据。'
FLOW_TEST_SECONDS = 86400


@contextmanager
def _db():
    with patient_shares._db() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS reference_cases (
          id TEXT PRIMARY KEY, case_id TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
          description TEXT NOT NULL, tags TEXT NOT NULL, status TEXT NOT NULL,
          kind TEXT NOT NULL, reviewer TEXT NOT NULL, review_note TEXT NOT NULL,
          authorization_confirmed INTEGER NOT NULL, labels_confirmed INTEGER NOT NULL,
          payload_sha256 TEXT, parameters TEXT NOT NULL, source_signature TEXT,
          created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reference_events (
          id INTEGER PRIMARY KEY, reference_id TEXT NOT NULL, action TEXT NOT NULL,
          reviewer TEXT NOT NULL, note TEXT NOT NULL, payload_sha256 TEXT, created_at INTEGER NOT NULL
        );
        ''')
        yield db


def snapshot_path(digest: str):
    if not re.fullmatch(r'[0-9a-f]{64}', digest or ''):
        raise ReferenceError('参考模型版本无效', 503)
    return patient_shares.SHARE_DIR / 'reference-snapshots' / (digest + '.json')


def get_entry(reference_id: str, *, published: bool = False) -> dict:
    if not re.fullmatch(r'[0-9a-f]{32}', reference_id):
        raise ReferenceError('参考病例不存在或已撤回', 404)
    with _db() as db:
        row = db.execute('SELECT * FROM reference_cases WHERE id=?', (reference_id,)).fetchone()
    if row is None or published and not _publicly_active(row):
        raise ReferenceError('参考病例不存在或已撤回', 404)
    return dict(row)


def _publicly_active(row) -> bool:
    return not cases.deletion_pending(row['case_id']) and row['status'] == 'published' and (row['kind'] != 'flow_test'
            or row['updated_at'] + FLOW_TEST_SECONDS > time.time())


def public_conditions(parameters: dict) -> dict:
    result = {}
    for key in CONDITION_FIELDS:
        value = parameters.get(key)
        if key in {'solver', 'turbulence_model', 'inlet_mode'}:
            if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value):
                result[key] = value
        elif type(value) in (int, float) and math.isfinite(value):
            result[key] = value
    return result


def _public(row) -> dict:
    return {k: row[k] for k in ('id', 'title', 'description', 'kind', 'updated_at')} | {
        'tags': json.loads(row['tags']), 'conditions': public_conditions(json.loads(row['parameters'])),
        'geometry_note': '气道空气空间的展示表面；不代表鼻甲、鼻中隔组织的独立分割。',
        'flow_verified': row['kind'] == 'flow',
        'warning': FLOW_TEST_WARNING if row['kind'] == 'flow_test' else '',
        'expires_at': row['updated_at'] + FLOW_TEST_SECONDS if row['kind'] == 'flow_test' else None,
        'version': _version(row),
    }


def _version(row) -> str:
    # Bind geometry and its publication metadata together, including parameters.
    # A republish between catalog and model fetch must not mix two versions.
    return hashlib.sha256(json.dumps([row[k] for k in (
        'id', 'title', 'description', 'tags', 'kind', 'payload_sha256', 'parameters', 'source_signature'
    )], ensure_ascii=False).encode()).hexdigest()


def catalog(tag: str | None = None) -> dict:
    if tag is not None and tag not in TAGS:
        raise ReferenceError('未知参考标签', 400)
    with _db() as db:
        rows = db.execute("SELECT * FROM reference_cases WHERE status='published' ORDER BY created_at,id").fetchall()
    return {'items': [_public(row) for row in rows if _publicly_active(row)
                      and (tag is None or tag in json.loads(row['tags']))], 'tags': TAGS}


def candidates() -> dict:
    with _db() as db:
        saved = {row['case_id']: dict(row) for row in db.execute('SELECT * FROM reference_cases')}
    items = []
    for case in cases.list_cases():
        case_id = case['case_id']
        case = cases.get_case(case_id)
        stages = case.get('stages', {})
        surface = cases.artifact_path(case_id, 'surface/airway.stl')
        item = {'case_id': case_id, 'surface_ready': stages.get('surface', {}).get('status') == 'ok'
                and surface.is_file() and not surface.is_symlink(),
                'cfd_status': stages.get('cfd_run', {}).get('status', 'pending'), 'entry': None}
        if case_id in saved:
            entry = saved[case_id]
            item['entry'] = {**_public(entry), 'status': entry['status'],
                             'authorization_confirmed': bool(entry['authorization_confirmed']),
                             'labels_confirmed': bool(entry['labels_confirmed']),
                             'review_note': entry['review_note']}
            if entry['status'] == 'published' and not _publicly_active(entry):
                item['entry']['status'] = 'expired'
        items.append(item)
    return {'items': items, 'tags': TAGS}


def _anatomy_model(case_id: str) -> bytes:
    # STL is generated by surface.py in patient LPS millimetres. The public
    # display is reduced, never used as input to CFD or written over the STL.
    import numpy as np
    import vtk
    from vtk.util.numpy_support import vtk_to_numpy
    path = cases.artifact_path(case_id, 'surface/airway.stl')
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 200 * 1024 * 1024:
        raise ReferenceError('当前没有可发布的气道表面')
    reader = vtk.vtkSTLReader()
    reader.SetFileName(str(path))
    reader.Update()
    surface = reader.GetOutput()
    source_cells = surface.GetNumberOfCells()
    if source_cells > 20000:
        decimate = vtk.vtkQuadricDecimation()
        decimate.SetInputData(surface)
        decimate.SetTargetReduction(1 - 20000 / source_cells)
        decimate.Update()
        surface = decimate.GetOutput()
    if surface.GetPoints() is None or surface.GetNumberOfCells() < 1:
        raise ReferenceError('气道表面为空，不能发布')
    points = vtk_to_numpy(surface.GetPoints().GetData()).astype(np.float64) / 1000
    if not np.isfinite(points).all():
        raise ReferenceError('气道表面含无效坐标')
    bounds = [float(value) for axis in range(3) for value in (points[:, axis].min(), points[:, axis].max())]
    if any(bounds[i] >= bounds[i + 1] for i in (0, 2, 4)):
        raise ReferenceError('气道表面没有完整的三维范围')
    polygons = surface.GetPolys()
    offsets = vtk_to_numpy(polygons.GetOffsetsArray())
    if not np.all(np.diff(offsets) == 3):
        raise ReferenceError('气道表面包含非三角网格')
    faces = vtk_to_numpy(polygons.GetConnectivityArray())
    view = {'kind': 'anatomy', 'units': {'coordinates': 'm'}, 'bounds_m': bounds,
            'wall': {'positions': points.reshape(-1).tolist(), 'indices': faces.tolist()},
            'patches': {}, 'streamlines': [], 'slices': [], 'ranges': {},
            'display_geometry': {'source_faces': source_cells, 'display_faces': surface.GetNumberOfCells(),
                                 'reduced_for_display': source_cells > 20000}}
    return json.dumps(view, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def _capture(case_id: str, kind: str) -> tuple[bytes, dict, str]:
    case = cases.get_case(case_id)
    if case.get('job', {}).get('status') == 'running' or any(
            s.get('status') == 'running' for s in case.get('stages', {}).values()):
        raise ReferenceError('病例正在处理，不能发布')
    automation.require_confirmed_mask(case_id)
    if case.get('stages', {}).get('surface', {}).get('status') != 'ok':
        raise ReferenceError('当前气道表面尚未生成或已过期')
    before = patient_shares._input_hashes(case_id)
    params = {}
    if kind == 'anatomy':
        snapshot = _anatomy_model(case_id)
        signature = hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest()
    else:
        if kind == 'flow':
            gate = openfoam_runner.check_result_gate(case_id)
            if gate.get('eligible') is not True or gate.get('numeric_ok') is not True:
                raise ReferenceError('CFD 数值、网格敏感性或工程复核未通过；可选择未复核测试气流或先发布结构')
        elif kind == 'flow_test':
            status_path = cases.artifact_path(case_id, 'cfd/run_status.json')
            try:
                if status_path.is_symlink():
                    raise OSError('symlink')
                run_status = json.loads(status_path.read_text(encoding='utf-8'))
            except (OSError, ValueError) as exc:
                raise ReferenceError('没有已完成的 CFD 求解，不能发布测试气流') from exc
            if not isinstance(run_status, dict) or run_status.get('status') != 'ran' or not run_status.get('run_id'):
                raise ReferenceError('CFD 求解尚未完成，不能发布测试气流')
        else:
            raise ReferenceError('发布类型无效', 400)
        result = cfd_results.get_results(case_id)
        signature = result.get('source_signature')
        if result.get('status') != 'ready' or not signature or not isinstance(result.get('payload'), str):
            raise ReferenceError('当前气流结果不存在或已过期')
        path = cases.artifact_path(case_id, result['payload'])
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 200 * 1024 * 1024:
            raise ReferenceError('气流模型无法安全发布')
        try:
            raw = patient_shares._public_view(json.loads(path.read_text(encoding='utf-8')))
            meta = json.loads(cases.artifact_path(case_id, 'cfd/case_meta.json').read_text(encoding='utf-8'))
            params = {k: meta.get(k) for k in patient_shares.PARAMETER_FIELDS}
            json.dumps(params, allow_nan=False)
            snapshot = patient_shares.display_model_bytes(json.dumps(raw, allow_nan=False).encode(), params)
        except (OSError, ValueError, TypeError) as exc:
            raise ReferenceError('气流模型或工况记录无法安全读取') from exc
        current = cfd_results.get_results(case_id)
        if current.get('source_signature') != signature or current.get('status') != 'ready':
            raise ReferenceError('发布期间气流结果发生变化，请重试')
    if patient_shares._input_hashes(case_id) != before:
        raise ReferenceError('发布期间上游模型发生变化，请重新核对')
    return snapshot, params, signature


def save(case_id: str, payload: dict, reviewer: str) -> dict:
    cases.get_case(case_id)
    action = payload['action']
    tags = payload['tags']
    if len(payload['title'].strip()) < 2 or len(payload['review_note'].strip()) < 10:
        raise ReferenceError('公开标题至少 2 字，内部审核说明至少 10 字', 400)
    for key in ('title', 'description'):
        if any(char in payload[key] for char in '<>'):
            raise ReferenceError('公开标题和说明请使用纯文本', 400)
    if len(tags) != len(set(tags)) or any(tag not in TAGS for tag in tags):
        raise ReferenceError('请选择有效且不重复的人工核对标签', 400)
    if action == 'publish' and (not tags or payload['authorization_confirmed'] is not True
                                or payload['labels_confirmed'] is not True):
        raise ReferenceError('发布前请确认展示授权、标签并至少选择一个标签', 400)
    if action == 'publish' and payload['kind'] == 'flow_test' and payload.get('test_use_confirmed') is not True:
        raise ReferenceError('请确认这是未复核测试气流，仅供获授权的可视化测试', 400)
    snapshot, params, signature = _capture(case_id, payload['kind']) if action == 'publish' else (None, {}, None)
    digest = hashlib.sha256(snapshot).hexdigest() if snapshot is not None else None
    now = int(time.time())
    with _db() as db:
        old = db.execute('SELECT * FROM reference_cases WHERE case_id=?', (case_id,)).fetchone()
        if old and _publicly_active(old) and action == 'draft':
            raise ReferenceError('该参考病例已发布；请重新发布固定版本，或先撤回再编辑草稿')
        reference_id = old['id'] if old else secrets.token_hex(16)
        if snapshot is not None:
            path = snapshot_path(digest)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.parent.is_symlink():
                raise ReferenceError('参考快照目录配置无效', 503)
            temp = path.with_name('.' + secrets.token_hex(12) + '.tmp')
            try:
                temp.write_bytes(snapshot)
                temp.replace(path)
            finally:
                temp.unlink(missing_ok=True)
        db.execute('''INSERT INTO reference_cases VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(case_id) DO UPDATE SET title=excluded.title,description=excluded.description,
          tags=excluded.tags,status=excluded.status,kind=excluded.kind,reviewer=excluded.reviewer,
          review_note=excluded.review_note,authorization_confirmed=excluded.authorization_confirmed,
          labels_confirmed=excluded.labels_confirmed,payload_sha256=excluded.payload_sha256,
          parameters=excluded.parameters,source_signature=excluded.source_signature,updated_at=excluded.updated_at''',
          (reference_id, case_id, payload['title'].strip(), payload['description'].strip(),
           json.dumps(tags), 'published' if action == 'publish' else 'draft', payload['kind'], reviewer,
           payload['review_note'].strip(), int(payload['authorization_confirmed']), int(payload['labels_confirmed']),
           digest, json.dumps(params, allow_nan=False), signature, old['created_at'] if old else now, now))
        db.execute('INSERT INTO reference_events(reference_id,action,reviewer,note,payload_sha256,created_at) VALUES(?,?,?,?,?,?)',
                   (reference_id, 'publish_test' if action == 'publish' and payload['kind'] == 'flow_test' else action,
                    reviewer, payload['review_note'].strip(), digest, now))
    entry = get_entry(reference_id)
    return {**_public(entry), 'status': entry['status']}


def revoke(reference_id: str, reviewer: str):
    get_entry(reference_id)
    with _db() as db:
        db.execute("UPDATE reference_cases SET status='withdrawn',updated_at=? WHERE id=?", (int(time.time()), reference_id))
        db.execute('INSERT INTO reference_events(reference_id,action,reviewer,note,created_at) VALUES(?,?,?,?,?)',
                   (reference_id, 'revoke', reviewer, '撤回参考展示', int(time.time())))


def revoke_case(case_id: str):
    with _db() as db:
        row = db.execute('SELECT id FROM reference_cases WHERE case_id=?', (case_id,)).fetchone()
    if row:
        revoke(row['id'], 'case-deletion')


def read_model(reference_id: str, version: str | None = None) -> bytes:
    row = get_entry(reference_id, published=True)
    if version is not None and version != _version(row):
        raise ReferenceError('参考病例版本已更新，请重新读取列表', 409)
    path = snapshot_path(row['payload_sha256'])
    if path.is_symlink() or not path.is_file():
        raise ReferenceError('参考模型暂不可用，请联系团队', 503)
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != row['payload_sha256']:
        raise ReferenceError('参考模型校验失败，请联系团队', 503)
    return data
