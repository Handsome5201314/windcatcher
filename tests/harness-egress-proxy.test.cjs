'use strict';

const assert = require('node:assert/strict');
const net = require('node:net');
const {test} = require('node:test');
const {createProxyServer, isPublicIpv4, bindAddressForSubnet, parseDohAnswer,
  dohRequestOptions, inspectClientHello} =
  require('../deploy/harness-egress/proxy');

const RELAY_HOST = 'tongyimohe.aitoken.credit';

function clientHello(host) {
  const name = Buffer.from(host, 'ascii');
  const list = Buffer.concat([Buffer.from([0, name.length >> 8, name.length & 255]), name]);
  const names = Buffer.concat([Buffer.from([list.length >> 8, list.length & 255]), list]);
  const extension = Buffer.concat([Buffer.from([0, 0, names.length >> 8, names.length & 255]), names]);
  const body = Buffer.concat([
    Buffer.from([3, 3]), Buffer.alloc(32), Buffer.from([0, 0, 2, 0x13, 1, 1, 0]),
    Buffer.from([extension.length >> 8, extension.length & 255]), extension,
  ]);
  const handshake = Buffer.concat([Buffer.from([1, body.length >> 16, body.length >> 8 & 255, body.length & 255]), body]);
  return Buffer.concat([Buffer.from([22, 3, 1, handshake.length >> 8, handshake.length & 255]), handshake]);
}

async function listen(server) {
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  return server.address().port;
}

async function close(server) {
  if (server.closeAllConnections) server.closeAllConnections();
  await new Promise((resolve) => server.close(resolve));
}

function connect(port, authority = 'api.deepseek.com:443', host = 'api.deepseek.com') {
  return new Promise((resolve, reject) => {
    const socket = net.connect(port, '127.0.0.1');
    socket.setTimeout(2000, () => socket.destroy(new Error('test timeout')));
    socket.once('error', reject);
    socket.once('connect', () => {
      socket.write('CONNECT ' + authority + ' HTTP/1.1\r\nHost: ' + host +
        '\r\n\r\n');
    });
    let received = '';
    socket.on('data', (chunk) => {
      received += chunk.toString();
      if (received.includes('\r\n\r\n')) {
        socket.removeAllListeners('data');
        socket.removeAllListeners('error');
        socket.setTimeout(0);
        resolve({socket, status: Number(/^HTTP\/1\.1 (\d+)/.exec(received)?.[1])});
      }
    });
  });
}

async function rawResponse(port, request) {
  return new Promise((resolve, reject) => {
    const socket = net.connect(port, '127.0.0.1');
    let data = '';
    socket.setTimeout(2000, () => socket.destroy(new Error('test timeout')));
    socket.once('error', reject);
    socket.once('connect', () => socket.write(request));
    socket.on('data', (chunk) => { data += chunk.toString(); });
    socket.once('close', () => resolve(data));
  });
}

test('IPv4 selection rejects special-use and accepts public unicast', () => {
  for (const ip of ['0.1.2.3', '10.0.0.1', '100.64.0.1', '127.0.0.1',
    '169.254.1.1', '172.16.0.1', '192.0.0.1', '192.0.2.1',
    '192.168.1.1', '198.18.0.1', '198.51.100.1', '203.0.113.1',
    '224.0.0.1', '255.255.255.255', '::ffff:8.8.8.8', 'not-an-ip']) {
    assert.equal(isPublicIpv4(ip), false, ip);
  }
  for (const ip of ['1.1.1.1', '8.8.8.8', '104.18.0.1']) {
    assert.equal(isPublicIpv4(ip), true, ip);
  }
});

test('proxy binds only the single address inside its internal subnet', () => {
  const interfaces = {
    eth0: [{family: 'IPv4', address: '172.28.0.2', internal: false}],
    eth1: [{family: 'IPv4', address: '172.29.0.2', internal: false}],
  };
  assert.equal(bindAddressForSubnet('172.28.0.0/16', interfaces), '172.28.0.2');
  assert.throws(() => bindAddressForSubnet('172.30.0.0/16', interfaces));
  assert.throws(() => bindAddressForSubnet('172.28.0.0/16', {
    eth0: interfaces.eth0, eth2: interfaces.eth0,
  }));
});

