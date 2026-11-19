"""Thin wrapper over the `orca` CLI. Every call passes --json and fixed argument lists (no shell strings).

Orca facts the driver relies on (CLI 1.4.x):
- A Run is bound to the terminal that created it (`run-create` from the driver's own terminal); other terminals
  changing it are fenced. So each ticket driver creates and keeps its own Run.
- A task with unfinished deps is `pending` and `worker-start --task` refuses it (`task_not_startable`).
- `worker_done` is the only completion signal; heartbeats are not completion.
- An accepted worker_done schedules a release of the worker terminal about 2 minutes later unless the worker was
  started with --keep-open — the implementing terminal is kept open so the judge can run in it.
- A terminal still held by a finished dispatch takes a new task only through `worker-start --task --terminal`
  (with --worktree of that terminal); `dispatch --to <terminal>` is refused with invalid_argument.
- `task-update --status failed` then `worker-start --task --retry-of <dispatch>` reopens a failed task.
"""

import json
import subprocess

from cbd.util import DriverError


def call(*args, timeout=120):
    try:
        r = subprocess.run(["orca", *args, "--json"], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise DriverError("환경", "orca CLI 가 PATH 에 없다")
    except subprocess.TimeoutExpired:
        raise DriverError("연결 불명", f"orca {' '.join(args[:2])} 응답 없음 ({timeout}s)")
    try:  # pretty-printed JSON, sometimes followed by more objects: take the first one
        d = json.JSONDecoder().raw_decode((r.stdout or "").lstrip())[0]
    except ValueError:
        raise DriverError("연결 불명", f"orca {' '.join(args[:2])}: {(r.stdout or r.stderr).strip()[:300]}")
    if d.get("ok") is not True:
        err = d.get("error") or d
        code = err.get("code", "") if isinstance(err, dict) else ""
        raise OrcaError(code, json.dumps(err, ensure_ascii=False)[:400])
    return d.get("result") or {}


class OrcaError(DriverError):
    def __init__(self, code, message):
        super().__init__("연결 불명", message)
        self.code = code


# How the driver answers an Orca error:
# (a) transient — Orca gave no definite refusal (timeout, unparsable output, a code not listed below, or a refusal that
#     names a passing state): the ticket driver re-enters the same step after 60/120/240 s and blocks only after that;
# (b) an alternative path exists — reusing a terminal refused: a new session at once (TicketDriver.start_judge);
# (c) a person must decide — verification, rebase conflict, approval, worker question: these are not Orca errors and
#     block or wait at once.
PERMANENT_CODES = frozenset({"invalid_argument", "task_not_startable", "not_found", "permission_denied",
                             "unauthenticated", "unauthorized", "forbidden"})
TRANSIENT_HINTS = ("timed out", "timeout", "temporarily", "try again", "not ready", "release runs", "releasing",
                   "econnrefused", "connection reset")


def transient(e):
    """True for class (a) above."""
    if not isinstance(e, DriverError) or e.kind != "연결 불명":
        return False
    if getattr(e, "code", "") not in PERMANENT_CODES:
        return True
    return any(h in e.message.lower() for h in TRANSIENT_HINTS)


def find(node, key):
    """First string value of `key` anywhere in a JSON tree."""
    if isinstance(node, dict):
        if isinstance(node.get(key), str):
            return node[key]
        node = list(node.values())
    if isinstance(node, list):
        for c in node:
            v = find(c, key)
            if v:
                return v
    return None


# ---- terminals
def live_handles():
    try:
        return frozenset(t.get("handle") for t in (call("terminal", "list").get("terminals") or [])
                         if t.get("connected", True))
    except DriverError:
        return frozenset()


def terminal_create(worktree_selector, title, command):
    res = call("terminal", "create", "--worktree", worktree_selector, "--title", title, "--command", command)
    h = find(res, "handle")
    if not h:
        raise DriverError("연결 불명", "terminal create 가 handle 을 주지 않았다")
    return h


def terminal_close(handle):
    try:
        call("terminal", "close", "--terminal", handle)
    except DriverError:
        pass


# ---- worktrees
def repo_info(name):
    """The Orca repo record whose display name is `name` (id, path, ...), or None."""
    for r in call("repo", "list").get("repos") or []:
        if r.get("displayName") == name:
            return r
    return None


def repo_path(name):
    r = repo_info(name)
    return r.get("path") if r else None


def worktrees():
    return call("worktree", "list").get("worktrees") or []


def find_worktree(branch):
    """The Orca worktree record checked out on `branch` (id, path, displayName, parentWorktreeId), or None."""
    try:
        rows = worktrees()
    except DriverError:
        return None
    return next((w for w in rows if w.get("branch") in (branch, f"refs/heads/{branch}")), None)


def worktree_for_branch(branch):
    w = find_worktree(branch)
    return w.get("path") if w else None


def worktree_create(repo, name, branch, base, parent=None, no_parent=False):
    """New worktree `name` from `base`. branch None = Orca's prefixed default; parent = parent worktree id."""
    args = ["worktree", "create", "--repo", f"name:{repo}", "--name", name, "--base-branch", base]
    if branch:
        args += ["--branch", branch]
    if parent:
        args += ["--parent-worktree", f"id:{parent}"]
    elif no_parent:
        args += ["--no-parent"]
    res = call(*args, timeout=600)
    path = find(res, "path")
    if not path:
        raise DriverError("환경", f"worktree create 가 경로를 주지 않았다 ({repo} {name})")
    return path


# ---- orchestration
def run_create(objective):
    res = call("orchestration", "run-create", "--objective", objective)
    rid = find(res, "id") or find(res, "runId")
    if not rid:
        raise DriverError("연결 불명", "run-create 가 Run id 를 주지 않았다")
    return rid


def run_use(run_id):
    call("orchestration", "run-use", "--id", run_id)


def task_create(run_id, title, spec_file, deps):
    args = ["orchestration", "task-create", "--run", run_id, "--task-title", title, "--display-name", title,
            "--spec-file", spec_file]
    if deps:
        args += ["--deps", json.dumps(deps)]
    res = call(*args)
    tid = find(res, "id") or find(res, "taskId")
    if not tid:
        raise DriverError("연결 불명", f"task-create 가 id 를 주지 않았다 ({title})")
    return tid


def task_list(run_id):
    return {t["id"]: t for t in (call("orchestration", "task-list", "--run", run_id).get("tasks") or [])}


def task_update(run_id, task_id, status, result=""):
    args = ["orchestration", "task-update", "--run", run_id, "--id", task_id, "--status", status]
    if result:
        args += ["--result", result[:2000]]
    call(*args)


def worker_start(run_id, task_id, worktree_path, agent, model, effort, title, keep_open=False, retry_of=None,
                 terminal=None):
    """Start a supervised worker for a ready (or failed, with retry_of) task — one call, no retry: the ticket driver
    decides what an error means (transient). `terminal` reuses a live terminal held by a finished dispatch; Orca then
    refuses --agent/--model/--effort and needs --worktree of that terminal."""
    args = ["orchestration", "worker-start", "--run", run_id, "--task", task_id, "--worktree", f"path:{worktree_path}"]
    if terminal:
        args += ["--terminal", terminal]
    else:
        args += ["--agent", agent, "--display-name", title]
        if model:
            args += ["--model", model]
        if effort:
            args += ["--effort", effort]
    if keep_open:
        args += ["--keep-open"]
    if retry_of:
        args += ["--retry-of", retry_of]
    res = call(*args, timeout=300)
    return {"dispatch": find(res, "dispatchId") or find(res, "id"),
            "handle": find(res, "handle") or find(res, "terminal") or find(res, "agentTerminalHandle")}


def worker_list(run_id):
    """Worker records of the Run: dispatchId, taskId, state, liveness, terminal."""
    return call("orchestration", "worker-list", "--run", run_id, "--limit", "100").get("workers") or []


def worker_release(dispatch_id):
    try:
        call("orchestration", "worker-release", "--dispatch", dispatch_id)
    except DriverError:
        pass


def worker_show(dispatch_id):
    return call("orchestration", "worker-show", "--dispatch", dispatch_id)


def check_wait(run_id, timeout_ms):
    """Consume the Run mailbox; block up to timeout_ms for worker_done/question/escalation. -> [message]."""
    try:
        res = call("orchestration", "check", "--run", run_id, "--wait", "--types", "worker_done,question,escalation",
                   "--timeout-ms", str(timeout_ms), "--full", timeout=timeout_ms // 1000 + 60)
    except OrcaError as e:
        if "timeout" in str(e).lower():
            return []
        raise
    return [message(m) for m in res.get("messages") or []]


def message(m):
    """One mailbox row in one shape. `check --full` gives raw records (from_handle, payload as a JSON string)."""
    payload = m.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    return {"id": m.get("id", ""), "type": m.get("type") or "status",
            "from": (m.get("from") or m.get("from_handle") or "").removeprefix("dispatch:"),
            "subject": m.get("subject") or "", "body": m.get("body") or "",
            "payload": payload if isinstance(payload, dict) else {}}


def reply(msg_id, body):
    call("orchestration", "reply", "--id", msg_id, "--body", body)
