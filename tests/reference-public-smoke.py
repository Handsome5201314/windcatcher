"""Read-only HTTPS and browser acceptance for the deployed reference library."""
import hashlib,importlib.util,json
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlencode,urlsplit

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('gateway_smoke',ROOT/'scripts/Smoke-PublicGateway.py')
gateway=importlib.util.module_from_spec(spec);spec.loader.exec_module(gateway)
def main():
 public=gateway.read_env(ROOT/'.env')
 infra=gateway.read_env(Path(public['WINDCATCHER_INFRA_ENV']))
 origin=public['WINDCATCHER_PUBLIC_URL'].rstrip('/')
 host=urlsplit(origin).hostname
 def probe(path,method='GET',body=None,headers=None):
  connection=gateway.PinnedHttps(host,infra['ALIBABA_SERVER_IP'])
  try:
   connection.request(method,path,body=body,headers={'Host':host,**(headers or {})})
   response=connection.getresponse()
   return response.status,response.getheader('Set-Cookie'),response.read()
  finally:connection.close()
 form=urlencode(dict(username=public['WINDCATCHER_PUBLIC_USERNAME'],password=public['WINDCATCHER_PUBLIC_PASSWORD'])).encode()
 code,cookie,_=probe('/login','POST',form,{'Origin':origin,'Content-Type':'application/x-www-form-urlencoded'})
 if code!=303 or not cookie:raise RuntimeError('Research authentication did not succeed')
 auth={'Cookie':cookie.split(';',1)[0]}
 code,_,data=probe('/api/reference-admin/cases',headers=auth)
 if code!=200:raise RuntimeError('Reference admin API did not return 200')
 candidates=json.loads(data)['items']
 assert len(candidates)==4 and all(item['surface_ready'] for item in candidates)
 assert probe('/api/reference-admin/cases')[0]==401
 code,_,data=probe('/api/references')
 assert code==200
 catalog=json.loads(data)
 assert probe('/references')[0]==200
 for name in ['app.css','patient-view.js','patient-view.css','reference-view.js','cfd-viewer.js']:
  code,_,data=probe('/static/'+name)
  assert code==200 and hashlib.sha256(data).digest()==hashlib.sha256((ROOT/'static'/name).read_bytes()).digest(),name
 code,_,data=probe('/static/reference-admin.js',headers=auth)
 assert code==200 and hashlib.sha256(data).digest()==hashlib.sha256((ROOT/'static/reference-admin.js').read_bytes()).digest()
 from playwright.sync_api import sync_playwright,expect
 with sync_playwright() as p:
  browser=p.chromium.launch(headless=True,args=['--no-proxy-server','--host-resolver-rules=MAP '+host+' '+infra['ALIBABA_SERVER_IP']])
  context=browser.new_context(viewport=dict(width=390,height=844),reduced_motion='reduce')
  jar=SimpleCookie();jar.load(cookie)
  context.add_cookies([dict(name=m.key,value=m.value,domain=host,path='/',secure=True,httpOnly=True,sameSite='Lax') for m in jar.values()])
  page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
  page.goto(origin+'/#sample',wait_until='domcontentloaded')
  expect(page.locator('#ref-admin-case option')).to_have_count(5)
  expect(page.locator('#ref-admin-status')).to_contain_text('4 个工作台病例')
  expect(page.locator('#ref-admin-form')).to_be_visible()
  expect(page.locator('#ref-admin-kind option[value="flow_test"]')).to_have_js_property('disabled',False)
  page.locator('#ref-admin-kind').select_option('flow_test')
  expect(page.locator('#ref-admin-test-confirmation')).to_be_visible()
  expect(page.locator('#ref-admin-test-confirmation')).to_contain_text('无需登录')
  assert page.locator('#page-sample h1').bounding_box()['height']<40
  assert page.evaluate('document.documentElement.scrollWidth')<=390
  assert page.locator('#ref-admin-tags input').count()==8
  out=ROOT/'.local-public/reference-ui-smoke';out.mkdir(parents=True,exist_ok=True)
  page.locator('#page-sample').screenshot(path=str(out/'deployed-admin-390.png'))
  page.goto(origin+'/references')
  expect(page.locator('h1')).to_have_text('参考病例库')
  if not catalog['items']:expect(page.locator('[data-role="count"]')).to_contain_text('暂未发布')
  assert page.evaluate('document.documentElement.scrollWidth')<=390
  assert not errors,errors
  browser.close()
 print(json.dumps(dict(https=True,assets_match=True,admin_candidates=len(candidates),
                       structure_ready=sum(item['surface_ready'] for item in candidates),
                       published=len(catalog['items']),browser_errors=errors),ensure_ascii=False))
if __name__=='__main__':main()
