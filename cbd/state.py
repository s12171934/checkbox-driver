"""The driver's own state under <home>/<project>/state/.

state/<PKG>.json       one ticket (status, worktree, base, run, checkboxes, exceptions)
state/plan.json        the dashboard plan (cbd.plan)
state/events.json      the last 40 events across tickets
state/run-<PKG>.json   {run_id, terminal} of the ticket's Orca Run (the dashboard globs run-*.json)
state/approvals/       <PKG>-<n> files written by `cbd approve`
state/replies/<run>/   <msg>.question.md from workers, <msg>.md answers to send, task_<id>.complete.md
state/scheduler.json   the scheduler terminal and the coordinator session that owns the plan
"""

import os
import time

from cbd.util import now_iso, now_ts, read_json, write_json

# Ticket statuses, in the dashboard's vocabulary (plan.json tickets[].status).
PLANNED, STARTED, RUNNING, BLOCKED = "planned", "started", "running", "blocked"
BOX_WAITING, BOX_DEFERRED, REBASE_PENDING = "box_waiting", "box_deferred", "rebase_pending"
VERIFIED, CLOSED, MANUAL = "verified", "closed", "manual"
DONE_STATUSES = (VERIFIED, CLOSED)
ACTIVE_STATUSES = (STARTED, RUNNING)


class Store:
    def __init__(self, cfg):
        self.cfg = cfg
        self.dir = cfg.state_dir

    def p(self, *parts):
        return os.path.join(self.dir, *parts)

    # ---- tickets
    def ticket(self, package):
        return read_json(self.p(f"{package}.json")) or {"package": package, "status": PLANNED, "checkboxes": []}

    def save(self, t):
        t["updated_at"] = now_iso()
        write_json(self.p(f"{t['package']}.json"), t)

    def all(self, packages):
        return {p: self.ticket(p) for p in packages}

    # ---- events
    def event(self, package, text):
        ev = read_json(self.p("events.json"), [])
        ev.append({"at": now_iso(), "package": package, "text": text})
        write_json(self.p("events.json"), ev[-40:])
        log_dir = self.cfg.p("logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, f"ticket-{package or 'scheduler'}.log"), "a", encoding="utf-8") as f:
            f.write(f"{now_iso()} {text}\n")

    def events(self):
        return read_json(self.p("events.json"), [])

    # ---- approvals
    def approved(self, package, n):
        return os.path.exists(self.p("approvals", f"{package}-{n}"))

    def approve(self, package, n, by="user"):
        os.makedirs(self.p("approvals"), exist_ok=True)
        with open(self.p("approvals", f"{package}-{n}"), "w", encoding="utf-8") as f:
            f.write(f"{now_iso()} {by}\n")

    # ---- runs
    def run_file(self, package):
        return read_json(self.p(f"run-{package}.json"))

    def save_run(self, package, run_id, terminal):
        old = self.run_file(package)
        if old and old.get("run_id") and old["run_id"] != run_id:
            os.replace(self.p(f"run-{package}.json"), self.p(f"run-{package}-{old['run_id']}.superseded.json"))
        write_json(self.p(f"run-{package}.json"), {"run_id": run_id, "terminal": terminal, "created_at": now_iso()})

    # ---- replies (worker questions are answered by a person or the top coordinator through files)
    def reply_dir(self, run_id):
        d = self.p("replies", run_id)
        os.makedirs(d, exist_ok=True)
        return d

    # ---- scheduler / ownership
    def scheduler(self):
        return read_json(self.p("scheduler.json"), {})

    def save_scheduler(self, data):
        write_json(self.p("scheduler.json"), {**self.scheduler(), **data})


class TicketLock:
    """One driver process per ticket: a lock directory holding the owner pid. A dead owner's lock is taken over."""

    def __init__(self, store, package):
        self.path = store.p("locks", package)

    def _owner(self):
        try:
            with open(os.path.join(self.path, "pid")) as f:
                return int(f.read().strip() or 0)
        except (OSError, ValueError):
            return 0

    def acquire(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        for _ in range(2):
            try:
                os.mkdir(self.path)
                with open(os.path.join(self.path, "pid"), "w") as f:
                    f.write(str(os.getpid()))
                return True
            except FileExistsError:
                pid = self._owner()
                if pid and _alive(pid):
                    return False
                self.release(force=True)
                time.sleep(0.1)
        return False

    def release(self, force=False):
        if not force and self._owner() != os.getpid():
            return
        try:
            os.remove(os.path.join(self.path, "pid"))
        except OSError:
            pass
        try:
            os.rmdir(self.path)
        except OSError:
            pass

    def held(self):
        pid = self._owner()
        return bool(pid and _alive(pid))


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def seconds_since(iso):
    import datetime
    try:
        return now_ts() - datetime.datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return float("inf")
