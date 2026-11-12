'use strict';

const http = require('node:http');
const https = require('node:https');
const net = require('node:net');
const os = require('node:os');

const TARGET_HOST = 'api.deepseek.com';
const RELAY_HOST = 'tongyimohe.aitoken.credit';
const ALLOWED_TARGETS = new Set([TARGET_HOST, RELAY_HOST]);
const MAX_CLIENT_HELLO_BYTES = 64 * 1024;
const PROXY_ALIAS = 'windcatcher-harness-proxy';
const PROXY_PORT = 3128;
const DEFAULT_LIMITS = Object.freeze({
  maxTunnels: 2,
  maxBytesEachDirection: 8 * 1024 * 1024,
  dnsTimeoutMs: 3000,
  connectTimeoutMs: 5000,
  idleTimeoutMs: 45000,
  totalTimeoutMs: 90000,
});

// Conservative IPv4 global-unicast allowlist: reject special-use ranges,
// including documentation, carrier NAT, benchmarking, multicast and reserved.
const NON_PUBLIC = [
  ['0.0.0.0', 8], ['10.0.0.0', 8], ['100.64.0.0', 10],
  ['127.0.0.0', 8], ['169.254.0.0', 16], ['172.16.0.0', 12],
  ['192.0.0.0', 24], ['192.0.2.0', 24], ['192.88.99.0', 24],
  ['192.168.0.0', 16], ['198.18.0.0', 15], ['198.51.100.0', 24],
  ['203.0.113.0', 24], ['224.0.0.0', 4], ['240.0.0.0', 4],
];

function ipv4Int(address) {
  if (net.isIP(address) !== 4) return null;
  return address.split('.').reduce((value, octet) =>
    ((value << 8) | Number(octet)) >>> 0, 0);
}

function inCidr(address, base, prefix) {
  const value = ipv4Int(address);
  const network = ipv4Int(base);
  if (value === null || network === null || !Number.isInteger(prefix) ||
      prefix < 0 || prefix > 32) return false;
  const mask = prefix === 0 ? 0 : (0xffffffff << (32 - prefix)) >>> 0;
  return (value & mask) === (network & mask);
}

function isPublicIpv4(address) {
  return ipv4Int(address) !== null &&
    !NON_PUBLIC.some(([base, prefix]) => inCidr(address, base, prefix));
}

function dnsName(value) {
  if (typeof value !== 'string') return '';
  return value.toLowerCase().replace(/\.$/, '');
}

function selectTargetHost(value = TARGET_HOST) {
  if (!ALLOWED_TARGETS.has(value)) throw new Error('Invalid fixed egress target');
  return value;
}

function parseDohAnswer(body, targetHost = TARGET_HOST) {
  targetHost = selectTargetHost(targetHost);
  const data = JSON.parse(body);
  if (data.Status !== 0 || !Array.isArray(data.Question) ||
      data.Question.length !== 1 || data.Question[0].type !== 1 ||
      dnsName(data.Question[0].name) !== targetHost ||
      !Array.isArray(data.Answer) || data.Answer.length > 32) {
    throw new Error('Unexpected DNS response');
  }
  let owner = targetHost;
  const allAddresses = data.Answer.filter((answer) => answer.type === 1)
    .map((answer) => answer.data);
  if (!allAddresses.every(isPublicIpv4)) {
    throw new Error('DNS response contains non-public IPv4');
  }
  const visited = new Set();
  for (let depth = 0; depth < 8; depth += 1) {
    if (visited.has(owner)) throw new Error('DNS alias cycle');
    visited.add(owner);
    const aliases = data.Answer.filter((answer) =>
      answer.type === 5 && dnsName(answer.name) === owner);
    if (aliases.length === 0) break;
    if (aliases.length !== 1) throw new Error('Ambiguous DNS alias');
    if (depth === 7) throw new Error('DNS alias chain too long');
    owner = dnsName(aliases[0].data);
  }
  const addresses = data.Answer.filter((answer) =>
    answer.type === 1 && dnsName(answer.name) === owner).map((answer) => answer.data);
  if (addresses.length < 1 || addresses.length > 16 ||
      !addresses.every(isPublicIpv4)) {
    throw new Error('DNS did not return public IPv4 addresses');
  }
  return addresses;
}

