"""Thin wrapper over the `orca` CLI. Every call passes --json and fixed argument lists (no shell strings).

Orca facts the driver relies on (CLI 1.4.x):
- A Run is bound to the terminal that created it (`run-create` from the driver's own terminal); other terminals
  changing it are fenced. So each ticket driver creates and keeps its own Run.
- A task with unfinished deps is `pending` and `worker-start --task` refuses it (`task_not_startable`).
- `worker_done` is the only completion signal; heartbeats are not completion.
- An accepted worker_done schedules a release of the worker terminal about 2 minutes later unless the worker was
  started with --keep-open — the implementing terminal is kept open so the judge can run in it.
"""

import json
import subprocess
import time

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
def worktree_for_branch(branch):
    try:
        res = call("worktree", "list")
    except DriverError:
        return None
    for w in res.get("worktrees") or []:
        if w.get("branch") in (branch, f"refs/heads/{branch}"):
            return w.get("path")
    return None


def worktree_create(repo, branch, base):
    res = call("worktree", "create", "--repo", f"name:{repo}", "--name", branch, "--branch", branch,
               "--base-branch", base, timeout=600)
    path = find(res, "path")
    if not path:
        raise DriverError("환경", f"worktree create 가 경로를 주지 않았다 ({repo} {branch})")
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


def worker_start(run_id, task_id, worktree_path, agent, model, effort, title, keep_open=False, retry_of=None):
    """Start a supervised worker for a ready task. Retries with backoff 0/60/120/240 s on transient errors."""
    args = ["orchestration", "worker-start", "--run", run_id, "--task", task_id, "--worktree", f"path:{worktree_path}",
            "--agent", agent, "--display-name", title]
    if model:
        args += ["--model", model]
    if effort:
        args += ["--effort", effort]
    if keep_open:
        args += ["--keep-open"]
    if retry_of:
        args += ["--retry-of", retry_of]
    last = None
    for delay in (0, 60, 120, 240):
        time.sleep(delay)
        try:
            res = call(*args, timeout=300)
            return {"dispatch": find(res, "dispatchId") or find(res, "id"), "handle": find(res, "handle")}
        except OrcaError as e:
            if e.code in ("task_not_startable",):
                raise
            last = e
    raise DriverError("정체", f"워커 기동 실패: {title} — {last}")


def dispatch(run_id, task_id, handle):
    """Hand a task to an existing terminal (the judge goes to the implementing terminal)."""
    res = call("orchestration", "dispatch", "--run", run_id, "--task", task_id, "--to", handle, "--inject")
    return find(res, "dispatchId") or find(res, "id")


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
    return res.get("messages") or []


def reply(msg_id, body):
    call("orchestration", "reply", "--id", msg_id, "--body", body)
