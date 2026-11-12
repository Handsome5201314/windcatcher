param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^sha256:[0-9a-f]{64}$')]
    [string]$ProxyImageId,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^sha256:[0-9a-f]{64}$')]
    [string]$WorkerImageId,

    [switch]$ProbeOfficialTls
)

# Synthetic topology test. No case data, credentials, published ports or API calls.
$ErrorActionPreference = 'Stop'
$created = $null

$directJs = @'
const net=require('node:net');
const targets=[['public','1.1.1.1',443],['lan','192.168.1.1',443],
 ['official','api.deepseek.com',443],['host','host.docker.internal',8765],
 ['outbound','__EGRESS_IP__',3128]];
Promise.all(targets.map(([name,host,port])=>new Promise(resolve=>{
 const socket=net.connect({host,port,timeout:2000});let done=false;
 const end=result=>{if(done)return;done=true;socket.destroy();
  console.log(name+'='+result);resolve(result!=='CONNECTED')};
 socket.on('connect',()=>end('CONNECTED'));
 socket.on('error',error=>end(error.code||'ERROR'));
 socket.on('timeout',()=>end('TIMEOUT'));
}))).then(results=>{console.log('all_blocked='+results.every(Boolean));
 if(!results.every(Boolean))process.exitCode=1});
'@

$denyJs = @'
const net=require('node:net'),proxy='windcatcher-harness-proxy';
const cases=[
 ['other','CONNECT example.com:443','example.com'],
 ['ip','CONNECT 127.0.0.1:443','127.0.0.1'],
 ['case','CONNECT API.DEEPSEEK.COM:443','API.DEEPSEEK.COM'],
 ['dot','CONNECT api.deepseek.com.:443','api.deepseek.com.'],
 ['port','CONNECT api.deepseek.com:80','api.deepseek.com'],
 ['host','CONNECT api.deepseek.com:443','example.com'],
 ['http','GET https://api.deepseek.com/','api.deepseek.com']];
Promise.all(cases.map(([name,line,host])=>new Promise(resolve=>{
 const socket=net.connect(3128,proxy);socket.setTimeout(2000);let output='';
 socket.on('connect',()=>socket.write(line+' HTTP/1.1\r\nHost: '+host+'\r\n\r\n'));
 socket.on('data',part=>{output+=part.toString();if(output.includes('\r\n')){
  const status=Number(/^HTTP\/1\.1 (\d+)/.exec(output)?.[1]);
  console.log(name+'='+status);socket.destroy();resolve(status===403)}});
 socket.on('error',error=>{console.log(name+'='+error.code);resolve(false)});
 socket.on('timeout',()=>{socket.destroy();console.log(name+'=TIMEOUT');resolve(false)});
}))).then(results=>{console.log('all_denied='+results.every(Boolean));
 if(!results.every(Boolean))process.exitCode=1});
'@

$routeJs = @'
const fs=require('node:fs'),os=require('node:os');
const row=fs.readFileSync('/proc/net/route','utf8').trim().split('\n').slice(1)
 .map(line=>line.trim().split(/\s+/)).find(fields=>fields[1]==='00000000');
if(!row)process.exit(2);
const address=os.networkInterfaces()[row[0]]?.find(item=>item.family==='IPv4')?.address;
if(!address)process.exit(3);
console.log(address);
'@

$egressBindJs = @'
const net=require('node:net');
const socket=net.connect({host:'__EGRESS_IP__',port:3128,timeout:2000});
socket.on('connect',()=>{console.log('egress_bound=true');socket.destroy();process.exitCode=1});
socket.on('error',error=>console.log('egress_bound=false '+error.code));
socket.on('timeout',()=>{console.log('egress_bound=false TIMEOUT');socket.destroy()});
'@

# Optional, explicit real egress check. The key is a literal fake value and
# the request contains only synthetic text; TLS must validate the real host.
$officialJs = @'
const net=require('node:net'),tls=require('node:tls');
const socket=net.connect(3128,'windcatcher-harness-proxy');
let done=false,header=Buffer.alloc(0);
function fail(message){if(done)return;done=true;console.error(message);process.exitCode=1;socket.destroy()}
socket.setTimeout(12000,()=>fail('official_probe_timeout'));
socket.on('error',error=>fail('official_probe_socket_'+(error.code||'ERROR')));
socket.on('connect',()=>socket.write('CONNECT api.deepseek.com:443 HTTP/1.1\r\n'+
 'Host: api.deepseek.com\r\n\r\n'));
