"""Synthetic HTTP fixtures; real mobile WebGL, comparison and admin interactions."""
import importlib.util,json,re,threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'.local-public/reference-ui-smoke'
spec=importlib.util.spec_from_file_location('smoke',ROOT/'tests/patient-view-browser-smoke.py')
smoke=importlib.util.module_from_spec(spec);spec.loader.exec_module(smoke)
MODEL=smoke.synthetic_model()
ANATOMY_MODEL=None
IDS=['a'*32,'b'*32,'c'*32,'d'*32]
COND=dict(solver='simpleFoam',turbulence_model='laminar',inlet_mode='flow',total_flow_lpm=15,nu=1.5e-5,rho_kg_m3=1.2)
TAGS={'septal_deviation':dict(label='鼻中隔偏曲',group='结构特征')}
ITEMS=[dict(id=id,title='验收参考 '+str(i+1),tags=['septal_deviation'],description='合成模型，仅用于验收',
 kind='anatomy' if i==0 else 'flow_test' if i==3 else 'flow',flow_verified=i in (1,2),
 warning='测试气流 · 未复核，仅供可视化测试' if i==3 else '',
 conditions=COND if i>0 else {},version='test-version') for i,id in enumerate(IDS)]
STATE=dict(empty=False,failure=False,withdrawn=False,post_failure=False,saved={},posts=[],cancelled_requests=0)
CAPTURE="""Object.defineProperty(window,'CfdViewer',{configurable:true,set(V){
 Object.defineProperty(window,'CfdViewer',{configurable:true,writable:true,value:new Proxy(V,{
 construct(t,a){const v=Reflect.construct(t,a);(window.__qaViewers ||= []).push(v);return v;}})});}});"""
FRAME="""()=>window.__qaViewers.filter(v=>!v.disposed).map(v=>{
v.camera.updateMatrixWorld();const p=v.frameCenter.clone().project(v.camera);
return {x:p.x,y:p.y,a:v.getPresentationState().azimuth,lines:v.lineGroup.visible,particles:v.particles.visible,colors:v.wall.material.vertexColors};})"""
class Handler(BaseHTTPRequestHandler):
 def send(self,value,code=200,kind='application/json'):
  data=value if isinstance(value,bytes) else json.dumps(value).encode()
  self.send_response(code);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(data)));self.end_headers()
  try:self.wfile.write(data)
  except (BrokenPipeError,ConnectionAbortedError,ConnectionResetError):
   # Selecting another case deliberately aborts the previous model request.
   STATE['cancelled_requests']+=1
 def do_GET(self):
  path=urlsplit(self.path).path
  if path=='/references': return self.send((ROOT/'deploy/reference-view.html').read_bytes(),kind='text/html; charset=utf-8')
  if path=='/qa/patient':
   return self.send(re.sub(r'<!--SHARE_[A-Z_]+-->','',(ROOT/'deploy/patient-view.html').read_text(encoding='utf-8')).encode(),kind='text/html; charset=utf-8')
  if path=='/qa/patient/model': return self.send(MODEL)
  if path=='/qa/patient/metadata':return self.send(dict(kind='flow_test',flow_verified=False,warning='测试气流 · 未复核',conditions=COND))
  if path=='/qa/admin':
   html=(ROOT/'index.html').read_text(encoding='utf-8')
   html=html[html.index('      <!-- ============ 参考病例'):html.index('    </main>')].replace('class="page"','class="page is-visible"',1)
   html='<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/app.css"></head><body><main>'+html+'</main><script>window.WindcatcherCloudAccess=true;window.WindcatcherActivityAccess=true;</script><script src="/static/reference-admin.js"></script><script>WindcatcherReferenceAdmin.activate();</script></body></html>'
   return self.send(html.encode(),kind='text/html; charset=utf-8')
  if path=='/api/reference-admin/cases':
   return self.send(dict(items=[dict(case_id='case-test-'+str(i),surface_ready=True,cfd_status='warn',entry=STATE['saved'].get('case-test-'+str(i))) for i in range(1,5)],tags=TAGS))
  if path=='/api/references':
   if STATE['failure']:return self.send(dict(detail=dict(reason='验收：参考库暂不可用')),503)
   return self.send(dict(items=[] if STATE['empty'] else ITEMS,tags=TAGS))
  m=re.fullmatch('/api/references/([a-d]{32})/model',path)
  if m:
   if STATE['withdrawn']:return self.send(dict(detail=dict(reason='参考病例已撤回')),404)
   data=json.loads(json.dumps(MODEL))
   if m[1]==IDS[0]:
    if ANATOMY_MODEL is not None:data=json.loads(json.dumps(ANATOMY_MODEL))
    else:
     data.update(kind='anatomy',units=dict(coordinates='m'),ranges={},slices=[],streamlines=[]);data['wall'].pop('pressure')
   if m[1]==IDS[2]:
    data['ranges']['velocity']=[0,8]
    for mesh in [data['wall'],*data['streamlines']]:
     mesh['positions']=[v*2+(.5 if i%3==0 else 0) for i,v in enumerate(mesh['positions'])]
    data['bounds_m']=[v*2+(.5 if i<2 else 0) for i,v in enumerate(data['bounds_m'])]
   return self.send(data)
  if path.startswith('/static/'):
   file=ROOT/path.lstrip('/')
   if file.is_file() and file.suffix in ('.css','.js','.svg'):
    return self.send(file.read_bytes(),kind='text/css' if file.suffix=='.css' else 'text/javascript')
  self.send(dict(detail=dict(reason='Not found')),404)
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))) or b'{}')
  STATE['posts'].append(dict(path=self.path,body=body))
  if STATE['post_failure']:return self.send(dict(detail=dict(reason='验收：版本已变化，请重新核对')),409)
  if self.path.endswith('/revoke'):
   for entry in STATE['saved'].values():entry['status']='withdrawn'
   return self.send(dict(status='withdrawn'))
  key=self.path.rsplit('/',1)[-1]
  entry=dict(body,id=IDS[0],status='published' if body['action']=='publish' else 'draft')
  STATE['saved'][key]=entry;self.send(entry)
 def log_message(self,*_):pass
