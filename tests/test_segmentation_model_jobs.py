import json
from pathlib import Path
import sys
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from test_segmentation_learning import sample_case
from pipeline import cases, segmentation_model_jobs as jobs, segmentation_predictor as predictor
from pipeline.volume import load_volume
REAL_PREDICT=jobs._predict


@pytest.fixture
def model_runtime(sample_case,tmp_path,monkeypatch):
    folder=tmp_path/'model';(folder/'fold_0').mkdir(parents=True)
    (folder/'fold_0/checkpoint_final.pth').write_bytes(b'test-control-only')
    monkeypatch.setattr(predictor,'PYTHON',Path(sys.executable))
    monkeypatch.setattr(predictor,'MODEL_ROOT',folder)
    monkeypatch.setattr(predictor,'runtime_ready',lambda:True)
    monkeypatch.setattr(predictor,'model_manifest',lambda model_id:({'archive_sha256':predictor.ARCHIVE_SHA256},folder))
    cid,_=sample_case
    def produce(root,active):
        candidate=np.load(root/'baseline.npy',allow_pickle=False);candidate[0,0,0]=1
        np.save(root/'candidate.npy',candidate,allow_pickle=False)
        jobs.write(root/'worker-result.json',{'geometry':predictor.image_geometry(load_volume(cid)),
            'elapsed_seconds':.1,'peak_gpu_mib':0})
    monkeypatch.setattr(jobs,'_predict',produce)
    return cid,produce


def finish(cid,run):
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        row=jobs.get_record(cid,run['run_id'])
        if row['status'] not in {'queued','running'}:return row
        time.sleep(.01)
    raise AssertionError('job did not finish')


def test_candidate_keeps_formal_inputs_and_reports_only_differences(model_runtime):
    cid,_=model_runtime; before=predictor.input_binding(cid)
    row=finish(cid,jobs.start(cid,1))
    assert row['status']=='succeeded'
    assert row['summary']['added_voxels']==1
    assert row['summary']['accuracy_status']=='reference_required'
    assert predictor.input_binding(cid)==before
    public=jobs.public(row)
    assert public['committed'] is False and 'binding' not in public and 'actor_id' not in public
    assert jobs.render(cid,row['run_id'],'axial',0,'difference',-500,2000).startswith(b'\x89PNG')


def test_adopt_candidate_creates_review_draft_and_keeps_downstream_blocked(model_runtime):
    cid, _ = model_runtime
    root = cases.case_dir(cid)
    original = np.load(root / 'segment/mask.npy', allow_pickle=False).copy()
    row = finish(cid, jobs.start(cid, 1))
    result = jobs.adopt_draft(cid, row['run_id'])
    assert result['status'] == 'drafted'
    assert result['need_human_confirm'] is True
    adopted = np.load(root / 'segment/mask.npy', allow_pickle=False)
    assert adopted[0, 0, 0] == 1
    assert np.array_equal(np.load(root / 'segment/candidate_mask.npy', allow_pickle=False), adopted)
    segmentation = json.loads((root / 'segment/segmentation.json').read_text(encoding='utf-8'))
    assert segmentation['need_human_confirm'] is True
    assert segmentation['mask_source'] == 'ai_model_candidate_draft'
    assert segmentation['ai_candidate_draft']['run_id'] == row['run_id']
    assert segmentation['mask_version'] == 2
    snapshot = root / 'review' / 'ai_candidate_drafts' / row['run_id']
    assert np.array_equal(np.load(snapshot / 'previous_mask.npy', allow_pickle=False), original)
    assert (snapshot / 'previous_candidate.npy').is_file()
    assert json.loads((snapshot / 'previous_segmentation.json').read_text(encoding='utf-8'))['mask_version'] == 1
    case = cases.get_case(cid)
    assert case['stages']['segment']['status'] == 'warn'
    assert case['stages']['segment']['data']['need_human_confirm'] is True
    assert case['stages']['surface']['status'] == 'pending'
    with pytest.raises(jobs.CandidateError, match='输入已变化'):
        jobs.adopt_draft(cid, row['run_id'])