function dohRequestOptions(targetHost = TARGET_HOST) {
  targetHost = selectTargetHost(targetHost);
  // Pin the resolver transport IP. HTTPS still validates cloudflare-dns.com.
  // Only one of the two fixed hostnames is queried; there is no caller URL.
  return {
    hostname: 'cloudflare-dns.com',
    servername: 'cloudflare-dns.com',
    rejectUnauthorized: true,
    path: '/dns-query?name=' + targetHost + '&type=A',
    headers: {Accept: 'application/dns-json'},
    lookup: (_hostname, options, callback) => callback(null,
      options.all ? [{address: '1.1.1.1', family: 4}] : '1.1.1.1', 4),
    timeout: DEFAULT_LIMITS.dnsTimeoutMs,
  };
}

function resolveOfficialDoH(targetHost = TARGET_HOST) {
  const options = dohRequestOptions(targetHost);
  return new Promise((resolve, reject) => {
    const request = https.get(options, (response) => {
      if (response.statusCode !== 200) {
        response.resume();
        reject(new Error('DNS resolver unavailable'));
        return;
      }
      let body = '';
      response.on('data', (part) => {
        body += part.toString('utf8');
        if (body.length > 4096) request.destroy(new Error('DNS response too large'));
      });
      response.on('error', reject);
      response.on('end', () => {
        try { resolve(parseDohAnswer(body, targetHost)); }
        catch (error) { reject(error); }
      });
    });
    request.on('timeout', () => request.destroy(new Error('DNS timeout')));
    request.on('error', reject);
  });
}

function bindAddressForSubnet(cidr, interfaces = os.networkInterfaces()) {
  const match = /^([^/]+)\/(\d{1,2})$/.exec(cidr || '');
  if (!match || ipv4Int(match[1]) === null) {
    throw new Error('Invalid internal network subnet');
  }
  const prefix = Number(match[2]);
  if (prefix < 1 || prefix > 30) {
    throw new Error('Invalid internal network subnet');
  }
  const candidates = Object.values(interfaces).flat().filter((item) =>
    item && !item.internal && (item.family === 'IPv4' || item.family === 4) &&
    inCidr(item.address, match[1], prefix));
  if (candidates.length !== 1) {
    throw new Error('Internal network address is not unique');
  }
  return candidates[0].address;
}

function statusLine(status) {
  const reasons = {400: 'Bad Request', 403: 'Forbidden', 413: 'Content Too Large',
    429: 'Too Many Requests', 502: 'Bad Gateway', 504: 'Gateway Timeout'};
  return 'HTTP/1.1 ' + status + ' ' + reasons[status] +
    '\r\nConnection: close\r\nContent-Length: 0\r\n\r\n';
}

function sendStatus(socket, status) {
  if (!socket.destroyed) socket.end(statusLine(status));
}

function hasExactHostHeader(request, targetHost) {
  const raw = request.rawHeaders || [];
  const hosts = [];
  for (let i = 0; i < raw.length; i += 2) {
    if (raw[i].toLowerCase() === 'host') hosts.push(raw[i + 1]);
  }
  return hosts.length === 1 && hosts[0] === targetHost;
}

function inspectClientHello(buffer, targetHost) {
  if (!Buffer.isBuffer(buffer) || buffer.length > MAX_CLIENT_HELLO_BYTES) return 'deny';
  let offset = 0;
  const parts = [];
  let total = 0;
  while (true) {
    if (buffer.length - offset < 5) return 'need_more';
    if (buffer[offset] !== 22 || buffer[offset + 1] !== 3 ||
        buffer[offset + 2] > 4) return 'deny';
    const length = buffer.readUInt16BE(offset + 3);
    if (length < 1 || length > 18432) return 'deny';
    if (buffer.length - offset < 5 + length) return 'need_more';
    parts.push(buffer.subarray(offset + 5, offset + 5 + length));
    total += length;
    const handshake = Buffer.concat(parts, total);
    if (handshake[0] !== 1) return 'deny';
    if (handshake.length < 4) {
      offset += 5 + length;
      if (offset >= buffer.length) return 'need_more';
      continue;
    }
    const helloLength = handshake.readUIntBE(1, 3);
    if (helloLength < 40 || helloLength > MAX_CLIENT_HELLO_BYTES - 4) return 'deny';
    if (handshake.length >= helloLength + 4) {
      return clientHelloSniMatches(handshake.subarray(4, helloLength + 4), targetHost) ?
        'match' : 'deny';
    }
    offset += 5 + length;
    if (offset >= buffer.length) return 'need_more';
  }
}

