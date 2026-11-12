"""Real browser/routes with a deterministic synthetic worker; no accuracy claims."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from contextlib import contextmanager
from playwright.sync_api import expect,sync_playwright

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('learning_smoke',Path(__file__).with_name('segmentation-learning-browser-smoke.py'))
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
OUTPUT=Path(tempfile.gettempdir())/'windcatcher-segmentation-models-20261106'
FIXTURE=r'''
from pipeline import segmentation_model_jobs as model_jobs,segmentation_predictor as model_predictor
model_root=temp/'synthetic-model'
(model_root/'fold_0').mkdir(parents=True)
(model_root/'fold_0/checkpoint_final.pth').write_bytes(b'synthetic-ui-fixture-only')
(model_root/'manifest.json').write_text('{}')
model_predictor.MODEL_ROOT=model_root
model_predictor.PYTHON=Path(sys.executable)
model_predictor.runtime_ready=lambda:True
model_predictor.model_manifest=lambda model_id:({'archive_sha256':model_predictor.ARCHIVE_SHA256},model_root)
def synthetic_predict(run,active):
    model_jobs.write(run/'progress.json',{'stage':'inference'})
    if active['cancel'].wait(1.5):raise model_jobs.CandidateError('任务已取消')
    candidate=np.load(run/'baseline.npy',allow_pickle=False)
    candidate[5,14,18]=0;candidate[6,14,17]=1
    np.save(run/'candidate.npy',candidate,allow_pickle=False)
    from pipeline.volume import load_volume
    model_jobs.write(run/'worker-result.json',{'geometry':model_predictor.image_geometry(load_volume(cid)),
        'elapsed_seconds':1.5,'peak_gpu_mib':0})
model_jobs._predict=synthetic_predict
'''
CHILD=base.CHILD.replace('import server, uvicorn',FIXTURE+chr(10)+'import server, uvicorn')


@contextmanager
def isolated_directory():
    directory=tempfile.TemporaryDirectory(prefix='windcatcher-model-ui-')
    try:
        yield directory.name
    finally:
        # taskkill returns before all descendant log handles finish closing.
        for attempt in range(20):
            try:
                directory.cleanup()
                break
            except PermissionError as exc:
                if getattr(exc,'winerror',None)!=32 or attempt==19:raise
                time.sleep(.15)


def main():
    OUTPUT.mkdir(parents=True,exist_ok=True)
    with isolated_directory() as directory:
        temp=Path(directory);script=temp/'server.py';script.write_text(CHILD,encoding='utf-8')
        with (temp/'server.log').open('w',encoding='utf-8') as log:
            proc=subprocess.Popen([str(ROOT/'.venv/Scripts/python.exe'),'-u',str(script),str(ROOT),str(temp)],
                stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                deadline=time.monotonic()+30
                while not (temp/'ready.json').exists():
                    if proc.poll() is not None or time.monotonic()>deadline:
                        raise RuntimeError((temp/'server.log').read_text(encoding='utf-8'))
                    time.sleep(.15)
                ready=json.loads((temp/'ready.json').read_text(encoding='utf-8'))
                formal=temp/'cases'/ready['case_id']/'segment/mask.npy'
                before=hashlib.sha256(formal.read_bytes()).hexdigest()
                errors=[];slices=[]
                with sync_playwright() as p:
                    browser=p.chromium.launch(headless=True)
                    page=browser.new_page(viewport={'width':1440,'height':1000})
                    page.on('pageerror',lambda err:errors.append(str(err)))
                    page.on('response',lambda response:slices.append((response.url,response.status)) if '/segment/models/' in response.url and '/slice/' in response.url else None)
                    page.goto(f"http://127.0.0.1:{ready['port']}/#work",wait_until='networkidle')
                    page.select_option('#case-select',ready['case_id'])
                    learning=page.locator('#seg-learning-panel')
                    expect(learning).to_be_visible(timeout=20000)
                    learning.locator(':scope > summary').click()
                    expect(page.locator('#seg-model-status')).to_contain_text('本机模型已就绪')
                    page.locator('#seg-model-run').click()
                    expect(page.locator('#seg-model-cancel')).to_be_visible()
                    expect(page.locator('#seg-model-status')).to_contain_text('候选完成',timeout=20000)
                    expect(page.locator('#seg-model-result')).to_contain_text('差异不代表对错')
                    for mode in ('baseline','candidate','difference'):
                        page.select_option('#seg-model-mode',mode)
                        page.wait_for_function("Array.from(document.querySelectorAll('.slice-loading')).every(el=>el.hidden)")
                        page.wait_for_timeout(150)
                    page.locator('#seg-model-jump').click()
                    page.wait_for_function("Array.from(document.querySelectorAll('.slice-loading')).every(el=>el.hidden)")
                    for mode in ('baseline','candidate','difference'):
                        for view in ('axial','coronal','sagittal'):
                            assert any('/slice/'+view in url and 'mode='+mode in url and status==200 for url,status in slices),(mode,view,slices)
                    page.locator('#viewer-views').screenshot(path=str(OUTPUT/'synthetic-three-planes.png'))
                    page.locator('#seg-model-panel').screenshot(path=str(OUTPUT/'desktop.png'))
                    page.set_viewport_size({'width':390,'height':844})
                    panel=page.locator('#seg-model-panel');panel.scroll_into_view_if_needed()
                    assert panel.evaluate('(el)=>el.scrollWidth<=el.clientWidth+1'),'model panel overflows at 390px'
                    panel.screenshot(path=str(OUTPUT/'mobile.png'))
                    page.select_option('#seg-model-mode','current')
                    page.locator('#seg-model-run').click()
                    expect(page.locator('#seg-model-cancel')).to_be_visible()
                    page.locator('#seg-model-cancel').click()
                    expect(page.locator('#seg-model-status')).to_contain_text('已取消',timeout=15000)
                    assert not errors,errors
                    browser.close()
                assert hashlib.sha256(formal.read_bytes()).hexdigest()==before
                result={'status':'passed','synthetic_only':True,'worker':'deterministic_test_fixture',
                    'production_changed':False,'checks':['candidate submission and real status polling',
                    'all three CT planes and three preview modes return PNG','difference jump',
                    '390px panel layout','cancel','formal mask unchanged','no page errors']}
            finally:
                if proc.poll() is None:
                    subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False)
                proc.wait(timeout=15)
    (OUTPUT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False));print(str(OUTPUT))


if __name__=='__main__':main()