socket.on('data',function onProxyData(part){
 header=Buffer.concat([header,part]);const boundary=header.indexOf('\r\n\r\n');
 if(boundary<0){if(header.length>4096)fail('official_probe_bad_proxy_header');return}
 socket.removeListener('data',onProxyData);
 const status=Number(/^HTTP\/1\.1 (\d+)/.exec(header.toString('utf8'))?.[1]);
 if(status!==200){fail('official_probe_proxy_status_'+status);return}
 if(header.length>boundary+4)socket.unshift(header.subarray(boundary+4));
 const secure=tls.connect({socket,servername:'api.deepseek.com',rejectUnauthorized:true});
 secure.setTimeout(12000,()=>fail('official_probe_tls_timeout'));
 secure.on('error',error=>fail('official_probe_tls_'+(error.code||'ERROR')));
 secure.on('secureConnect',()=>{
  if(!secure.authorized){fail('official_probe_tls_untrusted');return}
  console.log('tls_valid=true');
  const body=JSON.stringify({model:'deepseek-chat',max_tokens:1,stream:false,
   messages:[{role:'user',content:'synthetic connectivity check'}]});
  secure.write('POST /chat/completions HTTP/1.1\r\nHost: api.deepseek.com\r\n'+
   'Authorization: Bearer synthetic-egress-test-invalid-key\r\n'+
   'Content-Type: application/json\r\nContent-Length: '+Buffer.byteLength(body)+
   '\r\nConnection: close\r\n\r\n'+body);
 });
 let response='';
 secure.on('data',part=>{
  if(done)return;response+=part.toString('utf8');
  if(response.includes('\r\n')){
   const status=Number(/^HTTP\/1\.1 (\d+)/.exec(response)?.[1]);
   console.log('api_status='+status);done=true;
   if(status!==401)process.exitCode=1;
   secure.destroy();
  }
 });
});
'@

try {
    $raw = & "$PSScriptRoot/Start-HarnessEgressProxy.ps1" -ImageId $ProxyImageId
    $created = $raw | ConvertFrom-Json
    if ($created.internal_network_id -notmatch '^[0-9a-f]{64}$' -or
        $created.proxy_alias -ne 'windcatcher-harness-proxy' -or
        $created.proxy_port -ne 3128) {
        throw 'Proxy contract mismatch'
    }
    $internal = @(docker network inspect $created.internal_network_id |
        Out-String | ConvertFrom-Json)[0]
    if ($LASTEXITCODE -ne 0 -or -not $internal.Internal -or
        $internal.Options.'com.docker.network.bridge.gateway_mode_ipv4' -ne 'isolated') {
        throw 'Internal network is not isolated'
    }
    $container = @(docker inspect $created.proxy_container_name |
        Out-String | ConvertFrom-Json)[0]
    if ($LASTEXITCODE -ne 0 -or
        $container.Config.Labels.'windcatcher.role' -ne 'harness-egress-proxy') {
        throw 'Proxy identity label missing'
    }
    $egressName = @($container.NetworkSettings.Networks.PSObject.Properties.Name |
        Where-Object { $_ -ne $created.internal_network_name })[0]
    $egressIp = $container.NetworkSettings.Networks.PSObject.Properties[$egressName].Value.IPAddress
    if (-not $egressIp) { throw 'No outbound network address' }
    $ports = & docker port $created.proxy_container_name | Out-String
    if ($LASTEXITCODE -ne 0 -or $ports.Trim()) { throw 'Host port published' }

    $workerArgs = @('run', '--rm', '--network', $created.internal_network_id,
        '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
        '--entrypoint', 'node', $WorkerImageId, '-e')
    & docker @workerArgs ($directJs.Replace('__EGRESS_IP__', $egressIp))
    if ($LASTEXITCODE -ne 0) { throw 'Internal worker has a bypass route' }
    & docker @workerArgs $denyJs
    if ($LASTEXITCODE -ne 0) { throw 'Proxy accepted a forbidden request' }

    $routeAddress = (& docker exec $created.proxy_container_name node -e $routeJs |
        Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $routeAddress -ne $egressIp) {
        throw 'Outbound network is not the proxy default route'
    }
    $egressArgs = @('run', '--rm', '--network', $created.outbound_network_id,
        '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
        '--entrypoint', 'node', $WorkerImageId, '-e')
    & docker @egressArgs ($egressBindJs.Replace('__EGRESS_IP__', $egressIp))
    if ($LASTEXITCODE -ne 0) { throw 'Proxy exposed its listener on outbound network' }
    if ($ProbeOfficialTls) {
        & docker @workerArgs $officialJs
        if ($LASTEXITCODE -ne 0) { throw 'Official TLS/401 probe failed' }
    }
    Write-Output 'Dual-network synthetic negative probe passed.'
}
catch {
    if ($created) {
        & docker logs --tail 20 $created.proxy_container_name
    }
    throw
}
finally {
    if ($created) {
        & "$PSScriptRoot/Stop-HarnessEgressProxy.ps1" -InternalNetworkId $created.internal_network_id -OutboundNetworkId $created.outbound_network_id -ProxyContainerName $created.proxy_container_name
    }
}