test('DoH answer must follow the exact official CNAME to public IPv4', () => {
  const good = {Status: 0, Question: [{name: 'api.deepseek.com', type: 1}],
    Answer: [
      {name: 'api.deepseek.com', type: 5, data: 'edge.example.net.'},
      {name: 'edge.example.net', type: 1, data: '3.173.21.63'},
    ]};
  assert.deepEqual(parseDohAnswer(JSON.stringify(good)), ['3.173.21.63']);
  for (const bad of [
    {...good, Question: [{name: 'other.example.com', type: 1}]},
    {...good, Answer: [{name: 'edge.example.net', type: 1, data: '3.173.21.63'}]},
    {...good, Answer: [...good.Answer,
      {name: 'unrelated.example', type: 1, data: '198.18.0.174'}]},
    {...good, Answer: [
      {name: 'api.deepseek.com', type: 5, data: 'edge.example.net.'},
      {name: 'edge.example.net', type: 1, data: '10.0.0.1'},
    ]},
  ]) assert.throws(() => parseDohAnswer(JSON.stringify(bad)));
});

test('relay target is an explicit exact choice with pinned DoH transport', () => {
  assert.throws(() => createProxyServer({targetHost: 'attacker.example'}));
  assert.throws(() => createProxyServer({targetHost: 'TONGYIMOHE.AITOKEN.CREDIT'}));
  assert.throws(() => createProxyServer({targetHost: '127.0.0.1'}));
  const options = dohRequestOptions(RELAY_HOST);
  assert.equal(options.hostname, 'cloudflare-dns.com');
  assert.equal(options.servername, 'cloudflare-dns.com');
  assert.equal(options.rejectUnauthorized, true);
  assert.equal(options.path, '/dns-query?name=tongyimohe.aitoken.credit&type=A');
  options.lookup('cloudflare-dns.com', {}, (_error, address) =>
    assert.equal(address, '1.1.1.1'));
  const good = {Status: 0, Question: [{name: RELAY_HOST, type: 1}],
    Answer: [{name: RELAY_HOST, type: 1, data: '8.8.8.8'}]};
  assert.deepEqual(parseDohAnswer(JSON.stringify(good), RELAY_HOST), ['8.8.8.8']);
  assert.throws(() => parseDohAnswer(JSON.stringify({
    ...good, Question: [{name: 'api.deepseek.com', type: 1}],
  }), RELAY_HOST));
  assert.throws(() => parseDohAnswer(JSON.stringify({
    ...good, Answer: [{name: RELAY_HOST, type: 1, data: '10.0.0.1'}],
  }), RELAY_HOST));
});

test('relay mode accepts only exact relay CONNECT and Host', async () => {
  const seen = [];
  const {server} = createProxyServer({targetHost: RELAY_HOST,
    resolve4: async (host) => { seen.push(host); return ['127.0.0.1']; },
    audit: () => {}});
  const port = await listen(server);
  try {
    for (const [authority, host] of [
      ['api.deepseek.com:443', 'api.deepseek.com'],
      [RELAY_HOST + ':443', 'api.deepseek.com'],
      [RELAY_HOST.toUpperCase() + ':443', RELAY_HOST.toUpperCase()],
      [RELAY_HOST + '.:443', RELAY_HOST + '.'],
      ['x@' + RELAY_HOST + ':443', RELAY_HOST],
      [RELAY_HOST + ':80', RELAY_HOST],
    ]) {
      const result = await connect(port, authority, host);
      assert.equal(result.status, 403, authority);
      result.socket.destroy();
    }
    const allowed = await connect(port, RELAY_HOST + ':443', RELAY_HOST);
    assert.equal(allowed.status, 502); // synthetic private DNS answer, no network
    allowed.socket.destroy();
    assert.deepEqual(seen, [RELAY_HOST]);
  } finally {
    await close(server);
  }
});

test('first TLS ClientHello must carry only the selected SNI', () => {
  const good = clientHello(RELAY_HOST);
  assert.equal(inspectClientHello(good.subarray(0, 9), RELAY_HOST), 'need_more');
  assert.equal(inspectClientHello(good, RELAY_HOST), 'match');
  const first = good.subarray(5, 7);
  const second = good.subarray(7);
  const fragmented = Buffer.concat([
    Buffer.from([22, 3, 1, 0, first.length]), first,
    Buffer.from([22, 3, 1, second.length >> 8, second.length & 255]), second,
  ]);
  assert.equal(inspectClientHello(fragmented, RELAY_HOST), 'match');
  assert.equal(inspectClientHello(clientHello('api.deepseek.com'), RELAY_HOST), 'deny');
  assert.equal(inspectClientHello(Buffer.from('GET / HTTP/1.1\r\n'), RELAY_HOST), 'deny');
  assert.equal(inspectClientHello(clientHello(RELAY_HOST + '.fallback'), RELAY_HOST), 'deny');
  assert.equal(inspectClientHello(clientHello(''), RELAY_HOST), 'deny');
});

