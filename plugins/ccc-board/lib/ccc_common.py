"""What the ccc-board MCP server and its hooks share: where the board is, and how to talk to it.

Everything here is the Python standard library, for the same reason the dashboard is:
this runs on machines where installing a package is somebody else's change request.

Configuration (environment, all optional):

    CCC_DASHBOARD     http://127.0.0.1:8787    the Control Center dashboard
    CCC_ACTOR         main                     who board writes are recorded as
    CCC_EVIDENCE_DIR  %USERPROFILE%\\ccc-evidence   where evidence files are copied
    CCC_EVIDENCE_REF  that folder, with /       how the board names them
    CCC_WSL_DISTRO    Ubuntu                   where agentmux runs
    CCC_STATE_DIR     %LOCALAPPDATA%\\ccc-board  native-task mirror state
    CCC_AGENTMUX_CMD  (tests) a JSON argv prefix used instead of the WSL agentmux
"""

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

KEY_RE = re.compile(r"^(EP|TM|ADR|SP|CAP)-[0-9]{3,9}$")
LEADING_KEY = re.compile(r"^\s*\[?(TM-[0-9]{3,9})\]?\b")


class BoardError(Exception):
    """The board answered and said no. `missing` names the verbs that fix it."""

    def __init__(self, message, missing=None, status=None):
        super().__init__(message)
        self.missing = missing or []
        self.status = status

    def explain(self):
        hints = [m.get("hint") for m in self.missing if isinstance(m, dict) and m.get("hint")]
        return str(self) + ("".join("\n  fix: " + h for h in hints) if hints else "")


class Unreachable(Exception):
    pass


def dashboard():
    return os.environ.get("CCC_DASHBOARD", "http://127.0.0.1:8787").rstrip("/")


def actor():
    return os.environ.get("CCC_ACTOR", "main")


def evidence_dir():
    return Path(os.environ.get("CCC_EVIDENCE_DIR") or Path.home() / "ccc-evidence")


def evidence_ref(path):
    """The string the board stores for a file under evidence_dir()."""
    base = os.environ.get("CCC_EVIDENCE_REF") or str(evidence_dir()).replace("\\", "/")
    rel = Path(path).relative_to(evidence_dir()).as_posix()
    return base.rstrip("/") + "/" + rel


def state_dir():
    base = os.environ.get("CCC_STATE_DIR") or os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "state")
    path = Path(base) if os.environ.get("CCC_STATE_DIR") else Path(base) / "ccc-board"
    path.mkdir(parents=True, exist_ok=True)
    return path


def api(method, path, body=None, timeout=10):
    """One board request. Refusals come back as BoardError with the board's own words."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(f"{dashboard()}/api/{path}", method=method, data=data,
                                     headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"null")
    except urllib.error.HTTPError as err:
        try:
            payload = json.loads(err.read() or b"{}")
        except ValueError:
            payload = {}
        raise BoardError(payload.get("error") or f"HTTP {err.code}", payload.get("missing"), err.code) from None
    except (urllib.error.URLError, OSError, TimeoutError) as err:
        raise Unreachable(f"Control Center is not reachable at {dashboard()} "
                          f"({getattr(err, 'reason', err)}). Is its dashboard running?") from None


def write(op, **fields):
    """POST /api/board/<op> as the configured actor."""
    body = {k: v for k, v in fields.items() if v is not None}
    body.setdefault("actor", actor())
    return api("POST", f"board/{op}", body)


def board(timeout=10):
    return api("GET", "board", timeout=timeout)


def tasks_of(snapshot):
    """Every task on a board snapshot, whichever shape the board returns."""
    tasks = snapshot.get("tasks")
    if isinstance(tasks, list):
        return tasks
    out = []
    for epic in snapshot.get("epics") or []:
        out.extend(epic.get("tasks") or [])
    return out


def session_tasks(session, status="in_progress", timeout=10):
    if not session:
        return []
    return [t for t in tasks_of(board(timeout)) if t.get("status") == status and t.get("session") == session]


def now_stamp():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def store_evidence(key, path=None, text=None, name=None):
    """Copy a file (or write text) under evidence_dir()/<KEY>/ and return its board ref.

    The board keeps a string, never the bytes, so the copy is what makes the evidence
    outlive the scratch directory it was produced in.
    """
    if not KEY_RE.match(key or ""):
        raise BoardError(f"not a board key: {key!r}")
    folder = evidence_dir() / key
    folder.mkdir(parents=True, exist_ok=True)
    if path:
        source = Path(path)
        if not source.is_file():
            raise BoardError(f"no such file: {path}")
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name or source.name)[:80] or "evidence"
        target = folder / f"{now_stamp()}-{stem}"
        shutil.copy2(source, target)
    else:
        if not text:
            raise BoardError("evidence needs a file path or some text")
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", name or "evidence.log")[:80]
        target = folder / f"{now_stamp()}-{stem}"
        target.write_text(text, encoding="utf-8")
    return evidence_ref(target)


def agentmux(*args, timeout=30):
    """Run the WSL agentmux CLI as the orchestrator (outside any pane)."""
    prefix = os.environ.get("CCC_AGENTMUX_CMD")
    if prefix:
        argv = json.loads(prefix) + list(args)
    else:
        argv = ["wsl.exe", "-d", os.environ.get("CCC_WSL_DISTRO", "Ubuntu"), "--exec",
                "bash", "-lc", 'exec agentmux "$@"', "agentmux", *args]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise Unreachable("wsl.exe is not available, so agentmux cannot be reached") from None
    except subprocess.TimeoutExpired:
        raise Unreachable(f"agentmux {args[0] if args else ''} timed out after {timeout}s") from None
    return result.returncode, result.stdout.strip(), result.stderr.strip()
