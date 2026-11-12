"""Persistent read-only candidate jobs; no formal mask or clinical approvals."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager

import numpy as np
from PIL import Image

from . import cases, preview, segmentation_predictor as predictor
from .config import APP_DIR
from .endplane_history import CURRENT_ACTOR_ID
from .segmentation_learning import case_operation, RETENTION_DAYS
from .volume import load_volume

CandidateError = predictor.CandidateError
LOG = logging.getLogger(__name__)
POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix='segmentation-candidate')
LOCK = threading.RLock()
ACTIVE = {}
ID = re.compile(r'model-[0-9a-f]{32}')
TIMEOUT_SECONDS = 1800
REPORT_ROOT = APP_DIR / '.local-models' / 'evaluations'
PHASES = {'queued':'等待本机推理', 'loading_model':'加载本机模型', 'waiting_gpu':'等待 GPU',
    'inference':'GPU 分割中', 'checking_geometry':'校验 CT 坐标', 'comparing':'生成新旧差异', 'complete':'候选完成，待复核'}


def now():
    return datetime.now(timezone.utc).isoformat()


def directory(case_id):
    root = cases.case_dir(case_id).resolve()
    path = root / 'learning' / 'segmentation_models'
    if not path.resolve().is_relative_to(root):
        raise CandidateError('候选目录超出病例范围')
    return path


def run_dir(case_id, run_id):
    if not isinstance(run_id, str) or not ID.fullmatch(run_id):
        raise CandidateError('候选编号无效')
    path = directory(case_id) / run_id
    if path.is_symlink() or not path.resolve().is_relative_to(directory(case_id).resolve()):
        raise CandidateError('候选目录无效')
    return path


def read(path):
    try:
        with LOCK:
            result = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(result, dict):
            raise ValueError('not an object')
        return result
    except (OSError, ValueError) as exc:
        raise CandidateError('候选记录不存在或损坏') from exc


def write(path, value):
    # Parent is created only when submitting a job; never recreate a deleted case.
    temp = path.with_name(path.name + '.' + secrets.token_hex(4) + '.tmp')
    try:
        with LOCK:
            temp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf-8')
            for attempt in range(25):
                try:
                    temp.replace(path)
                    break
                except PermissionError as exc:
                    # Windows denies replace while another process briefly reads.
                    # A persistent ACL/disk failure still surfaces to the caller.
                    if os.name!='nt' or getattr(exc,'winerror',None) not in {5,32} or attempt==24:raise
                    time.sleep(.01)
    finally:
        temp.unlink(missing_ok=True)


def process_alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def process_created(pid):
    """Windows creation ticks distinguish a surviving process from reused PID."""
    if os.name != 'nt' or type(pid) is not int or pid <= 0:
        return None
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.OpenProcess(0x1000,False,pid)
    if not handle:return None
    try:
        times=[wintypes.FILETIME() for _ in range(4)]
        if kernel.GetProcessTimes(handle,*(ctypes.byref(t) for t in times)):
            return (times[0].dwHighDateTime<<32)|times[0].dwLowDateTime
        return None
    finally:kernel.CloseHandle(handle)


def record_process_alive(record,kind):
    pid=record.get(kind+'_pid')
    if not process_alive(pid):return False
    expected=record.get(kind+'_created')
    actual=process_created(pid)
    return expected is None or actual is None or expected==actual


@contextmanager
def submission_lock(case_id):
    from .batch_jobs import ProcessLease
    from .errors import PipelineError
    lease=ProcessLease(directory(case_id)/'submission.lock')
    try:
        lease.acquire()
    except PipelineError as exc:
        raise CandidateError('该病例正在提交候选，请稍后刷新') from exc
    try:yield
    finally:lease.release()


def get_record(case_id, run_id):
    path = run_dir(case_id, run_id) / 'status.json'
    result = read(path)
    if result.get('case_id') != case_id or result.get('run_id') != run_id:
        raise CandidateError('候选记录不属于当前病例')
    if datetime.fromisoformat(result['created_at']) + timedelta(days=RETENTION_DAYS) <= datetime.now(timezone.utc):
        raise CandidateError('该候选已超过保留期')
    if result['status'] in {'queued','running'} and not record_process_alive(result,'controller'):
        if record_process_alive(result,'worker'):
            result.update(message='服务中断，等待原推理进程退出后可重试',stage='interrupted',updated_at=now())
        else:
            result.update(status='failed', message='服务中断，候选未完成；可重新生成', updated_at=now())
        write(path, result)
    return result


def public(result):
    keys = ('case_id','run_id','model_id','status','stage','message','created_at','updated_at',
            'mask_version','summary','elapsed_seconds','peak_gpu_mib','error_code')
    output = {key: result[key] for key in keys if key in result}
    output.update(committed=False, auto_promotion=False, production_eligible=False, poll_after_seconds=3)
    output['current'] = predictor.binding_is_current(result['case_id'], result['binding'])
    if output['status'] in {'queued','running'} and result.get('stage') not in {'interrupted','cancelling'}:
        progress = run_dir(result['case_id'], result['run_id']) / 'progress.json'
        if progress.exists():
            stage = read(progress).get('stage')
            if stage in PHASES:
                output.update(stage=stage, message=PHASES[stage])
    return output


def inspect(case_id):
    cases.get_case(case_id)
    paths = sorted(directory(case_id).glob('model-*/status.json'), key=lambda p:p.stat().st_mtime, reverse=True)
    runs = []
    for path in paths[:10]:
        record = read(path)
        if datetime.fromisoformat(record['created_at']) + timedelta(days=RETENTION_DAYS) > datetime.now(timezone.utc):
            runs.append(public(get_record(case_id, path.parent.name)))
    available = predictor.runtime_ready() and (predictor.MODEL_ROOT/'manifest.json').is_file()
    return {'case_id':case_id, 'model_id':predictor.MODEL_ID, 'model_available':available,
            'runs':runs, 'committed':False, 'auto_promotion':False}


def case_busy(case_id):
    return any(row['status'] in {'queued','running'} for row in inspect(case_id)['runs'])


@case_operation
def start(case_id, mask_version, model_id=predictor.MODEL_ID):
    cases.get_case(case_id)
    with LOCK, submission_lock(case_id):
        case = cases.get_case(case_id)
        if case.get('job', {}).get('status') == 'running' or any(s.get('status') == 'running' for s in case.get('stages', {}).values()):
            raise CandidateError('病例正在处理，请等待当前步骤完成')
        if any(r['status'] in {'queued','running'} for r in inspect(case_id)['runs']):
            raise CandidateError('该病例已有候选任务，请查看进度或先取消')
        if not predictor.PYTHON.is_file():
            raise CandidateError('独立模型环境尚未安装')
        if not predictor.runtime_ready():
            raise CandidateError('模型运行环境尚未通过本机验证')
        manifest, model_folder = predictor.model_manifest(model_id)
        binding = predictor.input_binding(case_id)
        if type(mask_version) is not int or binding['mask_version'] != mask_version:
            raise CandidateError('掩膜版本已变化，请刷新再试')
        run_id = 'model-' + secrets.token_hex(16)
        root = run_dir(case_id, run_id); root.mkdir(parents=True, exist_ok=False)
        np.save(root / 'baseline.npy', np.load(cases.case_dir(case_id)/'segment/mask.npy', allow_pickle=False), allow_pickle=False)
        record = {'schema':1,'case_id':case_id,'run_id':run_id,'model_id':model_id,
            'mask_version':mask_version,'binding':binding,'model_archive_sha256':manifest['archive_sha256'],
            'actor_id':CURRENT_ACTOR_ID.get(),'controller_pid':os.getpid(),
            'controller_created':process_created(os.getpid()),'created_at':now(),
            'updated_at':now(),'status':'queued','stage':'queued','message':PHASES['queued']}
        write(root/'status.json',record)
        folds=[p.parent.name[5:] for p in model_folder.glob('fold_*/checkpoint_final.pth')]
        write(root/'request.json',{'source_root':str(cases.case_dir(case_id).resolve()),
            'model_folder':str(model_folder),'folds':[int(x) if x.isdigit() else x for x in folds]})
        ACTIVE[run_id]={'cancel':threading.Event(),'process':None}
        POOL.submit(execute,case_id,run_id)
        return public(record)


def cancel(case_id, run_id):
    with LOCK:
        result=get_record(case_id,run_id)
        if result['status'] not in {'queued','running'}:
            return public(result)
        active=ACTIVE.get(run_id)
        if active is None:
            raise CandidateError('任务由另一个服务进程执行，请在原入口取消')
        active['cancel'].set()
        result.update(message='正在取消候选任务',stage='cancelling',updated_at=now())
        write(run_dir(case_id,run_id)/'status.json',result)
        return public(result)


@case_operation
def adopt_draft(case_id, run_id):
    """将已完成候选交给人工复核流程；不自动确认或放行下游。"""
    with LOCK, submission_lock(case_id):
        result = get_record(case_id, run_id)
        if result.get('status') != 'succeeded':
            raise CandidateError('候选尚未完成，不能转为待复核草稿')
        if not predictor.binding_is_current(case_id, result['binding'], full=True):
            raise CandidateError('候选输入已变化，请重新生成后再采用')
        root = run_dir(case_id, run_id)
        candidate_path = root / 'candidate.npy'
        if not candidate_path.is_file():
            raise CandidateError('候选文件不存在，不能转为待复核草稿')
        signature = result.get('candidate_signature')
        current_signature = [candidate_path.stat().st_size, candidate_path.stat().st_mtime_ns]
        if signature is not None and signature != current_signature:
            raise CandidateError('候选文件已变化，拒绝采用未经验证的结果')
        try:
            candidate = np.load(candidate_path, allow_pickle=False)
        except (OSError, ValueError) as exc:
            raise CandidateError('候选文件无法读取，请重新生成') from exc
        from . import mask_review
        return mask_review.adopt_ai_candidate_draft(
            case_id,
            candidate,
            run_id=run_id,
            model_id=result.get('model_id', predictor.MODEL_ID),
            source_mask_version=result['mask_version'],
        )


def _stop(process):
    if process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,timeout=15)
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill(); process.wait(timeout=10)


def _predict(root, active):
    env=dict(os.environ, PYTHONIOENCODING='utf-8', OMP_NUM_THREADS='4', OPENBLAS_NUM_THREADS='4')
    # Secrets are unnecessary in the model subprocess.
    for key in list(env):
        if any(word in key.upper() for word in ('API_KEY','PASSWORD','TOKEN','SECRET')):
            env.pop(key)
    with (root/'worker.log').open('w',encoding='utf-8') as log:
        process=subprocess.Popen([str(predictor.PYTHON),'-u',str(APP_DIR/'scripts/segmentation_model_worker.py'),
            '--request',str(root/'request.json')],cwd=APP_DIR,env=env,stdout=log,stderr=log,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        active['process']=process
        with LOCK:
            record=read(root/'status.json')
            record.update(worker_pid=process.pid,worker_created=process_created(process.pid))
            write(root/'status.json',record)
        deadline=time.monotonic()+TIMEOUT_SECONDS
        while process.poll() is None:
            if active['cancel'].wait(.5):
                _stop(process); raise CandidateError('任务已取消')
            if time.monotonic()>deadline:
                _stop(process); raise CandidateError('模型推理超过 30 分钟，已停止，可重试')
        if process.returncode:
            error=read(root/'worker-error.json') if (root/'worker-error.json').exists() else {}
            raise CandidateError(error.get('message',f'模型推理失败（退出码 {process.returncode}），请查看本机 worker.log'))


def execute(case_id,run_id):
    root=run_dir(case_id,run_id); active=ACTIVE[run_id]
    if not (root/'status.json').is_file():
        with LOCK:
            ACTIVE.pop(run_id,None)
        return
    result=read(root/'status.json')
    try:
        if active['cancel'].is_set():
            raise CandidateError('任务已取消')
        if not predictor.binding_is_current(case_id,result['binding'],full=True):
            raise CandidateError('输入已变化，未运行过期任务')
        result.update(status='running',updated_at=now()); write(root/'status.json',result)
        _predict(root,active)
        if active['cancel'].is_set():
            raise CandidateError('任务已取消')
        volume=load_volume(case_id)
        details=read(root/'worker-result.json')
        candidate=predictor.validate_prediction(np.load(root/'candidate.npy',allow_pickle=False),volume,details['geometry'])
        result['summary']=predictor.comparison_summary(np.load(root/'baseline.npy',allow_pickle=False),candidate,volume.spacing)
        if not predictor.binding_is_current(case_id,result['binding'],full=True):
            raise CandidateError('推理期间 CT 或掩膜发生变化，请重新生成候选')
        result.update(status='succeeded',stage='complete',message=PHASES['complete'],
            elapsed_seconds=details['elapsed_seconds'],peak_gpu_mib=details['peak_gpu_mib'],
            candidate_sha256=predictor.file_hash(root/'candidate.npy'),
            candidate_signature=[(root/'candidate.npy').stat().st_size,(root/'candidate.npy').stat().st_mtime_ns])
    except Exception as exc:
        result.update(status='cancelled' if active['cancel'].is_set() else 'failed',
            error_code=type(exc).__name__, message=str(exc) if isinstance(exc,CandidateError) else '候选处理失败，请检查本机日志后重试')
        LOG.exception('Segmentation candidate failed for %s / %s',case_id,run_id)
    finally:
        with LOCK:
            try:
                if active['cancel'].is_set():
                    result.update(status='cancelled',stage='cancelled',message='任务已取消')
                result['updated_at']=now()
                if root.exists() and (cases.case_dir(case_id)/'case.json').exists():
                    write(root/'status.json',result)
            finally:
                ACTIVE.pop(run_id,None)


def render(case_id,run_id,view,index,mode,wl,ww):
    record=get_record(case_id,run_id)
    if record['status'] != 'succeeded' or not predictor.binding_is_current(case_id,record['binding']):
        raise CandidateError('候选未完成或输入已变化，请重新生成')
    if mode not in {'baseline','candidate','difference'}:
        raise CandidateError('未知对照模式')
    if not np.isfinite(wl) or not np.isfinite(ww) or ww <= 0:
        raise CandidateError('窗宽窗位无效')
    root=run_dir(case_id,run_id); volume=load_volume(case_id)
    signature=record.get('candidate_signature')
    unchanged=(signature == [(root/'candidate.npy').stat().st_size,(root/'candidate.npy').stat().st_mtime_ns]
        if signature is not None else predictor.file_hash(root/'candidate.npy')==record.get('candidate_sha256'))
    if not unchanged:
        raise CandidateError('候选文件已变化，拒绝显示未经验证的结果')
    before=np.load(root/'baseline.npy',mmap_mode='r',allow_pickle=False)
    after=np.load(root/'candidate.npy',mmap_mode='r',allow_pickle=False)
    hu,old,_=preview._slice_arrays(volume.array,before,view,index)
    _,new,_=preview._slice_arrays(volume.array,after,view,index)
    old,new=old.astype(bool),new.astype(bool)
    gray=preview._to_grayscale(hu,wl,ww); rgb=np.repeat(gray[...,None],3,axis=-1)
    overlays=[(old,(45,212,191))] if mode=='baseline' else [(new,(45,212,191))] if mode=='candidate' else [
        (old & new,(45,212,191)),(new & ~old,(108,220,86)),(old & ~new,(255,110,95))]
    for mask,color in overlays:
        rgb[mask]=(rgb[mask]*.5+np.asarray(color)*.5).astype(np.uint8)
    out=BytesIO();Image.fromarray(rgb).save(out,format='PNG')
    return out.getvalue()


def prune():
    removed=0; cutoff=datetime.now(timezone.utc)-timedelta(days=RETENTION_DAYS)
    from .segmentation_learning import retention_cases
    roots=[p for cid in retention_cases() for p in directory(cid).glob('model-*')]
    for root in roots:
        if root.is_symlink() or not ID.fullmatch(root.name):
            raise CandidateError('拒绝清理异常候选目录')
        if not root.resolve().is_relative_to(cases.CASES_DIR.resolve()):
            raise CandidateError('候选清理范围异常')
        record=read(root/'status.json')
        if datetime.fromisoformat(record['created_at']) < cutoff and root.name not in ACTIVE:
            if record_process_alive(record,'worker') or (record['status'] in {'queued','running'} and record_process_alive(record,'controller')):
                continue
            shutil.rmtree(root);removed+=1
    return {'removed_candidates':removed, 'removed_reports':prune_reports()}


def prune_reports():
    """Development metadata has the same retention/deletion boundary as cases."""
    removed=0
    for path in REPORT_ROOT.glob('*.json'):
        if path.is_symlink() or not path.resolve().is_relative_to(REPORT_ROOT.resolve()):
            raise CandidateError('拒绝清理异常评测文件')
        report=read(path)
        if report.get('report_kind') != 'segmentation_model_development':
            continue
        if datetime.fromisoformat(report['created_at'])+timedelta(days=RETENTION_DAYS)<=datetime.now(timezone.utc):
            path.unlink();removed+=1;continue
        remaining=[row for row in report['cases'] if (cases.case_dir(row['case_id'])/'case.json').is_file()]
        if len(remaining)!=len(report['cases']):
            if not remaining:path.unlink();removed+=1
            else:report['cases']=remaining;write(path,report)
    return removed