test('relay tunnel never forwards bytes with an unrelated SNI', async () => {
  let received = 0;
  const upstreamSockets = new Set();
  const upstream = net.createServer((socket) => {
    upstreamSockets.add(socket);
    socket.once('close', () => upstreamSockets.delete(socket));
    socket.on('data', (part) => { received += part.length; });
  });
  const upstreamPort = await listen(upstream);
  const events = [];
  const {server} = createProxyServer({targetHost: RELAY_HOST,
    resolve4: async () => ['8.8.8.8'],
    dial: () => net.connect(upstreamPort, '127.0.0.1'),
    audit: (event) => events.push(event),
  });
  const port = await listen(server);
  let tunnel;
  try {
    tunnel = await connect(port, RELAY_HOST + ':443', RELAY_HOST);
    assert.equal(tunnel.status, 200);
    const closed = new Promise((resolve) => tunnel.socket.once('close', resolve));
    tunnel.socket.write(clientHello('api.deepseek.com'));
    await closed;
    assert.equal(received, 0);
    assert.ok(events.includes('deny_sni'));
  } finally {
    if (tunnel) tunnel.socket.destroy();
    for (const socket of upstreamSockets) socket.destroy();
    await close(server);
    await close(upstream);
  }
});

test('official default and relay modes forward matching SNI handshakes', async () => {
  for (const targetHost of ['api.deepseek.com', RELAY_HOST]) {
    const upstreamSockets = new Set();
    const upstream = net.createServer((socket) => {
      upstreamSockets.add(socket);
      socket.once('close', () => upstreamSockets.delete(socket));
      socket.pipe(socket);
    });
    const upstreamPort = await listen(upstream);
    const lookedUp = [];
    const {server} = createProxyServer({
      ...(targetHost === RELAY_HOST ? {targetHost} : {}),
      resolve4: async (host) => { lookedUp.push(host); return ['8.8.8.8']; },
      dial: () => net.connect(upstreamPort, '127.0.0.1'),
      audit: () => {},
    });
    const port = await listen(server);
    let tunnel;
    try {
      tunnel = await connect(port, targetHost + ':443', targetHost);
      assert.equal(tunnel.status, 200);
      const hello = clientHello(targetHost);
      const echoed = new Promise((resolve) => tunnel.socket.once('data', resolve));
      tunnel.socket.write(hello);
      assert.deepEqual(await echoed, hello);
      assert.deepEqual(lookedUp, [targetHost]);
    } finally {
      if (tunnel) tunnel.socket.destroy();
      for (const socket of upstreamSockets) socket.destroy();
      await close(server);
      await close(upstream);
    }
  }
});

test('only exact CONNECT authority and Host reach DNS', async () => {
  let lookups = 0;
  const {server} = createProxyServer({
    resolve4: async () => { lookups += 1; return ['127.0.0.1']; },
    audit: () => {},
  });
  const port = await listen(server);
  try {
    const invalid = [
      ['example.com:443', 'example.com'],
      ['127.0.0.1:443', '127.0.0.1'],
      ['[::1]:443', '[::1]'],
      ['API.DEEPSEEK.COM:443', 'API.DEEPSEEK.COM'],
      ['api.deepseek.com.:443', 'api.deepseek.com.'],
      ['x@api.deepseek.com:443', 'api.deepseek.com'],
      ['api.deepseek.com:80', 'api.deepseek.com'],
      ['api.deepseek.com:443', 'example.com'],
    ];
    for (const [authority, host] of invalid) {
      const result = await connect(port, authority, host);
      assert.equal(result.status, 403, authority);
      result.socket.destroy();
    }
    const duplicate = await rawResponse(port,
      'CONNECT api.deepseek.com:443 HTTP/1.1\r\n' +
      'Host: api.deepseek.com\r\nHost: api.deepseek.com\r\n\r\n');
    assert.match(duplicate, /^HTTP\/1\.1 403/);
    const httpResult = await rawResponse(port,
      'GET https://api.deepseek.com/ HTTP/1.1\r\nHost: api.deepseek.com\r\n\r\n');
    assert.match(httpResult, /^HTTP\/1\.1 403/);
    assert.equal(lookups, 0);
  } finally {
    await close(server);
  }
});