function clientHelloSniMatches(hello, targetHost) {
  let offset = 34; // legacy version + random
  if (hello.length < offset + 1) return false;
  const sessionLength = hello[offset++];
  offset += sessionLength;
  if (hello.length < offset + 2) return false;
  const cipherLength = hello.readUInt16BE(offset);
  offset += 2 + cipherLength;
  if (!cipherLength || cipherLength % 2 || hello.length < offset + 1) return false;
  const compressionLength = hello[offset++];
  offset += compressionLength;
  if (!compressionLength || hello.length < offset + 2) return false;
  const extensionsLength = hello.readUInt16BE(offset);
  offset += 2;
  const end = offset + extensionsLength;
  if (end !== hello.length) return false;
  let seenSni = false;
  while (offset < end) {
    if (end - offset < 4) return false;
    const type = hello.readUInt16BE(offset);
    const length = hello.readUInt16BE(offset + 2);
    offset += 4;
    if (offset + length > end) return false;
    if (type === 0) {
      if (seenSni || length < 5) return false;
      seenSni = true;
      const listLength = hello.readUInt16BE(offset);
      if (listLength !== length - 2 || hello[offset + 2] !== 0) return false;
      const nameLength = hello.readUInt16BE(offset + 3);
      if (nameLength !== listLength - 3 ||
          !hello.subarray(offset + 5, offset + 5 + nameLength)
            .equals(Buffer.from(targetHost, 'ascii'))) return false;
    }
    offset += length;
  }
  return seenSni;
}