def test_version_change_during_inference_fails_and_preview_is_blocked(model_runtime,monkeypatch):
    cid,produce=model_runtime
    def change(root,active):
        produce(root,active)
        p=cases.case_dir(cid)/'segment/segmentation.json';d=json.loads(p.read_text());d['mask_version']=2;p.write_text(json.dumps(d))
    monkeypatch.setattr(jobs,'_predict',change)
    row=finish(cid,jobs.start(cid,1))
    assert row['status']=='failed' and '变化' in row['message']
    with pytest.raises(jobs.CandidateError):jobs.render(cid,row['run_id'],'axial',0,'difference',-500,2000)


@pytest.mark.parametrize('_attempt',range(12))
def test_cancellation_never_publishes_a_candidate(model_runtime,monkeypatch,_attempt):
    cid,_=model_runtime
    def waiting(root,active):
        if not active['cancel'].wait(3):raise AssertionError('cancel was not delivered')
        raise jobs.CandidateError('任务已取消')
    monkeypatch.setattr(jobs,'_predict',waiting)
    run=jobs.start(cid,1);jobs.cancel(cid,run['run_id']);row=finish(cid,run)
    assert row['status']=='cancelled'
    assert not (jobs.run_dir(cid,row['run_id'])/'candidate.npy').exists()


def test_rejects_stale_version_and_invalid_run_path(model_runtime):
    cid,_=model_runtime
    with pytest.raises(jobs.CandidateError,match='版本'):jobs.start(cid,2)
    with pytest.raises(jobs.CandidateError):jobs.get_record(cid,'../../volume')


def test_http_contract_forbids_paths_and_boolean_versions(model_runtime):
    import server
    cid,_=model_runtime;client=TestClient(server.app);path=f'/api/cases/{cid}/segment/models'
    for body in [{'mask_version':True},{'mask_version':1,'path':'private'},{'mask_version':1,'model_id':'url'}]:
        assert client.post(path,json=body).status_code==422
    response=client.post(path,json={'mask_version':1})
    assert response.status_code==202,response.text
    row=finish(cid,response.json())
    assert client.get(path+'/'+row['run_id']).json()['committed'] is False
    assert client.get(path+'/'+row['run_id']+'/slice/axial?index=0&mode=difference').status_code==200
    assert client.get(path+'/'+row['run_id']+'/slice/axial?index=99999').status_code==400


def test_unknown_model_routes_are_denied():
    from pipeline.access_policy import classify
    root='/api/cases/case-20261106-120000-abcd/segment/models';rid='model-'+'a'*32
    assert classify(root,'GET').capability=='view'
    assert classify(root,'POST').capability=='edit'
    assert classify(root+'/'+rid+'/cancel','POST').capability=='edit'
    assert classify(root+'/'+rid+'/slice/axial','GET').capability=='view'
    assert classify(root+'/'+rid+'/approve','POST').kind=='deny'


def test_interrupted_controller_does_not_allow_deletion_while_worker_survives(model_runtime,monkeypatch):
    cid,_=model_runtime;row=finish(cid,jobs.start(cid,1))
    row.update(status='running',controller_pid=900001,worker_pid=900002)
    jobs.write(jobs.run_dir(cid,row['run_id'])/'status.json',row)
    monkeypatch.setattr(jobs,'process_alive',lambda pid:pid==900002)
    recovered=jobs.get_record(cid,row['run_id'])
    assert recovered['stage']=='interrupted' and jobs.case_busy(cid)
    monkeypatch.setattr(jobs,'process_alive',lambda pid:False)
    assert jobs.get_record(cid,row['run_id'])['status']=='failed'


def test_expired_candidates_are_pruned_without_removing_formal_files(model_runtime,monkeypatch):
    from datetime import datetime,timedelta,timezone
    from pipeline import segmentation_learning
    cid,_=model_runtime;row=finish(cid,jobs.start(cid,1));before=predictor.input_binding(cid)
    row['created_at']=(datetime.now(timezone.utc)-timedelta(days=181)).isoformat()
    root=jobs.run_dir(cid,row['run_id']);jobs.write(root/'status.json',row)
    monkeypatch.setattr(segmentation_learning,'retention_cases',lambda:[cid])
    assert jobs.prune()['removed_candidates']==1
    assert not root.exists() and predictor.input_binding(cid)==before


