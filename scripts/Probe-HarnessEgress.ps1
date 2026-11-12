param([string]$Image = 'windcatcher-harness-worker:f3803c9')

# Synthetic, offline probe. Creates only a private network and a no-egress
# 403/502 proxy. Never reads case data, real credentials, or production config.
$ErrorActionPreference = 'Stop'
$imageId = (& docker image inspect $Image --format '{{.Id}}' | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $imageId -notmatch '^sha256:[0-9a-f]{64}$') {
    throw 'Harness image missing or immutable ID invalid'
}
$suffix = [guid]::NewGuid().ToString('N').Substring(0, 8)
$network = "windcatcher-harness-probe-$suffix"
$proxy = "windcatcher-harness-proxy-probe-$suffix"
$networkCreated = $false
$proxyStarted = $false
$proxyJs = @'
const http=require("http");
const server=http.createServer((request,response)=>{
  console.log("DENY_HTTP");response.writeHead(403);response.end();
});
server.on("connect",(request,socket)=>{
  const allow=request.url==="api.deepseek.com:443"&&request.headers.host==="api.deepseek.com";
  console.log(allow?"ALLOW_PROBE_CONNECT":"DENY_CONNECT");
  socket.write(allow?"HTTP/1.1 502 Bad Gateway\r\n\r\n":"HTTP/1.1 403 Forbidden\r\n\r\n");socket.end();
});
server.listen(3128,"0.0.0.0",()=>console.log("READY"));
'@
$directJs = @'
const net=require("net");
const targets=[["public","1.1.1.1",443],["host","__HOSTIP__",8765],
 ["lan","192.168.1.1",443],["official","api.deepseek.com",443],
 ["host-name","host.docker.internal",8765]];
Promise.all(targets.map(([name,host,port])=>new Promise(resolve=>{
 const s=net.connect({host,port,timeout:2000});let done=false;
 const end=x=>{if(done)return;done=true;s.destroy();console.log(name+"="+x);resolve(x!=="CONNECTED")};
 s.on("connect",()=>end("CONNECTED"));s.on("error",e=>end(e.code||"ERROR"));
 s.on("timeout",()=>end("TIMEOUT"));
}))).then(xs=>{console.log("all_blocked="+xs.every(Boolean));if(!xs.every(Boolean))process.exitCode=1});
'@
$proxyTestJs = @'
const net=require("net"),name="__PROXY__";
const cases=[
 ["official","api.deepseek.com:443","api.deepseek.com",502],
 ["other","example.com:443","example.com",403],
 ["ip","127.0.0.1:443","127.0.0.1",403],
 ["ipv6","[::1]:443","[::1]",403],
 ["case","API.DEEPSEEK.COM:443","API.DEEPSEEK.COM",403],
 ["dot","api.deepseek.com.:443","api.deepseek.com.",403],
 ["userinfo","x@api.deepseek.com:443","api.deepseek.com",403],
 ["port","api.deepseek.com:80","api.deepseek.com",403],
 ["host","api.deepseek.com:443","example.com",403]
];
cases.push(["http","GET https://api.deepseek.com/models","api.deepseek.com",403]);
Promise.all(cases.map(([label,authority,host,want])=>new Promise(resolve=>{
 let out="";const method=label==="http"?"":"CONNECT ";
 const req=method+authority+" HTTP/1.1\r\nHost: "+host+"\r\n\r\n";
 const s=net.connect(3128,name,()=>s.write(req));s.setTimeout(3000);
 s.on("data",chunk=>{out+=chunk.toString();if(out.includes("\r\n")){
  const m=/^HTTP\/1\.1 (\d+)/.exec(out),got=m?Number(m[1]):0;
  console.log(label+"="+got);s.destroy();resolve(got===want);
 }});
 s.on("error",e=>{console.log(label+"="+e.code);resolve(false)});
 s.on("timeout",()=>{console.log(label+"=TIMEOUT");s.destroy();resolve(false)});
}))).then(xs=>{console.log("all_pass="+xs.every(Boolean));if(!xs.every(Boolean))process.exitCode=1});
'@
try {
    $networkArgs = @('network','create','--driver','bridge','--internal','--opt',
        'com.docker.network.bridge.gateway_mode_ipv4=isolated',$network)
    & docker @networkArgs | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not create isolated network' }
    $networkCreated = $true
    $internal = (& docker network inspect $network --format '{{.Internal}}' | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $internal -ne 'true') { throw 'Network is not internal' }

    $proxyArgs = @('run','-d','--rm','--name',$proxy,'--network',$network,
        '--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
        '--pids-limit','32','--memory','256m','--cpus','0.5',
        '--entrypoint','node',$imageId,'-e',$proxyJs)
    & docker @proxyArgs | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not start probe proxy' }
    $proxyStarted = $true
    Start-Sleep -Milliseconds 500
    $published = (& docker port $proxy | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $published) { throw 'Probe proxy published host port' }

    $hostJs = 'require("dns").lookup("host.docker.internal",(e,a)=>{if(e)process.exit(2);console.log(a)})'
    $hostIp = (& docker run --rm --network bridge --entrypoint node $imageId -e $hostJs | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $hostIp -notmatch '^\d{1,3}(\.\d{1,3}){3}$') {
        throw 'Cannot resolve Docker Desktop host address'
    }
    $nodeArgs = @('run','--rm','--network',$network,'--read-only',
        '--cap-drop','ALL','--security-opt','no-new-privileges',
        '--pids-limit','64','--memory','512m','--cpus','1',
        '--entrypoint','node',$imageId,'-e')
    & docker @nodeArgs ($directJs.Replace('__HOSTIP__',$hostIp))
    if ($LASTEXITCODE -ne 0) { throw 'Worker bypassed internal network' }
    & docker @nodeArgs ($proxyTestJs.Replace('__PROXY__',$proxy))
    if ($LASTEXITCODE -ne 0) { throw 'Probe proxy allowlist failed' }

    $stages = @{}
    @('import','anonymize','qc','segment','surface','cfd_prep','cfd_run','report') |
        ForEach-Object { $stages[$_] = 'pending' }
    $summary = @{schema_version=1;case_id='synthetic-egress-probe';
        state_sha256=('a' * 64);stages=$stages;baseline_ready=$false;
        numeric_ok=$null;eligible=$false}
    $payload = @{summary=$summary;api_key='synthetic-probe-key';
        model='deepseek-v4-flash'} | ConvertTo-Json -Depth 8 -Compress
    $workerArgs = @('run','--rm','-i','--network',$network,'--read-only',
        '--cap-drop','ALL','--security-opt','no-new-privileges',
        '--pids-limit','64','--memory','512m','--memory-swap','512m','--cpus','1',
        '--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=1777',
        '--tmpfs','/home/harness:rw,noexec,nosuid,nodev,size=32m,uid=10001,gid=10001,mode=700',
        '--workdir','/tmp','--env',"HTTPS_PROXY=http://$($proxy):3128",
        '--env',"HTTP_PROXY=http://$($proxy):3128",$imageId)
    $payload | & docker @workerArgs
    if ($LASTEXITCODE -eq 0) { throw 'Fake key unexpectedly produced model output' }
    $probeLog = & docker logs $proxy | Out-String
    if ($LASTEXITCODE -ne 0 -or $probeLog -notmatch 'ALLOW_PROBE_CONNECT') {
        throw 'Official SDK did not use the explicit proxy'
    }
    Write-Output 'SDK used the proxy; probe returned 502 without external access.'
}
finally {
    if ($proxyStarted) { & docker rm -f $proxy | Out-Null }
    if ($networkCreated) { & docker network rm $network | Out-Null }
}