function createProxyServer(options = {}) {
  const targetHost = selectTargetHost(options.targetHost);
  const resolve4 = options.resolve4 || resolveOfficialDoH;
  const dial = options.dial || ((ip) => net.connect({host: ip, port: 443, family: 4}));
  const limits = Object.assign({}, DEFAULT_LIMITS, options.limits || {});
  const audit = options.audit || ((event) => console.log(JSON.stringify({event})));
  let active = 0;

  const server = http.createServer({maxHeaderSize: 4096}, (_request, response) => {
    audit('deny_http');
    response.writeHead(403, {'Connection': 'close', 'Content-Length': '0'});
    response.end();
  });
  server.maxHeadersCount = 16;
  server.maxConnections = 16;
  server.requestTimeout = 10000;
  server.headersTimeout = 5000;
  server.keepAliveTimeout = 1000;
  server.on('clientError', (_error, socket) => {
    audit('deny_malformed');
    sendStatus(socket, 400);
  });

  server.on('connect', (request, client, head) => {
    if (request.url !== targetHost + ':443' || !hasExactHostHeader(request, targetHost)) {
      audit('deny_authority');
      sendStatus(client, 403);
      return;
    }
    if (active >= limits.maxTunnels) {
      audit('deny_capacity');
      sendStatus(client, 429);
      return;
    }
    if (head.length > limits.maxBytesEachDirection) {
      audit('deny_bytes');
      sendStatus(client, 413);
      return;
    }

    active += 1;
    client.pause();
    let upstream;
    let established = false;
    let closed = false;
    let bytesUp = 0;
    let bytesDown = 0;
    let pendingHello = Buffer.alloc(0);
    let sniVerified = false;
    let dnsTimer;
    let connectTimer;
    let idleTimer;
    const totalTimer = setTimeout(() => finish(504, 'deny_total_timeout'),
      limits.totalTimeoutMs);

    function finish(status, event = 'tunnel_closed') {
      if (closed) return;
      closed = true;
      active -= 1;
      clearTimeout(dnsTimer);
      clearTimeout(connectTimer);
      clearTimeout(idleTimer);
      clearTimeout(totalTimer);
      if (upstream) upstream.destroy();
      if (status && !established) sendStatus(client, status);
      else client.destroy();
      audit(event);
    }

    function resetIdle() {
      clearTimeout(idleTimer);
      idleTimer = setTimeout(() => finish(undefined, 'deny_idle_timeout'),
        limits.idleTimeoutMs);
    }

    function forward(chunk, destination, direction) {
      if (closed) return;
      if (direction === 'up') bytesUp += chunk.length;
      else bytesDown += chunk.length;
      if (bytesUp > limits.maxBytesEachDirection ||
          bytesDown > limits.maxBytesEachDirection) {
        finish(undefined, 'deny_bytes');
        return;
      }
      resetIdle();
      if (!destination.write(chunk)) {
        const source = direction === 'up' ? client : upstream;
        source.pause();
        destination.once('drain', () => {
          if (!closed) source.resume();
        });
      }
    }

    function forwardClient(chunk) {
      if (sniVerified) {
        forward(chunk, upstream, 'up');
        return;
      }
      pendingHello = Buffer.concat([pendingHello, chunk]);
      if (pendingHello.length > limits.maxBytesEachDirection) {
        finish(undefined, 'deny_bytes');
        return;
      }
      const result = inspectClientHello(pendingHello, targetHost);
      if (result === 'need_more') { resetIdle(); return; }
      if (result !== 'match') { finish(undefined, 'deny_sni'); return; }
      sniVerified = true;
      forward(pendingHello, upstream, 'up');
      pendingHello = null;
    }

    client.once('error', () => finish());
    client.once('end', () => finish());
    client.once('close', () => finish());
    dnsTimer = setTimeout(() => finish(504, 'deny_dns_timeout'),
      limits.dnsTimeoutMs);

    Promise.resolve().then(() => resolve4(targetHost)).then((records) => {
      clearTimeout(dnsTimer);
      if (closed) return;
      if (!Array.isArray(records) || records.length < 1 || records.length > 16 ||
          !records.every(isPublicIpv4)) {
        finish(502, 'deny_dns_address');
        return;
      }
      // Never resolve the host again during connection: dial the checked IP.
      upstream = dial(records[0]);
      connectTimer = setTimeout(() => finish(504, 'deny_connect_timeout'),
        limits.connectTimeoutMs);
      upstream.once('error', () => finish(502, 'deny_connect_error'));
      upstream.once('close', () => finish());
      upstream.once('connect', () => {
        clearTimeout(connectTimer);
        if (closed) return;
        established = true;
        client.write('HTTP/1.1 200 Connection Established\r\n\r\n', () => {
          if (closed) return;
          client.on('data', forwardClient);
          upstream.on('data', (chunk) => forward(chunk, client, 'down'));
          resetIdle();
          if (head.length) forwardClient(head);
          client.resume();
          audit('tunnel_established');
        });
      });
    }).catch(() => finish(502, 'deny_resolve_or_dial'));
  });

  return {server, activeCount: () => active};
}

if (require.main === module) {
  try {
    const address = bindAddressForSubnet(process.env.WINDCATCHER_HARNESS_INTERNAL_SUBNET);
    const {server} = createProxyServer({targetHost:
      process.env.WINDCATCHER_HARNESS_TARGET_HOST || TARGET_HOST});
    server.listen(PROXY_PORT, address, () =>
      console.log(JSON.stringify({event: 'proxy_listening'})));
  } catch (_error) {
    console.error(JSON.stringify({event: 'proxy_start_failed'}));
    process.exitCode = 1;
  }
}

module.exports = {createProxyServer, isPublicIpv4, bindAddressForSubnet,
  parseDohAnswer, resolveOfficialDoH, dohRequestOptions, inspectClientHello,
  PROXY_ALIAS, PROXY_PORT};
