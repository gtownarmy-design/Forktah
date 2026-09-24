'use strict';

const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');

const WEB_ROOT = path.resolve(__dirname, '..', 'web');

const AGENTS = [
  { id: 'conductor', name: 'Conductor', role: 'orchestrator', provider: 'Claude' },
  { id: 'developer', name: 'Developer', role: 'full-stack developer', provider: 'Claude' },
  { id: 'imager', name: 'Imager', role: 'image generator', provider: 'Grok' },
  { id: 'reviewer', name: 'Reviewer', role: 'final reviewer', provider: 'Grok' },
].map((a) => ({ ...a, avatar: `/avatars/${a.id}.svg` }));

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.txt': 'text/plain; charset=utf-8',
};

function send(res, status, body, type) {
  res.writeHead(status, {
    'Content-Type': type,
    'Content-Length': Buffer.byteLength(body),
    'X-Content-Type-Options': 'nosniff',
  });
  res.end(res.req.method === 'HEAD' ? undefined : body);
}

function sendJson(res, status, value) {
  send(res, status, JSON.stringify(value), 'application/json; charset=utf-8');
}

function sendText(res, status, text) {
  send(res, status, text + '\n', 'text/plain; charset=utf-8');
}

// Returns an absolute path inside WEB_ROOT, or null if the request path is
// malformed or tries to leave the web root (raw, percent-encoded or otherwise).
function resolveStatic(rawPath) {
  let decoded;
  try {
    decoded = decodeURIComponent(rawPath);
  } catch {
    return null;
  }
  if (decoded.includes('\0') || decoded.includes('\\')) return null;
  if (decoded.split('/').includes('..')) return null;
  const rel = decoded.endsWith('/') ? decoded + 'index.html' : decoded;
  const full = path.resolve(WEB_ROOT, '.' + rel);
  if (full !== WEB_ROOT && !full.startsWith(WEB_ROOT + path.sep)) return null;
  return full;
}

function serveStatic(req, res, rawPath) {
  const file = resolveStatic(rawPath);
  if (!file) return sendText(res, 400, 'Bad Request');
  fs.stat(file, (err, stat) => {
    if (err || !stat.isFile()) return sendText(res, 404, 'Not Found');
    const type = TYPES[path.extname(file).toLowerCase()] || 'application/octet-stream';
    res.writeHead(200, {
      'Content-Type': type,
      'Content-Length': stat.size,
      'X-Content-Type-Options': 'nosniff',
    });
    if (req.method === 'HEAD') return res.end();
    fs.createReadStream(file).pipe(res);
  });
}

function handle(req, res) {
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    res.setHeader('Allow', 'GET, HEAD');
    return sendText(res, 405, 'Method Not Allowed');
  }
  // req.url is the raw request target; keep it undecoded so traversal checks
  // see every form a client can send.
  const rawPath = (req.url || '/').split('?')[0].split('#')[0];
  if (!rawPath.startsWith('/')) return sendText(res, 400, 'Bad Request');

  if (rawPath === '/api/health') return sendJson(res, 200, { ok: true });
  if (rawPath === '/api/agents') return sendJson(res, 200, AGENTS);
  if (rawPath.startsWith('/api/')) return sendJson(res, 404, { error: 'not found' });

  serveStatic(req, res, rawPath);
}

function createServer() {
  return http.createServer(handle);
}

module.exports = { createServer, AGENTS };

if (require.main === module) {
  const port = Number(process.env.PORT) || 4173;
  createServer().listen(port, () => {
    console.log(`Roll Call listening on http://localhost:${port}`);
  });
}
