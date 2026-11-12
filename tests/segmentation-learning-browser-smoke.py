"""Browser acceptance against real APIs and isolated synthetic data, never production."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = Path(os.environ.get('WINDCATCHER_SMOKE_OUTPUT', str(Path(tempfile.gettempdir()) / 'windcatcher-segmentation-learning-20261106')))
CHILD = r'''
import json, os, sys, socket
from pathlib import Path
root, temp = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(root))
for name, folder in [('WINDCATCHER_MONITOR_DIR','monitor'),('WINDCATCHER_AUTOMATION_DIR','automation'),
                     ('WINDCATCHER_LEARNING_DIR','learning'),('WINDCATCHER_IDENTITY_DIR','identity')]:
    os.environ[name] = str(temp / folder)
os.environ['WINDCATCHER_MULTIUSER'] = '0'
from pipeline import config, cases, mask_review, ai_report
config.CASES_DIR = cases.CASES_DIR = temp / 'cases'
ai_report.CONFIG_PATH = temp / 'ai-report.json'
from pipeline.volume import Volume, save_volume
import numpy as np
cid = cases.create_case('synthetic', '<synthetic browser acceptance>', synthetic=True)
shape=(24,48,48)
mask=np.zeros(shape,dtype=np.uint8)
mask[4:20,14:34,18:30]=1
mask[2,3,4]=1
hu=np.full(shape,40,dtype=np.float32)
hu[mask>0]=-850
save_volume(cid,Volume(hu,(1.,1.,1.),(0.,0.,0.),((0.,0.,1.),(0.,1.,0.),(1.,0.,0.)),{'shape':list(shape)}))
folder=cases.case_dir(cid)
np.save(folder/'segment/mask.npy',mask)
np.save(folder/'segment/candidate_mask.npy',mask)
(folder/'segment/segmentation.json').write_text(json.dumps({'mask_version':1,'params':{'ap_axis':1,'hu_high':-300},
    'need_human_confirm':True,'metrics':{}}),encoding='utf-8')
mask_review.edit_mask(cid,'axial',2,'erase',[{'x':4,'y':3}],0)
for stage in ('import','anonymize','qc'): cases.update_stage(cid,stage,'ok')
import server, uvicorn
server.CASES_DIR = cases.CASES_DIR
sock=socket.socket()
sock.bind(('127.0.0.1',0))
(temp/'ready.json').write_text(json.dumps({'port':sock.getsockname()[1],'case_id':cid}),encoding='utf-8')
uvicorn.Server(uvicorn.Config(server.app,host='127.0.0.1',lifespan='off',log_level='error')).run(sockets=[sock])
'''


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='windcatcher-learning-ui-') as directory:
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
                    page.select_option('#case-select',ready['case_id'])
                    panel=page.locator('#seg-learning-panel')
                    try:
                        expect(panel).to_be_visible(timeout=20000)
                    except AssertionError:
                        print(json.dumps({'page_errors':errors,'visible_text':page.locator('body').inner_text()[:1800]},ensure_ascii=False))
                        page.screenshot(path=str(OUTPUT/'failed.png'))
                        raise
                    panel.locator(':scope > summary').click()
                    expect(page.locator('#seg-learning-status')).to_contain_text('已有 1 条修正记录')
                    page.select_option('#seg-learning-strategy','remove_tiny_islands_v1')
                    page.locator('#seg-learning-run').click()
                    expect(page.locator('#seg-learning-result')).to_contain_text('尚无已审核参考')
                    panel.locator('details > summary').click()
                    page.locator('#seg-learning-subject').fill('subject-synthetic-001')
                    page.locator('#seg-learning-checked').check()
                    page.locator('#seg-learning-authorized').check()
                    page.locator('#seg-learning-approve').click()
                    expect(page.locator('#seg-learning-status')).to_contain_text('已登记参考')
                    expect(page.locator('#seg-learning-select option:checked')).to_contain_text('已核对参考')
                    page.locator('#seg-learning-run').click()
                    expect(page.locator('#seg-learning-result')).to_contain_text('误分：1 → 0')
                    expect(page.locator('#seg-learning-result')).to_contain_text('漏分：0 → 0')
                    panel.screenshot(path=str(OUTPUT/'desktop.png'))
                    page.set_viewport_size({'width':390,'height':844})
                    panel.scroll_into_view_if_needed()
                    assert panel.evaluate('(el)=>el.scrollWidth<=el.clientWidth+1'),'learning panel overflows on mobile'
                    panel.screenshot(path=str(OUTPUT/'mobile.png'))
                    assert not errors, errors
                    browser.close()
                result={'status':'passed','synthetic_only':True,'production_changed':False,
                    'checks':['real UI selection','real candidate API','explicit synthetic reference',
                        'FP 1 to 0 / FN 0 to 0','390px no panel overflow','no page errors']}
                (OUTPUT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps(result,ensure_ascii=False))
                print(str(OUTPUT))
            finally:
                if os.name == 'nt' and proc.poll() is None:
                    subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],
                        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False)
                elif proc.poll() is None:
                    proc.terminate()
                try: proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait(timeout=5)


if __name__=='__main__': main()
