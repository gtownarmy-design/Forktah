'use strict';

const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const { createServer } = require('../server/index.js');

let server;
let port;

before(async () => {
  server = createServer();
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  port = server.address().port;
});

after(() => new Promise((resolve) => server.close(resolve)));

// http.request sends the path verbatim, so traversal attempts reach the server
// unnormalised (fetch/URL would collapse `..` before sending).
function get(path) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, path, method: 'GET' }, (res) => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', (c) => (body += c));
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body }));
    });
    req.on('error', reject);
    req.end();
  });
}

test('GET /api/health returns { ok: true }', async () => {
  const res = await get('/api/health');
  assert.equal(res.status, 200);
  assert.match(res.headers['content-type'], /^application\/json/);
  assert.deepEqual(JSON.parse(res.body), { ok: true });
});

test('GET /api/agents returns exactly the four agents', async () => {
  const res = await get('/api/agents');
  assert.equal(res.status, 200);
  assert.match(res.headers['content-type'], /^application\/json/);
  const agents = JSON.parse(res.body);
  assert.ok(Array.isArray(agents));
  assert.equal(agents.length, 4);
  for (const a of agents) {
    assert.deepEqual(Object.keys(a).sort(), ['avatar', 'id', 'name', 'provider', 'role']);
    for (const v of Object.values(a)) assert.equal(typeof v, 'string');
    assert.equal(a.avatar, `/avatars/${a.id}.svg`);
  }
  const byId = Object.fromEntries(agents.map((a) => [a.id, a]));
  assert.deepEqual(Object.keys(byId).sort(), ['conductor', 'developer', 'imager', 'reviewer']);
  assert.equal(byId.conductor.role, 'orchestrator');
  assert.equal(byId.conductor.provider, 'Claude');
  assert.equal(byId.developer.role, 'full-stack developer');
  assert.equal(byId.developer.provider, 'Claude');
  assert.equal(byId.imager.role, 'image generator');
  assert.equal(byId.imager.provider, 'Grok');
  assert.equal(byId.reviewer.role, 'final reviewer');
  assert.equal(byId.reviewer.provider, 'Grok');
});

test('GET / serves the page from web/', async () => {
  const res = await get('/');
  assert.equal(res.status, 200);
  assert.match(res.headers['content-type'], /^text\/html/);
  assert.match(res.body, /<script src="\/app.js"/);
});

test('static assets are served with their types', async () => {
  const js = await get('/app.js');
  assert.equal(js.status, 200);
  assert.match(js.headers['content-type'], /javascript/);
  const css = await get('/style.css');
  assert.equal(css.status, 200);
  assert.match(css.headers['content-type'], /^text\/css/);
});

test('unknown paths return 404', async () => {
  for (const p of ['/nope.html', '/api/nope', '/avatars/does-not-exist.svg']) {
    const res = await get(p);
    assert.equal(res.status, 404, p);
  }
});

test('path traversal is rejected, raw and encoded', async () => {
  const attempts = [
    '/../server/index.js',
    '/../../etc/passwd',
    '/avatars/../../server/index.js',
    '/%2e%2e/server/index.js',
    '/%2E%2E/server/index.js',
    '/%2e%2e%2fserver%2findex.js',
    '/..%2fserver%2findex.js',
    '/.%2e/server/index.js',
    '/..%5cserver%5cindex.js',
    '/%2e%2e%5c%2e%2e%5cetc%5cpasswd',
    '/index.html%00.js',
    '/%E0%A4%A',
  ];
  for (const p of attempts) {
    const res = await get(p);
    assert.equal(res.status, 400, p);
    assert.doesNotMatch(res.body, /createServer/, p);
  }
});