def test_modified_candidate_is_not_rendered(model_runtime):
    cid,_=model_runtime;row=finish(cid,jobs.start(cid,1))
    path=jobs.run_dir(cid,row['run_id'])/'candidate.npy'
    arr=np.load(path);arr[0,0,0]=0;np.save(path,arr)
    with pytest.raises(jobs.CandidateError,match='已变化'):
        jobs.render(cid,row['run_id'],'axial',0,'candidate',-500,2000)


def test_rejects_unverified_runtime(model_runtime,monkeypatch):
    cid,_=model_runtime
    monkeypatch.setattr(predictor,'runtime_ready',lambda:False)
    with pytest.raises(jobs.CandidateError,match='本机验证'):
        jobs.start(cid,1)


def test_cancel_during_final_comparison_cannot_publish_success(model_runtime,monkeypatch):
    cid,_=model_runtime;compare=predictor.comparison_summary
    def cancel_before_commit(*args):
        rid=next(rid for rid in jobs.ACTIVE if jobs.run_dir(cid,rid).is_dir())
        jobs.cancel(cid,rid)
        return compare(*args)
    monkeypatch.setattr(predictor,'comparison_summary',cancel_before_commit)
    row=finish(cid,jobs.start(cid,1))
    assert row['status']=='cancelled'
    with pytest.raises(jobs.CandidateError):jobs.render(cid,row['run_id'],'axial',0,'candidate',-500,2000)


def test_process_cancel_stops_worker_and_child(tmp_path,monkeypatch):
    import threading
    scripts=tmp_path/'scripts';scripts.mkdir()
    script=scripts/'segmentation_model_worker.py'
    script.write_text(
        'import subprocess,sys,time,json; from pathlib import Path'+chr(10)+
        'root=Path(sys.argv[sys.argv.index("--request")+1]).parent'+chr(10)+
        'child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(40)"])'+chr(10)+
        '(root/"child.json").write_text(json.dumps({"pid":child.pid}))'+chr(10)+
        'time.sleep(40)',encoding='utf-8')
    run=tmp_path/'run';run.mkdir();jobs.write(run/'request.json',{});jobs.write(run/'status.json',{})
    monkeypatch.setattr(jobs,'APP_DIR',tmp_path);monkeypatch.setattr(predictor,'PYTHON',Path(sys.executable))
    active={'cancel':threading.Event(),'process':None};failures=[]
    def execute():
        try:REAL_PREDICT(run,active)
        except jobs.CandidateError as exc:failures.append(str(exc))
    thread=threading.Thread(target=execute);thread.start()
    try:
        deadline=time.monotonic()+8
        while not (run/'child.json').exists() and time.monotonic()<deadline:time.sleep(.05)
        assert (run/'child.json').exists()
        child=jobs.read(run/'child.json')['pid']
        active['cancel'].set();thread.join(timeout=20)
        assert not thread.is_alive() and failures==['任务已取消']
        assert active['process'].poll() is not None
        assert not jobs.process_alive(child)
    finally:
        active['cancel'].set();thread.join(timeout=20)


def test_development_report_expires_and_case_deletion_removes_its_row(model_runtime):
    from datetime import datetime,timedelta,timezone
    cid,_=model_runtime
    jobs.REPORT_ROOT.mkdir()
    expired=jobs.REPORT_ROOT/'expired.json'
    jobs.write(expired,{'report_kind':'segmentation_model_development',
        'created_at':(datetime.now(timezone.utc)-timedelta(days=181)).isoformat(),'cases':[{'case_id':cid}]})
    deleted=jobs.REPORT_ROOT/'deleted.json'
    jobs.write(deleted,{'report_kind':'segmentation_model_development','created_at':jobs.now(),
        'cases':[{'case_id':cid},{'case_id':'case-20000101-000000-abcd'}]})
    assert jobs.prune_reports()==1 and not expired.exists()
    assert jobs.read(deleted)['cases']==[{'case_id':cid}]