def run():
 OUT.mkdir(parents=True,exist_ok=True);server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
 worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
 base='http://127.0.0.1:'+str(server.server_port);report=[]
 try:
  with sync_playwright() as p:
   browser=p.chromium.launch(headless=True)
   for width,height in [(320,740),(390,844),(844,390),(1440,1000)]:
    context=browser.new_context(viewport=dict(width=width,height=height),reduced_motion='reduce')
    page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.add_init_script(CAPTURE)
    page.goto(base+'/references');expect(page.locator('[data-role="left-model"] canvas')).to_be_attached()
    expect(page.locator('[data-role="mode"] option[value="flow"]')).to_have_js_property('disabled',True)
    expect(page.locator('[data-role="policy"]')).to_contain_text('仅发布结构')
    page.locator('[data-role="left"]').select_option(IDS[1])
    expect(page.locator('[data-role="mode"] option[value="flow"]')).to_have_js_property('disabled',False)
    page.locator('[data-role="mode"]').select_option('flow')
    expect(page.locator('[data-role="legend"] h3')).to_contain_text('本模型')
    expect(page.locator('[data-role="range"]')).to_contain_text('本模型范围')
    single=page.evaluate(FRAME)
    assert len(single)==1 and single[0]['lines'] and single[0]['particles'],single
    page.locator('[data-role="left"]').select_option(IDS[3])
    expect(page.locator('[data-role="test-warning"]')).to_be_visible()
    expect(page.locator('[data-role="test-warning"]')).to_contain_text('未复核')
    expect(page.locator('[data-role="mode"] option[value="flow"]')).to_have_js_property('disabled',False)
    page.locator('[data-role="mode"]').select_option('flow')
    page.locator('[data-role="right"]').select_option(IDS[1])
    expect(page.locator('[data-role="policy"]')).to_contain_text('未复核测试')
    expect(page.locator('[data-role="mode"] option[value="flow"]')).to_have_js_property('disabled',False)
    page.locator('[data-role="left"]').select_option(IDS[1]);page.locator('[data-role="right"]').select_option(IDS[2])
    expect(page.locator('[data-role="right-model"] canvas')).to_be_attached()
    frames=page.evaluate(FRAME)
    assert len(frames)==2 and all(abs(v['x'])<1e-6 and abs(v['y'])<1e-6 for v in frames),frames
    assert all(not v['lines'] and not v['particles'] and not v['colors'] for v in frames),frames
    assert page.evaluate('document.documentElement.scrollWidth')<=width
    page.locator('[data-role="mode"]').select_option('flow')
    expect(page.locator('[data-role="range"]')).to_contain_text('8.00 m/s')
    page.evaluate("()=>window.__qaViewers.filter(v=>!v.disposed)[0].setPresentationPose(1.7,.3)")
    frames=page.evaluate(FRAME);assert abs(frames[0]['a']-frames[1]['a'])<1e-6,frames
    page.locator('[data-role="field"]').select_option('pressure');expect(page.locator('[data-role="range"]')).to_contain_text('Pa')
    page.screenshot(path=str(OUT/('comparison-'+str(width)+'.png')),full_page=True)
    page.locator('[data-role="right"]').select_option(IDS[0]);expect(page.locator('[data-role="policy"]')).to_contain_text('仅对照结构')
    expect(page.locator('[data-role="mode"] option[value="flow"]')).to_have_js_property('disabled',True)
    frames=page.evaluate(FRAME)
    assert len(frames)==2 and all(abs(v['x'])<1e-6 and abs(v['y'])<1e-6 for v in frames),frames
    if width==390:page.screenshot(path=str(OUT/'actual-structure-390.png'),full_page=True)
    page.goto(base+'/qa/patient');expect(page.locator('#model canvas')).to_be_attached()
    page.locator('#open-reference-comparison').click();expect(page.locator('#patient-reference-comparison')).to_be_visible()
    page.locator('[data-role="right"]').select_option(IDS[1]);expect(page.locator('[data-role="policy"]')).to_contain_text('未复核测试')
    expect(page.locator('[data-role="test-warning"]')).to_be_visible()
    expect(page.locator('[data-role="mode"] option[value="flow"]')).to_have_js_property('disabled',False)
    page.locator('[data-role="close"]').click();expect(page.locator('#model canvas')).to_be_visible()
    if width==390:
     page.goto(base+'/qa/admin');expect(page.locator('#ref-admin-status')).to_contain_text('4 个工作台病例')
     page.locator('#ref-admin-case').select_option('case-test-4')
     page.locator('#ref-admin-tags input[value="septal_deviation"]').check()
     page.locator('#ref-admin-authorization').check();page.locator('#ref-admin-labels-confirmed').check()
     page.locator('#ref-admin-reviewer').fill('验收记录人');page.locator('#ref-admin-note').fill('仅限本地界面验收，确认标签和展示授权的测试记录。')
     page.locator('#ref-admin-kind').select_option('flow_test')
     expect(page.locator('#ref-admin-test-confirmation')).to_be_visible()
     page.locator('#ref-admin-publish').click();expect(page.locator('#ref-admin-status')).to_contain_text('未复核测试')
     page.locator('#ref-admin-test-confirmation input').check()
     STATE['post_failure']=True
     page.locator('#ref-admin-publish').click();expect(page.locator('#ref-admin-status')).to_contain_text('版本已变化')
     assert STATE['posts'][-1]['body']['kind']=='flow_test' and STATE['posts'][-1]['body']['test_use_confirmed'] is True
     expect(page.locator('#ref-admin-note')).to_have_value('仅限本地界面验收，确认标签和展示授权的测试记录。')
     expect(page.locator('#ref-admin-authorization')).to_be_checked()
     STATE['post_failure']=False
     page.locator('#ref-admin-publish').click();expect(page.locator('#ref-admin-status')).to_contain_text('未复核测试气流已发布')
     assert STATE['posts'][-1]['path'].endswith('case-test-4')
     page.locator('#ref-admin-reviewer').fill('')
     page.locator('#ref-admin-revoke').click();expect(page.locator('#ref-admin-status')).to_contain_text('已撤回')
     page.screenshot(path=str(OUT/'admin-390.png'),full_page=True)
    assert not errors,errors
    report.append(dict(viewport=[width,height],checks=['center','structure','flow','sync','patient-return'],errors=errors));context.close()
   context=browser.new_context();page=context.new_page();STATE['empty']=True;page.goto(base+'/references')
   expect(page.locator('[data-role="count"]')).to_contain_text('暂未发布')
   STATE.update(empty=False,failure=True);page.locator('[data-role="retry"]').click()
   expect(page.locator('[data-role="status"]')).to_contain_text('暂不可用')
   STATE['failure']=False;page.locator('[data-role="retry"]').click()
   expect(page.locator('[data-role="left-model"] canvas')).to_be_attached()
   STATE['withdrawn']=True;page.locator('[data-role="right"]').select_option(IDS[1])
   expect(page.locator('[data-role="status"]')).to_contain_text('已撤回')
   context.close();browser.close()
 finally:server.shutdown();server.server_close();worker.join()
 (OUT/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False))
if __name__=='__main__':
 import argparse
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--anatomy-model',type=Path)
 args=parser.parse_args()
 if args.anatomy_model:ANATOMY_MODEL=json.loads(args.anatomy_model.read_text(encoding='utf-8'))
 run()
