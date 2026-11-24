'use strict';
// Fake Nezha dashboard for local integration testing (test fixture only,
// not part of the agent). Speaks just enough gRPC-over-h2c to exercise the
// Python agent: registration, state stream, task stream, geoip.
//
// Usage: node fake_dashboard.js <port> <httpPort> <resultFile>
// It writes a JSON summary to <resultFile> once it has seen registration,
// >= 2 state messages and the 3 task results it asked for, then exits 0.
// A plain HTTP/1.1 server on <httpPort> serves as the HTTPGet/TCPPing target.
const http2 = require('http2');
const http = require('http');
const fs = require('fs');

const port = Number(process.argv[2]);
const httpPort = Number(process.argv[3]);
const resultFile = process.argv[4];

const summary = {
  registered: false,
  authOk: false,
  stateCount: 0,
  taskResults: {},
  geoip: false,
};

function frame(payload) {
  const head = Buffer.alloc(5);
  head[0] = 0;
  head.writeUInt32BE(payload.length, 1);
  return Buffer.concat([head, payload]);
}

// Tiny protobuf helpers (only what this fixture needs).
function encodeVarint(v) {
  let x = BigInt(v);
  const bytes = [];
  do {
    let b = Number(x & 0x7fn);
    x >>= 7n;
    if (x > 0n) b |= 0x80;
    bytes.push(b);
  } while (x > 0n);
  return Buffer.from(bytes);
}
function encodeTask(id, type, data) {
  const parts = [];
  parts.push(Buffer.from([0x08]), encodeVarint(id));       // field 1 varint
  parts.push(Buffer.from([0x10]), encodeVarint(type));     // field 2 varint
  const d = Buffer.from(data, 'utf8');
  parts.push(Buffer.from([0x1a]), encodeVarint(d.length), d); // field 3 string
  return Buffer.concat(parts);
}
function decodeTaskResult(buf) {
  // fields: 1 id varint, 2 type varint, 3 delay float32, 4 data string, 5 successful bool
  const out = {};
  let off = 0;
  const readVarint = () => {
    let r = 0n, s = 0n;
    while (off < buf.length) {
      const b = buf[off++];
      r |= BigInt(b & 0x7f) << s;
      if (!(b & 0x80)) return Number(r);
      s += 7n;
    }
    return Number(r);
  };
  while (off < buf.length) {
    const tag = readVarint();
    const field = tag >> 3, wire = tag & 7;
    if (wire === 0) {
      const v = readVarint();
      if (field === 1) out.id = v;
      else if (field === 2) out.type = v;
      else if (field === 5) out.successful = v !== 0;
    } else if (wire === 5) {
      out.delay = buf.readFloatLE(off);
      off += 4;
    } else if (wire === 2) {
      const len = readVarint();
      const data = buf.subarray(off, off + len).toString('utf8');
      off += len;
      if (field === 4) out.data = data;
    } else if (wire === 1) {
      off += 8;
    } else {
      break;
    }
  }
  return out;
}

function makeFrameFeeder(onMessage) {
  let buf = Buffer.alloc(0);
  return (chunk) => {
    buf = Buffer.concat([buf, chunk]);
    for (;;) {
      if (buf.length < 5) return;
      const len = buf.readUInt32BE(1);
      if (buf.length < 5 + len) return;
      onMessage(Buffer.from(buf.subarray(5, 5 + len)));
      buf = buf.subarray(5 + len);
    }
  };
}

function maybeFinish() {
  const kinds = Object.keys(summary.taskResults);
  if (summary.registered && summary.stateCount >= 2 && kinds.length >= 3) {
    fs.writeFileSync(resultFile, JSON.stringify(summary, null, 2));
    console.log('FAKE DASHBOARD: all expectations met');
    process.exit(0);
  }
}

const server = http2.createServer();
server.on('stream', (stream, headers) => {
  const path = headers[':path'];
  if (headers['client-secret'] === 'testkey' &&
      headers['client-uuid'] === 'test-uuid-1234') {
    summary.authOk = true;
  }
  stream.on('error', () => {});

  if (path === '/proto.NezhaService/ReportSystemInfo2') {
    stream.respond({ 'content-type': 'application/grpc', ':status': 200 },
                   { waitForTrailers: true });
    stream.on('wantTrailers', () => stream.sendTrailers({ 'grpc-status': '0' }));
    stream.on('data', makeFrameFeeder(() => {
      summary.registered = true;
      // Uint64Receipt { data: 12345 }
      const payload = Buffer.concat([Buffer.from([0x08]), encodeVarint(12345)]);
      stream.end(frame(payload));
    }));
  } else if (path === '/proto.NezhaService/ReportGeoIP') {
    stream.respond({ 'content-type': 'application/grpc', ':status': 200 },
                   { waitForTrailers: true });
    stream.on('wantTrailers', () => stream.sendTrailers({ 'grpc-status': '0' }));
    stream.on('data', makeFrameFeeder(() => {
      summary.geoip = true;
      stream.end(frame(Buffer.alloc(0)));
    }));
  } else if (path === '/proto.NezhaService/ReportSystemState') {
    stream.respond({ 'content-type': 'application/grpc', ':status': 200 });
    stream.on('data', makeFrameFeeder(() => {
      summary.stateCount += 1;
      // Receipt { proced: true }
      try { stream.write(frame(Buffer.from([0x08, 0x01]))); } catch (_) {}
      maybeFinish();
    }));
  } else if (path === '/proto.NezhaService/RequestTask') {
    stream.respond({ 'content-type': 'application/grpc', ':status': 200 });
    stream.on('data', makeFrameFeeder((msg) => {
      const r = decodeTaskResult(msg);
      summary.taskResults[r.id] = r;
      maybeFinish();
    }));
    setTimeout(() => {
      try {
        stream.write(frame(encodeTask(1, 7, ''))); // Keepalive
        stream.write(frame(encodeTask(2, 3, '127.0.0.1:' + httpPort))); // TCPPing
        stream.write(frame(encodeTask(3, 1, 'http://127.0.0.1:' + httpPort + '/'))); // HTTPGet
      } catch (_) {}
    }, 1000);
  } else {
    stream.respond({ ':status': 404 });
    stream.end();
  }
});
server.listen(port, '127.0.0.1');

const target = http.createServer((req, res) => {
  res.writeHead(200, { 'content-type': 'text/plain' });
  res.end('ok');
});
target.listen(httpPort, '127.0.0.1');

setTimeout(() => {
  fs.writeFileSync(resultFile, JSON.stringify(summary, null, 2));
  console.log('FAKE DASHBOARD: timeout, partial summary written');
  process.exit(2);
}, 60000);