test('DNS rejects mixed private/public answers and never dials', async () => {
  let dials = 0;
  const events = [];
  const {server} = createProxyServer({
    resolve4: async () => ['8.8.8.8', '10.0.0.1'],
    dial: () => { dials += 1; throw new Error('must not dial'); },
    audit: (event) => events.push(event),
  });
  const port = await listen(server);
  try {
    const result = await connect(port);
    assert.equal(result.status, 502);
    result.socket.destroy();
    assert.equal(dials, 0);
    assert.deepEqual(events, ['deny_dns_address']);
  } finally {
    await close(server);
  }
});

test('checked public IP is pinned for the tunnel, with concurrency and byte limits', async () => {
  const upstreamSockets = new Set();
  const upstream = net.createServer((socket) => {
    upstreamSockets.add(socket);
    socket.once('close', () => upstreamSockets.delete(socket));
    socket.pipe(socket);
  });
  const upstreamPort = await listen(upstream);
  const dialed = [];
  const events = [];
  const {server, activeCount} = createProxyServer({
    resolve4: async () => ['8.8.8.8'],
    dial: (ip) => { dialed.push(ip); return net.connect(upstreamPort, '127.0.0.1'); },
    limits: {maxTunnels: 1, maxBytesEachDirection: 3},
    audit: (event) => events.push(event),
  });
  const port = await listen(server);
  let first;
  try {
    first = await connect(port);
    assert.equal(first.status, 200);
    assert.equal(activeCount(), 1);
    const second = await connect(port);
    assert.equal(second.status, 429);
    second.socket.destroy();
    const closed = new Promise((resolve) => first.socket.once('close', resolve));
    first.socket.write('four');
    await closed;
    assert.equal(activeCount(), 0);
    assert.deepEqual(dialed, ['8.8.8.8']);
    assert.ok(events.includes('deny_capacity'));
    assert.ok(events.includes('deny_bytes'));
  } finally {
    if (first) first.socket.destroy();
    for (const socket of upstreamSockets) socket.destroy();
    await close(server);
    await close(upstream);
  }
});

test('unresolved DNS times out without connecting', async () => {
  const {server} = createProxyServer({
    resolve4: () => new Promise(() => {}),
    limits: {dnsTimeoutMs: 25, totalTimeoutMs: 200},
    audit: () => {},
  });
  const port = await listen(server);
  try {
    const result = await connect(port);
    assert.equal(result.status, 504);
    result.socket.destroy();
  } finally {
    await close(server);
  }
});

test('unconnected upstream times out before returning tunnel success', async () => {
  const events = [];
  const {server} = createProxyServer({
    resolve4: async () => ['8.8.8.8'],
    dial: () => new net.Socket(),
    limits: {connectTimeoutMs: 25, totalTimeoutMs: 200},
    audit: (event) => events.push(event),
  });
  const port = await listen(server);
  try {
    const result = await connect(port);
    assert.equal(result.status, 504);
    result.socket.destroy();
    assert.ok(events.includes('deny_connect_timeout'));
  } finally {
    await close(server);
  }
});

test('idle and total deadlines close established tunnels', async () => {
  for (const [limit, event] of [
    [{idleTimeoutMs: 25, totalTimeoutMs: 200}, 'deny_idle_timeout'],
    [{idleTimeoutMs: 200, totalTimeoutMs: 25}, 'deny_total_timeout'],
  ]) {
    const sockets = new Set();
    const upstream = net.createServer((socket) => {
      sockets.add(socket);
      socket.once('close', () => sockets.delete(socket));
    });
    const upstreamPort = await listen(upstream);
    const events = [];
    const {server, activeCount} = createProxyServer({
      resolve4: async () => ['8.8.8.8'],
      dial: () => net.connect(upstreamPort, '127.0.0.1'),
      limits: limit,
      audit: (name) => events.push(name),
    });
    const port = await listen(server);
    let tunnel;
    try {
      tunnel = await connect(port);
      assert.equal(tunnel.status, 200);
      await new Promise((resolve) => tunnel.socket.once('close', resolve));
      assert.equal(activeCount(), 0);
      assert.ok(events.includes(event), event);
    } finally {
      if (tunnel) tunnel.socket.destroy();
      for (const socket of sockets) socket.destroy();
      await close(server);
      await close(upstream);
    }
  }
});
