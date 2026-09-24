# Agent Roll Call

A tiny, dependency-free web app that lists the four agents of the roll-call team.
See `BRIEF.md` for the contract.

## Run

Requires Node.js 18 or newer. There is nothing to install.

```sh
node server/index.js            # http://localhost:4173
PORT=8080 node server/index.js  # choose another port
```

Open the URL in a browser to see one card per agent (avatar, name, role, provider).
If an avatar under `web/avatars/` is missing, the card shows a lettered placeholder.

### Routes

| Route              | Response                                                         |
| ------------------ | ---------------------------------------------------------------- |
| `GET /api/health`  | `{ "ok": true }`                                                 |
| `GET /api/agents`  | JSON array of 4 `{ id, name, role, provider, avatar }`           |
| anything else      | a static file from `web/` (`/` serves `index.html`), or 404      |

Paths that try to leave `web/` (`..`, `%2e%2e`, encoded slashes or backslashes, NUL bytes,
malformed escapes) get `400 Bad Request`.

## Test

```sh
node --test
```

This starts the server on a random port and covers both API routes, static serving,
404s for unknown paths, and path-traversal rejection (raw and percent-encoded).

## Layout

```
server/index.js   HTTP server (node:http only)
web/              index.html, app.js, style.css; avatars/ is drawn by rollcall-imager
test/             node:test suite
```
