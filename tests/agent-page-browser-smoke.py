"""Real browser and batch API acceptance; generated fixtures in an isolated server only."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path(tempfile.gettempdir()) / 'windcatcher-agent-page-20261106'
CHILD = r'''
import json, os, sys, socket
from pathlib import Path
root, temp = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(root))
for name, folder in [('WINDCATCHER_MONITOR_DIR','monitor'),('WINDCATCHER_AUTOMATION_DIR','automation'),
                     ('WINDCATCHER_LEARNING_DIR','learning'),('WINDCATCHER_IDENTITY_DIR','identity')]:
    os.environ[name] = str(temp / folder)
os.environ['WINDCATCHER_MULTIUSER'] = '0'
from pipeline import config, cases, ai_report, capabilities
config.CASES_DIR = cases.CASES_DIR = temp / 'cases'
ai_report.CONFIG_PATH = temp / 'ai-report.json'
capabilities.probe_capabilities = lambda: {'groups': []}
from pipeline.volume import Volume, save_volume
import numpy as np
ids=[]
for index in range(2):
    # Test-generated data, flagged selectable ONLY inside this isolated fixture store.
    cid = cases.create_case('synthetic', '<generated browser fixture>', synthetic=False)
    ids.append(cid)
    shape=(24,48,48)
    mask=np.zeros(shape,dtype=np.uint8)
    mask[4:20,14:34,18:30]=1
    hu=np.full(shape,40,dtype=np.float32)
    hu[mask>0]=-850
    save_volume(cid,Volume(hu,(1.,1.,1.),(0.,0.,0.),((0.,0.,1.),(0.,1.,0.),(1.,0.,0.)),{'shape':list(shape)}))
    folder=cases.case_dir(cid)
    np.save(folder/'segment/mask.npy',mask)
    np.save(folder/'segment/candidate_mask.npy',mask)
    segmentation={'mask_version':1,'params':{'ap_axis':1,'hu_high':-300},
        'need_human_confirm':index != 0,'metrics':{}}
    if index == 0:
        # Isolated UI fixture: confirmed mask, to exercise the automatic primary action.
        segmentation['external_air_validation']={'mask_version':1,'body_hu_threshold':-300,
            'outside_voxels':0,'candidate_outside_voxels':0}
    (folder/'segment/segmentation.json').write_text(json.dumps(segmentation),encoding='utf-8')
    for stage in ('import','anonymize','qc'): cases.update_stage(cid,stage,'ok')
    cases.update_stage(cid,'segment','ok' if index == 0 else 'warn')
import server, uvicorn
from pipeline import batch_jobs, orchestration_batch
server.CASES_DIR = cases.CASES_DIR
authorize=lambda actor,cid: actor=='local-operator' and cid in ids
queue=batch_jobs.BatchQueue(temp/'automation/orchestration.sqlite3',
    lambda item:orchestration_batch.preflight(item,authorize),
    lambda item,progress:orchestration_batch.execute(item,authorize,progress),
    snapshot=lambda item:orchestration_batch.submission_snapshot(item,authorize),execute_with_progress=True)
server.app.state.orchestration_queue=queue
queue.start()
sock=socket.socket()
sock.bind(('127.0.0.1',0))
(temp/'ready.json').write_text(json.dumps({'port':sock.getsockname()[1],
    'case_ids':sorted(ids),'confirmed_case_id':ids[0]}),encoding='utf-8')
try:
    uvicorn.Server(uvicorn.Config(server.app,host='127.0.0.1',lifespan='off',log_level='error')).run(sockets=[sock])
finally: queue.stop()
'''


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='windcatcher-agent-ui-') as directory:
        temp=Path(directory)
        script=temp/'server.py'
        script.write_text(CHILD,encoding='utf-8')
        with (temp/'server.log').open('w',encoding='utf-8') as log:
            proc=subprocess.Popen([str(ROOT/'.venv/Scripts/python.exe'),'-u',str(script),str(ROOT),str(temp)],
                stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                deadline=time.monotonic()+25
                while not (temp/'ready.json').exists():
                    if proc.poll() is not None or time.monotonic()>deadline:
                        raise RuntimeError((temp/'server.log').read_text(encoding='utf-8'))
                    time.sleep(.15)
                ready=json.loads((temp/'ready.json').read_text(encoding='utf-8'))
                errors=[]
                with sync_playwright() as p:
                    browser=p.chromium.launch(headless=True)
                    page=browser.new_page(viewport={'width':1440,'height':1000})
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    page.goto(f"http://127.0.0.1:{ready['port']}/#work",wait_until='networkidle')
                    expect(page.locator('#case-select option')).to_have_count(3,timeout=20000)
                    first,second=ready['case_ids']
                    page.select_option('#case-select',ready['confirmed_case_id'])
                    page.wait_for_function('state.case?.case_id === state.caseId && state.ctViewer.meta !== null',timeout=20000)
                    expect(page.locator('#quick-preview-primary')).to_have_text('自动处理当前病例')
                    page.locator('#quick-preview-primary').click()
                    expect(page.locator('#page-agent')).to_be_visible()
                    assert not page.locator('#advanced-workflow-panel').evaluate('(el)=>el.open')
                    expect(page.locator('#orchestration-selected-count')).to_have_text('已选 1 例')
                    expect(page.locator('#orchestration-results li')).to_have_count(1,timeout=20000)
                    expect(page.locator('#orchestration-status')).to_contain_text('本轮结束',timeout=20000)
                    # Reset only this browser session before exercising the general batch controls below.
                    page.evaluate('sessionStorage.clear()')
                    page.goto(f"http://127.0.0.1:{ready['port']}/?smoke_reset=1#work",wait_until='networkidle')
                    page.select_option('#case-select',first)
                    page.wait_for_function('state.case?.case_id === state.caseId && state.ctViewer.meta !== null',timeout=20000)
                    page.locator('#quick-preview-open-advanced').click()
                    expect(page.locator('#history-panel')).to_be_visible()
                    expect(page.locator('#history-list')).to_be_visible()
                    expect(page.locator('#btn-history-toggle')).to_have_attribute('aria-expanded','true')
                    page.locator('#btn-history-toggle').click()
                    expect(page.locator('#history-list')).to_be_hidden()
                    expect(page.locator('#history-count')).to_have_text('2 条')
                    expect(page.locator('#btn-history-toggle')).to_have_attribute('aria-expanded','false')
                    page.locator('#btn-history-toggle').click()
                    expect(page.locator('#history-list')).to_be_visible()
                    # A seeded unsaved draft exercises the actual routing and dialog guard.
                    page.evaluate('state.endplanes.dirty = true')
                    page.locator('[data-route="agent"]').click()
                    expect(page.locator('#page-agent')).to_be_visible()
                    expect(page.locator('#orchestration-cases input')).to_have_count(2)
                    assert page.evaluate('state.endplanes.dirty')
                    expect(page.locator('#page-work #orchestration-panel')).to_have_count(0)
                    expect(page.locator('[data-route="agent"]')).to_have_attribute('aria-current','page')
                    expect(page.locator('#orchestration-start')).to_be_disabled()
                    page.locator('#orchestration-current').click()
                    expect(page.locator('#orchestration-selected-count')).to_have_text('已选 1 例')
                    page.locator('[data-route="work"]').click()
                    assert page.evaluate('state.endplanes.dirty')
                    page.locator('[data-route="agent"]').click()
                    expect(page.locator('#orchestration-selected-count')).to_have_text('已选 1 例')
                    page.locator('#orchestration-clear').click()
                    expect(page.locator('#orchestration-start')).to_be_disabled()
                    page.locator('#orchestration-select-all').click()
                    expect(page.locator('#orchestration-selected-count')).to_have_text('已选 2 例')
                    assert not page.locator('#orchestration-settings').evaluate('(el)=>el.open')
                    page.locator('#orchestration-start').click()
                    expect(page.locator('#orchestration-status')).to_contain_text('本轮结束',timeout=20000)
                    expect(page.locator('#orchestration-status')).to_contain_text('暂停 2 例')
                    expect(page.locator('#orchestration-progress')).to_have_attribute('value','2')
                    expect(page.locator('#orchestration-results li')).to_have_count(2)
                    page.locator('#orchestration-detail-'+first).click()
                    page.locator('#orchestration-refresh').click()
                    expect(page.locator('#orchestration-refresh')).to_be_enabled()
                    assert page.locator('#orchestration-detail-'+first).evaluate('(el)=>el.parentElement.open')
                    page.once('dialog',lambda dialog:dialog.dismiss())
                    page.locator('#orchestration-open-'+second).click()
                    expect(page.locator('#page-agent')).to_be_visible()
                    assert page.evaluate('state.endplanes.dirty')
                    assert page.evaluate('state.caseId')==first
                    page.locator('#orchestration-open-'+first).click()
                    expect(page.locator('#page-work')).to_be_visible()
                    assert page.locator('#advanced-workflow-panel').evaluate('(el)=>el.open')
                    expect(page.locator('#viewer-card')).to_be_visible()
                    assert page.evaluate('state.endplanes.dirty')
                    page.evaluate('state.endplanes.dirty = false')
                    page.locator('[data-route="agent"]').click()
                    page.locator('#orchestration-open-'+second).click()
                    expect(page.locator('#page-work')).to_be_visible(timeout=20000)
                    expect(page.locator('#case-select')).to_have_value(second)
                    page.locator('[data-route="agent"]').click()
                    expect(page.locator('#orchestration-results li')).to_have_count(2)
                    page.screenshot(path=str(OUTPUT/'desktop.png'),full_page=True)
                    page.reload(wait_until='networkidle')
                    expect(page.locator('#orchestration-status')).to_contain_text('暂停 2 例')
                    expect(page.locator('#orchestration-cases input')).to_have_count(2)
                    page.set_viewport_size({'width':390,'height':844})
                    page.evaluate('window.scrollTo(0,0)')
                    page.locator('[data-route="work"]').click()
                    page.evaluate('localStorage.removeItem("windcatcher:history-drawer")')
                    page.reload(wait_until='networkidle')
                    expect(page.locator('#history-list')).to_be_hidden()
                    page.locator('[data-route="agent"]').click()
                    expect(page.locator('[data-route="agent"] .label-text')).to_be_visible()
                    assert page.locator('#page-agent').evaluate('(el)=>el.scrollWidth<=el.clientWidth+1'),'agent page overflow'
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),'document overflow'
                    page.screenshot(path=str(OUTPUT/'mobile.png'),full_page=True)
                    page.locator('#orchestration-select-all').focus()
                    page.keyboard.press('Enter')
                    expect(page.locator('#orchestration-selected-count')).to_have_text('已选 2 例')
                    assert not errors,errors
                    browser.close()
                result={'status':'passed','fixture_data_only':True,'production_changed':False,'llm_or_cfd_run':False,
                    'checks':['primary action submits current case','separate sidebar route','automatic case list','select/current/clear/all',
                        'draft survives navigation','actual batch API stops at two review gates',
                        'review link exact case and cancel draft discard','expanded details persist',
                        'reload restores batch','390px no overflow','keyboard selection','no page errors']}
                (OUTPUT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps(result,ensure_ascii=False))
                print(str(OUTPUT))
            except Exception:
                if 'page' in locals() and not page.is_closed():
                    try: page.screenshot(path=str(OUTPUT/'failed.png'),full_page=True)
                    except Exception: pass
                print((temp/'server.log').read_text(encoding='utf-8')[-2500:])
                raise
            finally:
                if os.name=='nt' and proc.poll() is None:
                    subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],
                        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False)
                elif proc.poll() is None: proc.terminate()
                try: proc.wait(timeout=10)
                except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=5)
                # Windows can release the inherited server.log handle just after taskkill returns.
                if os.name == 'nt': time.sleep(.3)


if __name__=='__main__': main()
