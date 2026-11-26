"""The ticket driver: `cbd <project> run <PKG>`, one process per ticket in its own Orca terminal.

start_ticket -> ensure_run -> precreate_tasks -> contract_check -> process_checkbox x N -> integration_review
  [-> fix checkbox -> remeasure] -> finish_ticket (verified, or rebase_pending while the chain prev is not verified)

At every checkbox boundary (and before finishing) boundary_rebase moves the branch onto a nearer verified chain head
when one appeared, and measures again what was green before (방식 · 같은 저장소 체인).
A consumer ticket checks the producer's API on an isolated stack before its first checkbox (contract_check,
체크박스를 넘는 검증 §1). A UI ticket's verify compares one scene with the design snapshot; a mismatch inserts a fix
checkbox before the verify and runs the verify again (§2).

Beside the loop, every commit starts a cumulative check (one GPT session over base..commit) that the loop does not wait
for; its problems go to the coordinator as alerts (state/alerts.jsonl kind 「cumulative」). The driver waits for the
outstanding checks only before verified, and their problems never stop the ticket.

Every step is resumable: the checkbox record keeps its phase, task ids, dispatches and finished roles, so a restarted
driver continues where the last one stopped (a worker_done received before a restart is read back from the task
status, not from the mailbox).
"""

import os
import re
import shlex
import time

from cbd import boxes as B
from cbd import briefs, orca
from cbd import plan as P
from cbd.design import expand_tcs
from cbd.gates import Gate
from cbd.jira import Jira
from cbd.select import base_for, repo_base
from cbd.state import BLOCKED, BOX_WAITING, DONE_STATUSES, REBASE_PENDING, RUNNING, VERIFIED, TicketLock
from cbd.util import DriverError, git, now_iso, now_ts, run, write_text


RETRY_DELAYS = (60, 120, 240)  # transient Orca errors (orca.transient): the same step again, then blocked
WORKER_RETRIES = 3              # a failed or lost worker is started again with --retry-of this many times
WORKER_GONE = ("failed", "lost", "cancelled", "canceled", "abandoned", "stopped")


class BoxWait(Exception):
    def __init__(self, packages, why):
        super().__init__(why)
        self.packages = packages
        self.why = why


class Requeue(Exception):
    """Checkboxes were inserted before the current one (UI verify scene mismatch): pick the next pending box again."""


class TicketDriver:
    def __init__(self, cfg, design, store, package):
        self.cfg, self.design, self.store, self.pkg = cfg, design, store, package
        if package not in design.tickets:
            raise SystemExit(f"{package} 는 설계 계획에 없다")
        self.t = design.tickets[package]
        self.st = store.ticket(package)
        self.gate = Gate(cfg, store, self.log)
        self.jira = Jira(cfg)
        self.handle = os.environ.get("ORCA_TERMINAL_HANDLE", "")
        self.progress = 0  # bumped whenever a worker or checkbox finishes; resets the transient-retry count

    # ---- bookkeeping
    def log(self, text):
        self.store.event(self.pkg, text)
        print(f"{now_iso()} {self.pkg} {text}", flush=True)

    def save(self):
        self.store.save(self.st)

    def box(self, n):
        return next(c for c in self.st["checkboxes"] if c["index"] == n)

    def work(self, n):
        d = os.path.join(self.cfg.work_dir, self.pkg, n if isinstance(n, str) else f"{n:02d}")
        os.makedirs(d, exist_ok=True)
        return d

    def integ(self):
        """The integration review record (None when the review is off), shaped like a checkbox record."""
        if not B.integration_enabled(self.cfg):
            return self.st.get("integration")
        return self.st.setdefault("integration", {"index": B.INTEGRATION, "phase": "pending"})

    def rec(self, box):
        return self.integ() if box == B.INTEGRATION else self.box(box)

    def records(self):
        ig = self.st.get("integration")
        return list(self.st.get("checkboxes") or []) + ([ig] if ig else [])

    def goals(self):
        """The ticket's 검증 목표: the design's, else the Jira body's 「검증 목표」 section (read once, kept in state)."""
        g = self.st.get("goals")
        if g is not None:
            return g["items"]
        items, source = list(self.t.goals), "design"
        if not items and self.jira.enabled and self.t.jira:
            try:
                items, source = self.jira.goals(self.t.jira), "jira"
            except DriverError as e:
                self.log(f"Jira 본문의 검증 목표를 읽지 못함 — {e.message[:200]}")
        if not items:
            source = "none"
            self.log("검증 목표 없음 — 통합 리뷰·누적 검증은 기본 관점만 본다")
        self.st["goals"] = {"items": items, "source": source}
        self.save()
        return items

    def notify(self, text):
        if self.cfg.get("notify", True):
            run(["osascript", "-e", f'display notification {quote_as(text)} with title "{self.cfg["project"]} 드라이버"'],
                timeout=10)

    # ---- entry
    def run(self):
        lock = TicketLock(self.store, self.pkg)
        if not lock.acquire():
            print(f"{self.pkg}: 다른 드라이버 프로세스가 이 티켓을 몰고 있다 (중복 기동)")
            return 3
        try:
            for e in self.st.get("exceptions") or []:
                if not e.get("resolved"):
                    e["resolved"] = now_iso()
            self.st.update(status=RUNNING, driver_terminal_handle=self.handle or self.st.get("driver_terminal_handle"),
                           pid=os.getpid())
            self.save()
            self.drive()
            return 0
        except BoxWait as w:
            self.st.update(status=BOX_WAITING, waiting_on=",".join(w.packages))
            self.save()
            self.log(f"체크박스 선행 대기 — {w.why}")
            return 0
        except DriverError as e:
            self.block(e)
            return 2
        except Exception as e:  # a driver bug must not leave the ticket looking alive under "running"
            self.block(DriverError("드라이버 오류", f"{type(e).__name__}: {e}"))
            raise
        finally:
            lock.release()
            try:
                P.write(self.cfg, self.design, self.store, orca.live_handles())
            except Exception as e:  # the plan is a view; never hide the real outcome behind it
                print(f"plan.json 갱신 실패: {e}")

    def drive(self):
        """Every step is resumable, so a transient Orca error re-enters the same step after 60/120/240 s; the ticket
        blocks on the fourth failure in a row without progress, marked retryable for the scheduler's auto-resolve."""
        failures, mark = 0, self.progress
        while True:
            try:
                self.start_ticket()
                self.ensure_run()
                self.precreate_tasks()
                self.contract_check()
                self.run_boxes()
                if self.integration_review():
                    self.precreate_tasks()
                    self.run_boxes()
                self.remeasure()
                self.collect_cumulative(wait=True)
                self.finish_ticket()
                return
            except DriverError as e:
                if not orca.transient(e):
                    raise
                if self.progress != mark:
                    failures, mark = 0, self.progress
                if failures >= len(RETRY_DELAYS):
                    raise DriverError(e.kind, f"{len(RETRY_DELAYS)}회 재시도 후에도 실패 — {e.message}", retryable=True)
                delay = RETRY_DELAYS[failures]
                failures += 1
                self.log(f"일시 오류 — {delay}초 뒤 같은 단계 재시도 {failures}/{len(RETRY_DELAYS)}: {e.message[:300]}")
                time.sleep(delay)

    def run_boxes(self):
        while True:
            c = next((c for c in self.st["checkboxes"] if c.get("phase") not in ("done", "deferred")), None)
            if not c:
                return
            self.reload_config(c)
            if not c.get("dispatch"):  # a boundary: nothing of this checkbox is running on the work tree yet
                self.boundary_rebase(c["index"])
            try:
                self.process_checkbox(c)
            except Requeue:
                self.precreate_tasks()
                continue
            self.start_cumulative(c)
            self.collect_cumulative()

    def block(self, e):
        self.st.setdefault("exceptions", []).append({"kind": e.kind, "message": e.message, "at": now_iso(),
                                                     "retryable": e.retryable})
        self.st["status"] = BLOCKED
        self.save()
        self.log(f"멈춤 [{e.kind}] {e.message}")
        self.store.alert(self.pkg, "blocked", f"[{e.kind}] {e.message}", exception_kind=e.kind,
                         retryable=e.retryable, terminal=self.handle)
        self.notify(f"{self.pkg} 멈춤 [{e.kind}] {e.message[:80]}")

    # ---- start
    def start_ticket(self):
        st, t = self.st, self.t
        if st.get("worktree") and os.path.isdir(st["worktree"]):
            return
        if not t.branch:
            raise DriverError("착수 시트", f"{self.pkg} 의 브랜치 이름이 없다 (order.md 스택 체인 · jira-map)")
        repo = self.cfg.repo(t.repo)
        states = self.store.all(self.design.order)
        base, problem = base_for(self.design, states, self.cfg, self.pkg)
        if problem:
            raise DriverError("환경", problem)
        if not base.get("sha"):  # a repo base: the local branch of that name may be months behind origin
            base = dict(base, sha=self.fetch_base(repo, base["ref"]))
        repo_name = repo.get("orca_repo") or t.repo
        parent = self.project_worktree(repo, repo_name)
        found = orca.find_worktree(t.branch)
        if found:
            path = found["path"]
            if parent and found.get("parentWorktreeId") != parent:
                raise DriverError("환경", f"브랜치 {t.branch} 의 워크트리 {path} 가 프로젝트 워크트리 "
                                        f"{self.cfg['project_worktree']} 아래가 아니다 — 그 아래로 옮긴 뒤 재기동")
            if run(["git", "-C", path, "merge-base", "--is-ancestor", base["sha"], "HEAD"]).returncode != 0:
                raise DriverError("환경", f"재사용할 워크트리 {path} 의 HEAD 가 베이스 {base['ref']} @{base['sha'][:10]} 를 "
                                        "포함하지 않는다 — 변경이 없으면 베이스로 reset, 있으면 리베이스한 뒤 재기동")
            self.log(f"워크트리 재사용 {path}")
        else:
            name = (t.jira or t.branch) if parent else t.branch
            path = orca.worktree_create(repo_name, name, t.branch, base["sha"], parent=parent)
            actual = git(path, "rev-parse", "--abbrev-ref", "HEAD")
            if actual != t.branch:  # Orca suffixes a --branch name that is already taken
                raise DriverError("환경", f"워크트리 {path} 가 브랜치 {actual} 로 만들어졌다 ({t.branch} 가 이미 있다)")
            self.log(f"워크트리 생성 {path} (베이스 {base['ref']} @{base['sha'][:10]})")
        st.update(worktree=path, branch=t.branch, base=base, base_sha=git(path, "rev-parse", "HEAD"),
                  started_at=st.get("started_at") or now_iso())
        if not st.get("checkboxes"):
            st["checkboxes"] = [{"index": b.index, "title": b.title, "phase": "pending",
                                 "kind": B.kind(self.cfg, t, b)} for b in t.boxes]
        self.save()
        for cmd in repo.get("install") or []:
            r = run(cmd if isinstance(cmd, list) else shlex.split(cmd), cwd=path, timeout=1800)
            if r.returncode != 0:
                raise DriverError("환경", f"준비 명령 실패 `{cmd}`: {(r.stderr or r.stdout).strip()[-300:]}")
        self.jira.transition(t.jira, (self.cfg["jira"] or {}).get("progress_status"))

    def project_worktree(self, repo, repo_name):
        """Orca id of the repo's top-level project worktree (config project_worktree), created when missing the way a
        project worktree is made by hand: Orca's default branch name, from refs/remotes/origin/<repo base>, no parent."""
        name = self.cfg.get("project_worktree")
        if not name:
            return None
        info = orca.repo_info(repo_name)
        if not info:
            raise DriverError("환경", f"Orca 에 저장소 {repo_name} 이 없다")

        def lookup():
            return next((w for w in orca.worktrees() if w.get("repoId") == info["id"] and w.get("displayName") == name
                         and not w.get("parentWorktreeId")), None)
        found = lookup()
        if found:
            return found["id"]
        ref = repo_base(self.design, self.cfg, self.t.repo, self.pkg)
        if not ref:
            raise DriverError("환경", f"프로젝트 워크트리 {name} 의 베이스를 모름 — config repos.{self.t.repo}.base")
        ref = ref.removeprefix("origin/")
        self.fetch_base(repo, ref)
        path = orca.worktree_create(repo_name, name, None, f"refs/remotes/origin/{ref}", no_parent=True)
        found = lookup()
        if not found:
            raise DriverError("환경", f"프로젝트 워크트리 {name} 을 만들었지만({path}) Orca 목록에서 찾지 못했다")
        self.log(f"프로젝트 워크트리 생성 {path} (베이스 origin/{ref})")
        return found["id"]

    def fetch_base(self, repo, ref):
        """sha of origin/<ref> right after fetching it, in the repo's own checkout."""
        name = self.t.repo
        src = repo.get("path") or orca.repo_path(repo.get("orca_repo") or name)
        if not src:
            raise DriverError("환경", f"저장소 {name} 의 로컬 경로를 모른다 — config repos.{name}.path")
        branch = ref.removeprefix("origin/")
        r = run(["git", "-C", src, "fetch", "-q", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
                timeout=600)
        if r.returncode != 0:
            raise DriverError("환경", f"git fetch origin {branch} 실패 ({src}): {(r.stderr or r.stdout).strip()[-300:]}")
        return git(src, "rev-parse", f"refs/remotes/origin/{branch}^{{commit}}")

    def ensure_run(self):
        rid = self.st.get("run_id")
        if rid:
            try:
                orca.run_use(rid)
                return
            except orca.OrcaError as e:
                self.log(f"Run {rid} 을 이어 쓸 수 없다 ({e.code or e.message[:60]}) — 새 Run 으로 다시 등록")
                for c in self.records():
                    if c.get("phase") not in ("done", "skipped"):
                        c.pop("tasks", None)
                        c.pop("dispatch", None)
                        c.pop("done", None)
                for c in (self.st.get("cumulative") or {}).values():
                    if c.get("phase") == "running":  # started again in the new Run
                        c.update(phase="gated", tasks={}, dispatch={})
        rid = orca.run_create(f"{self.cfg['project']} {self.pkg} 체크박스 드라이버")
        self.st["run_id"] = rid
        self.store.save_run(self.pkg, rid, self.handle)
        self.save()
        self.log(f"Run {rid}")

    def _boxes(self):
        return P.ticket_boxes(self.design, self.st, self.pkg)

    def precreate_tasks(self):
        """Every agent session of the ticket becomes an Orca task up front, with deps (방식 절차 4: 워커 생성 계획)."""
        rid, key_to_task = self.st["run_id"], {}
        known = orca.task_list(rid)
        for c in self.records():  # a task the Run no longer has is created again (unless that role finished)
            for k in [k for k, tid in (c.get("tasks") or {}).items() if tid not in known and not (c.get("done") or {}).get(k)]:
                c["tasks"].pop(k)
                (c.get("dispatch") or {}).pop(k, None)
        for c in self.records():  # a done checkbox may keep ids of an old Run; Orca refuses deps across Runs
            for k, tid in (c.get("tasks") or {}).items():
                if tid in known:
                    key_to_task[(c["index"], k)] = tid
        rows = B.ticket_sessions(self.cfg, self.t, self._boxes())
        by_key = {r["key"]: r for r in rows}
        created = 0
        for r in rows:
            c = self.rec(r["box"])
            if r["kind"] == "driver" or c.get("phase") in ("done", "deferred", "skipped", "fix") or \
                    (c.get("tasks") or {}).get(r["kind"]):
                continue
            if r["box"] == B.INTEGRATION:
                spec = self.write_integration_brief(r["kind"].removeprefix("review-"))
            else:
                box = next(b for b in self._boxes() if b.index == r["box"])
                spec = self.write_brief(c, box, r["kind"])
            deps = [key_to_task.get((by_key[d]["box"], by_key[d]["kind"])) for d in r["deps"] if d in by_key]
            tid = orca.task_create(rid, r["key"], spec, [d for d in deps if d])
            c.setdefault("tasks", {})[r["kind"]] = tid
            key_to_task[(r["box"], r["kind"])] = tid
            created += 1
        if created:
            self.save()
            self.log(f"태스크 {created}개 사전 등록")

    def write_brief(self, c, box, kind):
        work = self.work(c["index"])
        wt = self.st["worktree"]
        if kind == "impl":
            tcs = expand_tcs(box.title, self.cfg["tc_pattern"]) if box.prefix == "test" else []
            rows = briefs.tc_rows_text(self.design, tcs) if tcs else ""
            if rows:
                write_text(os.path.join(work, "tc-rows.md"), rows + "\n")
            scene = briefs.ui_scene(self.cfg, self.design, self.t, wt, work, self.goals()) \
                if self.is_verify(c) and self.cfg.is_ui(self.pkg) else ""
            text = briefs.impl(self.cfg, self.design, self.t, box, wt, work, rows, extra=self.fix_extra(c) + scene)
        elif kind == "judge":
            text = briefs.judge(self.cfg, self.design, self.t, box, wt, work)
        else:
            text = briefs.review(self.cfg, self.design, self.t, box, wt, work, kind.removeprefix("review-"),
                                 extra=self.fix_extra(c, review=True))
        path = os.path.join(work, f"{kind}-brief.md")
        write_text(path, text)
        return path

    def fix_extra(self, c, review=False):
        if c.get("fix_source") == "ui_scene":
            return briefs.scene_fix_extra(c.get("scene_file") or "", review=review)
        if c.get("fix_source") != "integration":
            return ""
        files = [briefs.integration_path(self.work(B.INTEGRATION), r) for r in (self.integ() or {}).get("findings", {})]
        return briefs.fix_extra(files, self.goals(), review=review)

    def write_integration_brief(self, name):
        work = self.work(B.INTEGRATION)
        text = briefs.integration_review(self.cfg, self.design, self.t, self.st["worktree"], work, name,
                                         self.st.get("base_sha") or "HEAD", self.goals())
        path = os.path.join(work, f"review-{name}-brief.md")
        write_text(path, text)
        return path

    # ---- config edits take effect at checkbox boundaries
    def reload_config(self, c):
        """Re-read config.json before checkbox c starts; a checkbox already under way keeps its sessions. Sessions the
        new config drops from not-yet-started checkboxes are closed, new ones are registered."""
        if c.get("dispatch"):
            return
        before = self.cfg.raw
        error = self.cfg.reload()
        if error:
            self.log(f"경고: config.json 다시 읽기 실패 — 직전 설정 유지: {error}")
            return
        if self.cfg.raw == before:
            return
        self.log(f"#{c['index']} 부터 바뀐 config.json 적용")
        boxes = {b.index: b for b in self._boxes()}
        kinds = {(r["box"], r["kind"]) for r in B.ticket_sessions(self.cfg, self.t, list(boxes.values()))}
        for box in self.st["checkboxes"]:
            if box.get("phase") in ("done", "deferred") or box.get("dispatch") or box["index"] not in boxes:
                continue
            box["kind"] = B.kind(self.cfg, self.t, boxes[box["index"]])
            for kind in [k for k in (box.get("tasks") or {}) if (box["index"], k) not in kinds]:
                orca.task_update(self.st["run_id"], box["tasks"].pop(kind), "completed",
                                 "cbd: 건너뜀 — config.json 변경으로 빠진 세션")
        ig = self.st.get("integration")
        if ig and not ig.get("dispatch") and ig.get("phase") == "pending":  # the review has not started: follow config
            for kind in [k for k in (ig.get("tasks") or {}) if (B.INTEGRATION, k) not in kinds]:
                orca.task_update(self.st["run_id"], ig["tasks"].pop(kind), "completed",
                                 "cbd: 건너뜀 — config.json 변경으로 빠진 세션")
        self.save()
        self.precreate_tasks()

    # ---- checkboxes
    def process_checkbox(self, c):
        key = self.cfg.box_key(self.pkg, c["index"])
        states = self.store.all(self.design.order)
        need = [p for p in self.cfg["box_prereqs"].get(key, []) if states.get(p, {}).get("status") not in DONE_STATUSES]
        if need:
            raise BoxWait(need, f"{key} 는 {', '.join(need)} verified 뒤")
        if key in self.cfg["approval_boxes"] and not self.store.approved(self.pkg, c["index"]):
            raise DriverError("사람 승인", f"{key} 는 승인이 필요하다 — cbd {self.cfg.key} approve {self.pkg} {c['index']}")
        self.st.pop("waiting_on", None)
        self.log(f"#{c['index']} 시작 — {c['title'][:70]}")
        if self.is_verify(c):
            self.verify_gate(states)
        if c["kind"] == B.DRIVER_VERIFY:
            self.verify_box(c, states)
        elif c["kind"] == B.DRIVER_PIN:
            self.pin_box(c, states)
        else:
            self.agent_box(c)
        c["phase"] = "done"
        c["finished_at"] = now_iso()
        self.progress += 1
        self.save()
        try:
            self.jira.check_box(self.t.jira, c["title"])
        except DriverError as e:
            self.log(f"#{c['index']} Jira 체크 실패 — {e.message}")
        self.log(f"#{c['index']} 완료 {c.get('commit', '')[:10]}")
        P.write(self.cfg, self.design, self.store, orca.live_handles())

    def subject(self, c):
        return f"[{self.t.jira}] {c['title']}" if self.t.jira else c["title"]

    def verify_gate(self, states):
        prev = self.t.chain_prev
        if prev and states.get(prev, {}).get("status") not in DONE_STATUSES:
            raise BoxWait([prev], f"첫 verify 는 체인 앞 {prev} verified 뒤 (verify 게이트)")

    def verify_box(self, c, states):
        wt, rc = self.st["worktree"], self.cfg.repo(self.t.repo)
        out, ok = [], True
        tcs = self.t.tcs
        if rc.get("test_cmd"):
            if not tcs:
                raise DriverError("검증 실패", f"{self.pkg} verify 대상 TC 를 찾지 못함 (완료 기준: {self.t.criteria[:80]})")
            res = self.run_cmd(rc["test_cmd"], tcs, wt, c, "verify")
            out.append(res[1])
            ok &= res[0]
        if rc.get("typecheck_cmd") and (self.cfg.is_ui(self.pkg) or rc.get("typecheck_always")):
            res = self.run_cmd(rc["typecheck_cmd"], [], wt, c, "typecheck")
            out.append(res[1])
            ok &= res[0]
        if not out:
            raise DriverError("환경", f"repos.{self.t.repo}.test_cmd 가 없어 verify 를 잴 수 없다")
        if not ok:
            raise DriverError("검증 실패", f"#{c['index']} verify — " + " / ".join(out))
        git(wt, "commit", "--allow-empty", "-m", self.subject(c), "-m", "\n".join(out))
        c["commit"] = git(wt, "rev-parse", "HEAD")

    def run_cmd(self, template, tcs, cwd, c, label):
        args = []
        for a in template:
            args += tcs if a == "{tcs}" else [a.replace("{worktree}", cwd)]
        r = run(args, cwd=cwd, timeout=3600)
        log = os.path.join(self.cfg.p("logs"), self.pkg, f"{c['index']:02d}-{label}.log")
        write_text(log, f"$ {shlex.join(args)}\n{r.stdout}\n{r.stderr}")
        tail = (r.stdout or r.stderr).strip().splitlines()[-1:] or [""]
        return r.returncode == 0, f"{label}: {'통과' if r.returncode == 0 else f'실패(exit {r.returncode})'} — {tail[0][:200]} (로그 {log})"

    def pin_box(self, c, states):
        producer = self.t.pin
        head = states.get(producer, {}).get("verified_head")
        if states.get(producer, {}).get("status") not in DONE_STATUSES or not head:
            raise BoxWait([producer], f"핀 대상 {producer} verified 뒤")
        wt, rc = self.st["worktree"], self.cfg.repo(self.t.repo)
        sub = rc.get("pin_path")
        if not sub:
            raise DriverError("환경", f"repos.{self.t.repo}.pin_path 가 없어 핀을 올릴 수 없다")
        sp = os.path.join(wt, sub)
        if not os.path.exists(os.path.join(sp, ".git")):
            git(wt, "submodule", "update", "--init", sub, timeout=600)
        # Producer branches are not pushed while implementing: take the head from the producer's worktree first.
        src = states.get(producer, {}).get("worktree")
        branch = self.design.tickets[producer].branch if producer in self.design.tickets else None
        if src and branch and os.path.isdir(src):
            git(sp, "fetch", "--quiet", src, f"refs/heads/{branch}", timeout=600)
        if run(["git", "-C", sp, "cat-file", "-e", f"{head}^{{commit}}"]).returncode != 0:
            git(sp, "fetch", "--quiet", "origin", timeout=600)
        git(sp, "checkout", "--quiet", head)
        git(wt, "add", sub)
        empty = run(["git", "-C", wt, "diff", "--cached", "--quiet"]).returncode == 0  # pin already at head (chain prev)
        r = run(["git", "-C", wt, "commit", *(["--allow-empty"] if empty else []), "-m", self.subject(c),
                 "-m", f"{sub} → {producer} verified head {head}"])
        if r.returncode != 0:
            raise DriverError("환경", f"핀 커밋 실패 (훅?): {(r.stderr or r.stdout).strip()[-300:]}")
        c["commit"] = git(wt, "rev-parse", "HEAD")

    # ---- agent checkbox: implement -> review x N -> judge
    def agent_box(self, c):
        wt = self.st["worktree"]
        work = self.work(c["index"])
        p = briefs.paths(work)
        done, disp = c.setdefault("done", {}), c.setdefault("dispatch", {})
        impl = self.cfg["impl"]
        if not done.get("impl"):
            if not disp.get("impl"):
                c["notes"] = self.gate.wait([impl.get("pool", impl["agent"])], f"#{c['index']} 착수")
                c["head_before"] = git(wt, "rev-parse", "HEAD")
                self.launch(c, "impl")
            self.wait_task(c, "impl")
        if self.is_verify(c) and self.cfg.is_ui(self.pkg):
            self.ui_scene(c, work)
        if run(["git", "-C", wt, "diff", "--cached", "--quiet"]).returncode == 0:
            if not self.is_verify(c):
                raise DriverError("정체", f"{c['index']}번 구현이 스테이지를 남기지 않았다")
            self.empty_verify(c, p)
            self.close_box_workers(c)
            return
        if not os.path.exists(p["draft"]):
            raise DriverError("정체", f"{c['index']}번 구현이 커밋 메시지 초안을 남기지 않았다 ({p['draft']})")
        reviewers = self.cfg["reviewers"]
        pending = [r for r in reviewers if not done.get(f"review-{r['name']}")]
        if pending:
            c.setdefault("notes", []).extend(self.gate.wait(sorted({r.get("pool", r["agent"]) for r in pending}),
                                                            f"#{c['index']} 리뷰"))
            for r in pending:
                if not disp.get(f"review-{r['name']}"):
                    self.launch(c, f"review-{r['name']}")
            for r in pending:
                self.wait_task(c, f"review-{r['name']}", accept_file=briefs.findings_path(work, r["name"]))
        if not done.get("judge"):
            if not disp.get("judge"):
                self.launch(c, "judge")
            self.wait_task(c, "judge")
        self.check_commit(c)
        self.close_box_workers(c)

    def close_box_workers(self, c):
        for k, d in (c.get("dispatch") or {}).items():
            if k != "judge" and d:
                orca.worker_release(d)
        if c.get("impl_handle"):  # kept open for the judge (which ran in it, or not); the checkbox is over (루프 10 종료)
            orca.terminal_close(c["impl_handle"])

    @staticmethod
    def is_verify(c):
        return re.match(r"verify[(:!]", c["title"]) is not None

    def empty_verify(self, c, p):
        """A verify box whose implementer changed no code: nothing to review, so the reviewers and the judge are
        skipped and the driver commits the implementer's draft with --allow-empty (the test result is the evidence)."""
        wt = self.st["worktree"]
        for key in ("test", "draft"):
            if not os.path.exists(p[key]):
                raise DriverError("정체", f"{c['index']}번 verify 구현이 스테이지 없이 {os.path.basename(p[key])} 도 "
                                        f"남기지 않았다 ({p[key]})")
        if git(wt, "rev-parse", "HEAD") == c.get("head_before"):  # a restart after the commit only re-checks it
            with open(p["draft"], encoding="utf-8") as f:
                first = f.readline().strip()
            if first != self.subject(c):
                raise DriverError("검증 실패", f"{c['index']}번 초안 첫 줄이 커밋 제목과 다르다: {first[:100]}")
            self.skip_sessions(c, "verify 에 스테이지가 없다 — 코드 변경 없음, 드라이버가 초안으로 빈 커밋")
            r = run(["git", "-C", wt, "commit", "--allow-empty", "-F", p["draft"]])
            if r.returncode != 0:
                raise DriverError("환경", f"{c['index']}번 빈 커밋 실패 (훅?): {(r.stderr or r.stdout).strip()[-300:]}")
        self.check_commit(c)

    def skip_sessions(self, c, why):
        """Reviewers and judge that will not run: their precreated tasks are closed and the plan shows them skipped."""
        rid, skipped = self.st["run_id"], c.setdefault("skipped", {})
        kinds = [f"review-{r['name']}" for r in self.cfg["reviewers"]] + ["judge"]
        for kind in kinds:
            if kind in skipped or (c.get("done") or {}).get(kind):
                continue
            tid = (c.get("tasks") or {}).get(kind)
            if tid:
                orca.task_update(rid, tid, "completed", f"cbd: 건너뜀 — {why}")
            skipped[kind] = why
            self.save()
        self.log(f"#{c['index']} 리뷰·판정 건너뜀 ({', '.join(kinds)}) — {why}")

    def launch(self, c, kind, retry_of=None):
        """Start the worker of `kind` (impl | review-<name> | judge) and record its dispatch."""
        disp = c.setdefault("dispatch", {})
        title = f"{self.pkg}#{c['index']}"
        if kind == "impl":
            impl = self.cfg["impl"]
            w = self.start_worker(c, kind, impl, f"{title} 구현", keep_open=True, retry_of=retry_of)
            c["impl_handle"] = w["handle"]
        elif kind == "judge":
            w = self.start_judge(c, retry_of)
        elif kind == "contract":
            w = self.start_worker(c, kind, self.cfg["impl"], f"{self.pkg} 실서버 대조", retry_of=retry_of)
        else:
            r = next(r for r in self.cfg["reviewers"] if f"review-{r['name']}" == kind)
            w = self.start_worker(c, kind, r, f"{title} 리뷰 {r['name']}", retry_of=retry_of)
        disp[kind] = w["dispatch"]
        c["dispatched_at"] = now_iso()
        self.save()

    def start_judge(self, c, retry_of=None):
        """The judge runs in the implementing terminal (it remembers the work); a new session judges from the files."""
        impl, h = self.cfg["impl"], c.get("impl_handle")
        title = f"{self.pkg}#{c['index']} 판정"
        if h and h in orca.live_handles():
            try:
                return self.start_worker(c, "judge", impl, title, retry_of=retry_of, terminal=h)
            except DriverError as e:  # (b) any refusal to reuse the terminal has the new-session path
                self.log(f"#{c['index']} 구현 터미널 {h} 재사용 실패 — 새 세션으로 판정: {e.message[:300]}")
        else:
            self.log(f"#{c['index']} 구현 터미널이 닫혀 판정을 새 세션으로 띄운다")
        return self.start_worker(c, "judge", impl, title, retry_of=retry_of)

    def start_worker(self, c, kind, agent, title, keep_open=False, retry_of=None, terminal=None):
        """worker-start, unless an earlier start whose answer was lost already dispatched the task: starting it
        again would put two workers on one worktree."""
        rid, tid = self.st["run_id"], c["tasks"][kind]
        if orca.task_list(rid).get(tid, {}).get("status") in ("dispatched", "completed"):
            w = self.current_worker(tid)
            if w:
                self.log(f"#{c['index']} {kind} 는 이미 기동돼 있다 — {w.get('dispatchId')} 를 이어 받는다")
                return {"dispatch": w.get("dispatchId"), "handle": w.get("terminal")}
        return orca.worker_start(rid, tid, self.st["worktree"], agent["agent"], agent.get("model"), agent.get("effort"),
                                 title, keep_open=keep_open, retry_of=retry_of, terminal=terminal)

    def current_worker(self, tid, dispatch=None):
        rows = [w for w in orca.worker_list(self.st["run_id"]) if w.get("taskId") == tid]
        if dispatch:
            return next((w for w in rows if w.get("dispatchId") == dispatch), None)
        return next((w for w in rows if w.get("state") not in WORKER_GONE), None)

    def check_commit(self, c):
        wt = self.st["worktree"]
        head = git(wt, "rev-parse", "HEAD")
        if head == c.get("head_before"):
            raise DriverError("정체", f"{c['index']}번 판정이 커밋을 만들지 않았다")
        subj = git(wt, "log", "-1", "--format=%s")
        if _norm(c["title"]) not in _norm(subj):
            raise DriverError("검증 실패", f"{c['index']}번 커밋 제목이 체크박스와 다르다: {subj[:100]}")
        dirty = [l for l in git(wt, "status", "--porcelain", "--untracked-files=no").splitlines() if l.strip()]
        if dirty:
            raise DriverError("검증 실패", f"{c['index']}번 커밋 뒤 작업 트리가 깨끗하지 않다: {', '.join(dirty[:5])}")
        c["commit"] = head

    def wait_task(self, c, kind, accept_file=None):
        """Wait until the task of `kind` completes. Mail is the wake-up; the task status is the truth, so a done
        signal received before a restart is not lost. Questions are saved to files and answered from files.
        A failed task or a worker gone without worker_done is started again with --retry-of, WORKER_RETRIES times."""
        rid, tid = self.st["run_id"], c["tasks"][kind]
        started = now_ts()
        while True:
            task = orca.task_list(rid).get(tid, {})
            status = task.get("status")
            if status == "completed":
                c.setdefault("done", {})[kind] = now_iso()
                self.progress += 1
                self.save()
                return
            why = f"태스크 실패: {str(task.get('result', ''))[:200]}" if status == "failed" else self.worker_gone(c, kind)
            if why:
                if accept_file and os.path.exists(accept_file):
                    self.log(f"#{c['index']} {kind} 가 실패로 끝났지만 결과 파일이 있어 수용 ({accept_file})")
                    c.setdefault("done", {})[kind] = now_iso()
                    self.progress += 1
                    self.save()
                    return
                retries = c.setdefault("retries", {})
                if retries.get(kind, 0) >= WORKER_RETRIES:
                    kind_name = "리뷰어 불가" if kind.startswith("review") else "정체"
                    raise DriverError(kind_name, f"#{c['index']} {kind} {WORKER_RETRIES}회 재시도 후에도 {why}")
                retries[kind] = retries.get(kind, 0) + 1
                prev = c["dispatch"].get(kind)
                if status != "failed":  # Orca reopens only a failed task with --retry-of
                    orca.task_update(rid, tid, "failed", f"cbd: {why}")
                if kind == "impl" and c.get("impl_handle"):
                    orca.terminal_close(c["impl_handle"])
                self.log(f"#{c['index']} {kind} 워커 재시도 {retries[kind]}/{WORKER_RETRIES} (--retry-of {prev}) — {why}")
                self.launch(c, kind, retry_of=prev)
                started = now_ts()
                continue
            if now_ts() - started > int(self.cfg["quiet_limit_sec"]) and not self._has_open_question():
                raise DriverError("정체", f"#{c['index']} {kind} 가 {int(self.cfg['quiet_limit_sec']) // 60}분 동안 끝나지 않았다")
            self.pump(rid)
            if kind != "cumulative":
                self.collect_cumulative()

    def worker_gone(self, c, kind):
        """Why the worker of a not-yet-finished task is gone (exited or lost without worker_done), or None."""
        d = c.get("dispatch", {}).get(kind)
        w = self.current_worker(c["tasks"][kind], d) if d else None
        if not w or w.get("state") == "succeeded":
            return None
        if w.get("state") in WORKER_GONE or w.get("liveness") in ("exited", "lost"):
            return f"워커 {d} 가 worker_done 없이 끝났다 (state {w.get('state')}, liveness {w.get('liveness')})"
        return None

    def _has_open_question(self):
        return any(not q.get("answered") for q in self.st.get("questions") or [])

    def pump(self, rid):
        """One mailbox wait: store questions, send answers that appeared as files, close tasks finished outside."""
        for m in orca.check_wait(rid, int(self.cfg["mail_wait_ms"])):
            if m.get("type") in ("question", "escalation"):
                d = self.store.reply_dir(rid)
                path = os.path.join(d, f"{m['id']}.question.md")
                write_text(path, f"# {m['subject']}\n\n- 보낸 이: {m['from']}\n- 태스크: {m['payload'].get('taskId', '')}"
                                 f"\n\n{m['body']}\n")
                self.st.setdefault("questions", []).append({"msg_id": m["id"], "path": path, "at": now_iso()})
                self.save()
                self.log(f"질문 대기 — {path}")
                self.store.alert(self.pkg, m["type"], (m["body"] or m["subject"]), question=path, msg_id=m["id"],
                                 answer=os.path.join(d, f"{m['id']}.md"), worker=m["from"])
                self.notify(f"{self.pkg} 워커 질문: {(m['body'] or m['subject'])[:60]}")
        d = self.store.reply_dir(rid)
        for q in self.st.get("questions") or []:
            ans = os.path.join(d, f"{q['msg_id']}.md")
            if not q.get("answered") and os.path.exists(ans):
                with open(ans, encoding="utf-8") as f:
                    orca.reply(q["msg_id"], f.read().strip())
                q["answered"] = now_iso()
                self.save()
                self.log(f"답장 보냄 {q['msg_id']}")
        for name in os.listdir(d):
            m = re.match(r"task_(.+)\.complete\.md$", name)
            if m and not os.path.exists(os.path.join(d, name + ".sent")):
                with open(os.path.join(d, name), encoding="utf-8") as f:
                    orca.task_update(rid, m.group(1), "completed", f.read().strip())
                write_text(os.path.join(d, name + ".sent"), now_iso())

    # ---- 같은 저장소 체인: checkbox-boundary rebase (방식 · 같은 저장소 체인)
    def nearer(self, new_from, old_from):
        """Is the verified chain ticket `new_from` nearer to this ticket than `old_from` (None = the repo base)?
        The same ticket counts as nearer when its verified head moved."""
        chain = self.design.chains.get(self.t.repo, [])
        pos = {p: i for i, p in enumerate(chain)}
        if new_from is None:
            return False
        return new_from == old_from or pos.get(new_from, -1) > pos.get(old_from, -1)

    def measure(self, n, label, only=None):
        """[(name, ok, text)] of the completion tests and the type check on the work tree; `only` limits the names."""
        wt, rc, out = self.st["worktree"], self.cfg.repo(self.t.repo), []
        c = {"index": n or 0}
        if rc.get("test_cmd") and self.t.tcs and (only is None or "test" in only):
            ok, text = self.run_cmd(rc["test_cmd"], self.t.tcs, wt, c, label)
            out.append(("test", ok, text))
        if rc.get("typecheck_cmd") and (only is None or "typecheck" in only):
            ok, text = self.run_cmd(rc["typecheck_cmd"], [], wt, c, f"{label}-typecheck")
            out.append(("typecheck", ok, text))
        return out

    def boundary_rebase(self, n=None):
        """When a nearer verified chain head than the ticket's base exists (a chain ticket that was in progress at the
        start is verified now), move the branch onto it before the next checkbox. What was green before the rebase
        (completion tests, type check) is measured again after it. A conflict or a red run stops the ticket — never
        resolved by the driver. Returns True when the branch moved."""
        st = self.st
        wt, old = st.get("worktree"), st.get("base") or {}
        if not wt or not st.get("base_sha"):
            return False
        states = self.store.all(self.design.order)
        new, problem = base_for(self.design, states, self.cfg, self.pkg)
        if problem or not new or not new.get("sha") or new["sha"] == old.get("sha") or \
                not self.nearer(new.get("from"), old.get("from")):
            return False
        if run(["git", "-C", wt, "merge-base", "--is-ancestor", new["sha"], "HEAD"]).returncode == 0:
            st.update(base=new, base_sha=new["sha"])  # already on it (a restart right after the rebase)
            self.save()
            return False
        dirty = [l for l in git(wt, "status", "--porcelain", "--untracked-files=no").splitlines() if l.strip()]
        if dirty:
            raise DriverError("리베이스 충돌", f"체크박스 경계인데 작업 트리가 깨끗하지 않아 {new['from']} 위로 옮기지 못함: "
                                         f"{', '.join(dirty[:5])}")
        old_base, head = st["base_sha"], git(wt, "rev-parse", "HEAD")
        self.log(f"경계 리베이스 — {old.get('from') or old.get('ref')} @{old_base[:10]} → {new['from']} verified 헤드 "
                 f"@{new['sha'][:10]}")
        before = self.measure(n, "rebase-before")
        green = {name for name, ok, _ in before if ok}
        r = run(["git", "-C", wt, "rebase", "--keep-empty", "--empty=keep", "--onto", new["sha"], old_base], timeout=600)
        if r.returncode != 0:
            run(["git", "-C", wt, "rebase", "--abort"])
            raise DriverError("리베이스 충돌", f"{old_base[:10]}..{head[:10]} 를 {new['from']} verified 헤드 {new['sha'][:10]} "
                                         f"위로 옮기다 충돌 — 브랜치는 그대로 두었다(abort). 사람이 리베이스한 뒤 resolve: "
                                         f"{(r.stderr or r.stdout).strip()[-300:]}")
        olds = git(wt, "rev-list", "--reverse", f"{old_base}..{head}").split()
        news = git(wt, "rev-list", "--reverse", f"{new['sha']}..HEAD").split()
        moved = dict(zip(olds, news)) if len(olds) == len(news) else {}
        if not moved and olds:
            self.log(f"경고: 리베이스 전후 커밋 수가 다르다 ({len(olds)} → {len(news)}) — 체크박스 커밋 해시를 옮기지 못함")
        for c in st["checkboxes"]:
            if c.get("commit") in moved:
                c["commit"] = moved[c["commit"]]
        for rec in (st.get("cumulative") or {}).values():
            if rec.get("phase") == "gated" and rec.get("commit") in moved:
                rec["commit"] = moved[rec["commit"]]
        entry = {"at": now_iso(), "from": old, "onto": new, "old_head": head, "new_head": git(wt, "rev-parse", "HEAD"),
                 "before": [t for _, _, t in before]}
        st.setdefault("rebases", []).append(entry)
        st.update(base=new, base_sha=new["sha"])
        self.save()
        after = self.measure(n, "rebase-after", only=green) if green else []
        entry["after"] = [t for _, _, t in after] or ["리베이스 전 초록이던 측정 없음 — 재측정 생략"]
        self.save()
        red = [t for _, ok, t in after if not ok]
        if red:
            raise DriverError("검증 실패", "경계 리베이스 뒤 재검증 — " + " / ".join(red))
        self.progress += 1
        self.log(f"경계 리베이스 완료 {entry['new_head'][:10]} — 재측정: {' / '.join(entry['after'])}")
        return True

    # ---- 체크박스를 넘는 검증 §1: consumer start against the producer's real server
    def contract_spec(self):
        """{"producers", "apis", "source"} of the ticket's 실서버 대조, or None. config contract_checks.<PKG> overrides
        the design line (false turns it off)."""
        over = (self.cfg.get("contract_checks") or {}).get(self.pkg)
        if over is False:
            return None
        if over:
            prods = over.get("producer") or over.get("producers") or []
            return {"producers": [prods] if isinstance(prods, str) else list(prods), "apis": over.get("apis", ""),
                    "source": "config"}
        if self.t.contract:
            return {**self.t.contract, "source": "design"}
        return None

    def contract_check(self):
        """Before the first checkbox: one agent session starts the producer's verified head as an isolated stack and
        sends real requests to the APIs this ticket consumes. 「일치」 lets the ticket go on; anything else stops it
        (「계약 불일치」) — the consumer never invents the contract. A resolve checks again from the start."""
        spec = self.contract_spec()
        if not spec:
            return
        rec = self.st.get("contract") or {}
        if rec.get("phase") == "done":
            return
        if rec.get("phase") in ("mismatch", "failed"):
            rec = {"previous": rec.get("path")}
        if not rec:
            rec = {}
        rec.setdefault("index", "계약")
        rec.setdefault("phase", "pending")
        self.st["contract"] = rec
        states = self.store.all(self.design.order)
        need = [p for p in spec["producers"]
                if states.get(p, {}).get("status") not in DONE_STATUSES or not states.get(p, {}).get("verified_head")]
        if need:
            raise BoxWait(need, f"실서버 대조 대상 {', '.join(need)} verified 뒤")
        work = self.work("contract")
        path = briefs.contract_path(work)
        if not (rec.get("done") or {}).get("contract"):
            if not (rec.get("dispatch") or {}).get("contract"):
                impl = self.cfg["impl"]
                rec.setdefault("notes", []).extend(self.gate.wait([impl.get("pool", impl["agent"])], "실서버 대조"))
                if os.path.exists(path):  # a result of an earlier run must not be read as this one's
                    os.replace(path, path.replace(".md", f"-{now_ts()}.md"))
                producers = []
                for p in spec["producers"]:
                    pt = self.design.tickets.get(p)
                    repo = pt.repo if pt else None
                    producers.append({"package": p, "repo": repo, "head": states[p]["verified_head"],
                                      "worktree": states[p].get("worktree"), "branch": states[p].get("branch"),
                                      "stack_cmd": self.cfg["repos"].get(repo or "", {}).get("stack_cmd")})
                brief = os.path.join(work, "contract-brief.md")
                write_text(brief, briefs.contract(self.cfg, self.design, self.t, self.st["worktree"], work, spec,
                                                  producers))
                if not (rec.get("tasks") or {}).get("contract"):
                    rec.setdefault("tasks", {})["contract"] = orca.task_create(
                        self.st["run_id"], f"{self.pkg}#계약 대조 {B.impl_name(self.cfg)}", brief, [])
                rec.update(phase="running", producers={x["package"]: x["head"] for x in producers},
                           started_at=now_iso())
                self.log(f"실서버 대조 시작 — {', '.join(spec['producers'])} verified 헤드, {spec['apis'][:80]}")
                self.launch(rec, "contract")
            self.wait_task(rec, "contract", accept_file=path)
            for d in (rec.get("dispatch") or {}).values():
                if d:
                    orca.worker_release(d)
        verdict = verdict_of(path)
        rec.update(verdict=verdict, path=path, finished_at=now_iso())
        if verdict == "일치":
            rec["phase"] = "done"
            self.progress += 1
            self.save()
            self.log(f"실서버 대조 일치 — {path}")
            return
        rec["phase"] = "mismatch" if verdict else "failed"
        self.save()
        if verdict == "불일치":
            raise DriverError("계약 불일치", f"실서버 대조 불일치 ({path}) — 소비자 쪽에서 계약을 맞춰 발명하지 않는다. 생산자를 "
                                        f"고치거나 설계를 정정한 뒤 cbd {self.cfg.key} resolve {self.pkg} (대조를 다시 한다)")
        raise DriverError("환경" if verdict else "정체",
                          f"실서버 대조 {'판단 불가' if verdict else '결과 없음'} ({path}) — 스택·시드를 확인하고 resolve "
                          "(대조를 다시 한다)")

    # ---- 체크박스를 넘는 검증 §2: one scene of a UI ticket's verify
    def ui_scene(self, c, work):
        """Read the implementer's scene result. 일치 (or a person's acceptance) goes on to the commit; 불일치 inserts a
        fix checkbox before this verify and runs the verify again (Requeue), up to ui_scene_max_fix times; 판단 불가 and
        mismatches beyond that stop for a person (cbd accept-ui records an acceptance)."""
        path = briefs.scene_path(work)
        verdict = verdict_of(path)
        n = c["index"]
        if verdict is None:
            self.reset_box(c, "화면 대조 결과 없음")
            raise DriverError("정체", f"#{n} verify 가 화면 대조 결과를 남기지 않았다 ({path}) — resolve 하면 verify 를 다시 돈다")
        c["scene"] = {"verdict": verdict, "path": path}
        if verdict == "일치":
            self.save()
            return
        acc = self.store.ui_accepted(self.pkg, n)
        if acc:
            note = f"\n화면 대조: {verdict} — 사람 수용 ({acc.get('at')} {acc.get('by')}): {acc.get('reason')}\n결과: {path}\n"
            draft = briefs.paths(work)["draft"]
            if os.path.exists(draft):
                with open(draft, encoding="utf-8") as f:
                    text = f.read()
                if "사람 수용" not in text:
                    write_text(draft, text.rstrip("\n") + "\n" + note)
            c["scene"]["accepted"] = acc
            self.save()
            self.log(f"#{n} 화면 대조 {verdict} — 사람 수용 기록으로 진행: {acc.get('reason', '')[:80]}")
            return
        if run(["git", "-C", self.st["worktree"], "diff", "--cached", "--quiet"]).returncode != 0:
            self.reset_box(c, f"화면 대조 {verdict} 인데 스테이지가 있다")
            raise DriverError("검증 실패", f"#{n} 화면 대조 {verdict} 인데 verify 가 변경을 스테이지했다 — 스테이지를 비우고 resolve")
        accept = f"cbd {self.cfg.key} accept-ui {self.pkg} {n} '<사유>' 뒤 resolve"
        if verdict != "불일치":
            self.reset_box(c, f"화면 대조 {verdict}")
            raise DriverError("사람 판정", f"#{n} 화면 대조 {verdict} ({path}) — 기준(스냅숏·Figma 링크)을 마련하고 resolve, "
                                       f"고치지 않고 받아들이려면 {accept}")
        fixes = [x for x in self.st["checkboxes"] if x.get("fix_source") == "ui_scene" and x.get("fix_for") == n]
        if len(fixes) >= int(self.cfg.get("ui_scene_max_fix", 2)):
            self.reset_box(c, "화면 대조 불일치 — 사람 판정 대기")
            raise DriverError("사람 판정", f"#{n} 화면 대조 불일치가 수정 체크박스 {len(fixes)}개 뒤에도 남았다 ({path}) — "
                                       f"고칠 수 없는 차이면 {accept}, 더 고치려면 config ui_scene_max_fix 를 올리고 resolve")
        archived = self.reset_box(c, "화면 대조 불일치 — 수정 체크박스 뒤 다시")
        m = max(x["index"] for x in self.st["checkboxes"]) + 1
        title = self.cfg.get("ui_scene_fix_title") or "fix: 화면 대조 불일치 반영"
        self.st["checkboxes"].insert(self.st["checkboxes"].index(c), {
            "index": m, "title": title, "phase": "pending", "kind": B.AGENT, "fix_source": "ui_scene", "fix_for": n,
            "scene_file": archived})
        self.save()
        self.log(f"#{n} 화면 대조 불일치 — 수정 체크박스 #{m} 를 verify 앞에 넣고 verify 를 다시 돈다 ({archived})")
        self.store.alert(self.pkg, "ui_scene", f"#{n} 화면 대조 불일치 — 수정 체크박스 #{m} 로 처리", path=archived)
        try:
            self.jira.add_box(self.t.jira, title)
        except DriverError as e:
            self.log(f"Jira 체크리스트에 #{m} 추가 실패 — {e.message}")
        raise Requeue()

    def reset_box(self, c, why):
        """Put an agent checkbox back to pending: unfinished sessions are closed, the scene result is kept aside under
        a numbered name (so the next run's result is not confused with it). Returns that archived path."""
        rid = self.st.get("run_id")
        for kind, tid in (c.get("tasks") or {}).items():
            if tid and not (c.get("done") or {}).get(kind):
                orca.task_update(rid, tid, "completed", f"cbd: 건너뜀 — {why}")
        for kind, d in (c.get("dispatch") or {}).items():
            if d and kind != "judge":
                orca.worker_release(d)
        if c.get("impl_handle"):
            orca.terminal_close(c["impl_handle"])
        work = self.work(c["index"])
        path, archived = briefs.scene_path(work), ""
        if os.path.exists(path):
            k = 1 + sum(1 for f in os.listdir(work) if re.match(r"ui-scene-\d+\.md$", f))
            archived = os.path.join(work, f"ui-scene-{k}.md")
            os.replace(path, archived)
        for k in ("tasks", "dispatch", "done", "retries", "impl_handle", "head_before", "skipped", "notes"):
            c.pop(k, None)
        c["phase"] = "pending"
        c.setdefault("resets", []).append({"at": now_iso(), "why": why, "scene": archived})
        self.save()
        return archived

    # ---- ticket integration review (체크박스를 넘는 검증 §3)
    def integration_review(self):
        """Every reviewer reads the whole branch once against the 검증 목표. Findings add one fix checkbox at the end
        (returns True); none -> the review is done. Not while a checkbox is deferred (the ticket is not finished)."""
        ig = self.integ()
        if not ig or not B.integration_enabled(self.cfg) or ig.get("phase") in ("done", "skipped", "fix"):
            return False
        if any(c.get("phase") == "deferred" for c in self.st["checkboxes"]):
            return False
        self.precreate_tasks()
        work = self.work(B.INTEGRATION)
        done, disp = ig.setdefault("done", {}), ig.setdefault("dispatch", {})
        pending = [r for r in self.cfg["reviewers"] if not done.get(f"review-{r['name']}")]
        if pending:
            if ig.get("phase") == "pending":
                self.log(f"티켓 통합 리뷰 시작 — 검증 목표 {len(self.goals())}개, {self.st.get('base_sha', '')[:10]}..HEAD")
            ig["phase"] = "reviewing"
            ig.setdefault("notes", []).extend(self.gate.wait(sorted({r.get("pool", r["agent"]) for r in pending}),
                                                             "통합 리뷰"))
            for r in pending:
                if not disp.get(f"review-{r['name']}"):
                    self.launch(ig, f"review-{r['name']}")
            for r in pending:
                self.wait_task(ig, f"review-{r['name']}", accept_file=briefs.integration_path(work, r["name"]))
            for d in disp.values():
                if d:
                    orca.worker_release(d)
        counts = {r["name"]: integration_count(briefs.integration_path(work, r["name"])) for r in self.cfg["reviewers"]}
        ig["findings"] = {k: v for k, v in counts.items() if v}
        ig["head"] = git(self.st["worktree"], "rev-parse", "HEAD")
        total = sum(counts.values())
        if not total:
            ig.update(phase="done", finished_at=now_iso())
            self.save()
            self.log("티켓 통합 리뷰 지적 0 — 완료 처리로")
            return False
        n = max(c["index"] for c in self.st["checkboxes"]) + 1 if self.st["checkboxes"] else 1
        title = self.cfg.get("integration_fix_title") or "fix: 티켓 통합 리뷰 지적 반영"
        self.st["checkboxes"].append({"index": n, "title": title, "phase": "pending", "kind": B.AGENT,
                                      "fix_source": "integration"})
        ig.update(phase="fix", fix_box=n)
        self.save()
        self.log(f"티켓 통합 리뷰 지적 {total}건 ({', '.join(f'{k} {v}' for k, v in ig['findings'].items())}) — "
                 f"수정 체크박스 #{n} 추가")
        self.store.alert(self.pkg, "integration", f"티켓 통합 리뷰 지적 {total}건 — 수정 체크박스 #{n} 로 처리",
                         findings=[briefs.integration_path(work, k) for k in ig["findings"]])
        try:
            self.jira.add_box(self.t.jira, title)
        except DriverError as e:
            self.log(f"Jira 체크리스트에 #{n} 추가 실패 — {e.message}")
        return True

    def remeasure(self):
        """After the integration fix commit: the completion tests and the type check again, measured only (no commit).
        A red run stops the ticket — it is not verified."""
        ig = self.st.get("integration") or {}
        if ig.get("phase") != "fix":
            return
        c = self.box(ig["fix_box"])
        if c.get("phase") != "done":
            return
        wt, rc = self.st["worktree"], self.cfg.repo(self.t.repo)
        out, ok = [], True
        if rc.get("test_cmd") and self.t.tcs:
            res = self.run_cmd(rc["test_cmd"], self.t.tcs, wt, c, "remeasure")
            out.append(res[1])
            ok &= res[0]
        if rc.get("typecheck_cmd"):
            res = self.run_cmd(rc["typecheck_cmd"], [], wt, c, "remeasure-typecheck")
            out.append(res[1])
            ok &= res[0]
        if not ok:
            raise DriverError("검증 실패", "통합 리뷰 수정 뒤 재측정 — " + " / ".join(out))
        ig.update(phase="done", remeasured=" / ".join(out) or "재측정 명령 없음", finished_at=now_iso())
        self.save()
        self.log(f"통합 리뷰 수정 뒤 재측정 — {ig['remeasured']}")

    # ---- cumulative check: after every commit, GPT checks base..commit beside the loop
    def cumulative_agent(self):
        cc = self.cfg.get("cumulative_check") or {}
        if not cc.get("enabled", True):
            return None
        if cc.get("agent"):
            return cc
        rs = self.cfg["reviewers"]
        return next((r for r in rs if r["name"] == cc.get("reviewer", "codex")), None) or \
            next((r for r in rs if r["agent"] == "codex"), None)

    def start_cumulative(self, c, rec=None):
        """Start the cumulative check of checkbox c's commit; a usage gate over the threshold leaves it 「gated」 for a
        later boundary instead of holding the loop. Its failures are reported, never raised."""
        agent = self.cumulative_agent()
        commit = c.get("commit")
        if not agent or not commit:
            return
        recs = self.st.setdefault("cumulative", {})
        rec = rec or recs.get(str(c["index"]))
        if rec and rec.get("phase") not in ("gated",):
            return
        wt = self.st["worktree"]
        if not rec and run(["git", "-C", wt, "diff", "--quiet", f"{commit}^", commit]).returncode == 0:
            return  # an empty commit (verify) changes nothing to check
        rec = rec or {"index": c["index"], "title": c["title"], "commit": commit, "tasks": {}, "dispatch": {}}
        recs[str(c["index"])] = rec
        blocked, _, note = self.gate.blocked(agent.get("pool", agent["agent"]))
        if blocked:
            rec["phase"] = "gated"
            self.save()
            self.log(f"#{c['index']} 누적 검증 보류 — {note}")
            return
        work = self.work("cumulative")
        try:
            path = os.path.join(work, f"{c['index']:02d}-brief.md")
            write_text(path, briefs.cumulative(self.cfg, self.design, self.t, wt, work, c["index"], c["title"],
                                               self.st.get("base_sha") or f"{commit}^", commit, self.goals()))
            rid = self.st["run_id"]
            if not rec["tasks"].get("cumulative"):
                rec["tasks"]["cumulative"] = orca.task_create(rid, f"{self.pkg}#{c['index']} 누적검증 {agent['name'] if 'name' in agent else agent['agent']}", path, [])
            w = self.start_worker(rec, "cumulative", agent, f"{self.pkg}#{c['index']} 누적검증")
            rec.update(phase="running", started_at=now_iso())
            rec["dispatch"]["cumulative"] = w["dispatch"]
            self.save()
            self.log(f"#{c['index']} 누적 검증 시작 ({commit[:10]})")
        except DriverError as e:
            rec.update(phase="error", error=e.message[:300])
            self.save()
            self.log(f"#{c['index']} 누적 검증을 띄우지 못함 — {e.message[:200]}")
            self.store.alert(self.pkg, "cumulative", f"#{c['index']} 누적 검증을 띄우지 못함 — {e.message}",
                             commit=commit)

    def collect_cumulative(self, wait=False):
        """Read finished cumulative checks and alert the coordinator on problems; gated ones are started again.
        wait=True (before verified) waits until none is left running."""
        recs = self.st.get("cumulative") or {}
        started = now_ts()
        while True:
            live = False
            for rec in recs.values():
                if rec.get("phase") == "gated":
                    c = next((x for x in self.st["checkboxes"] if x["index"] == rec["index"]), rec)
                    if wait:
                        agent = self.cumulative_agent()
                        if agent:
                            self.gate.wait([agent.get("pool", agent["agent"])], f"#{rec['index']} 누적 검증")
                    self.start_cumulative(c, rec)
                if rec.get("phase") == "running":
                    self.read_cumulative(rec)
                live |= rec.get("phase") in ("running", "gated")
            if not wait or not live:
                return
            if now_ts() - started > int(self.cfg["quiet_limit_sec"]) and not self._has_open_question():
                for rec in recs.values():  # reported and given up on: they never hold verified
                    if rec.get("phase") in ("running", "gated"):
                        rec.update(phase="failed", finished_at=now_iso())
                        self.store.alert(self.pkg, "cumulative", f"#{rec['index']} 누적 검증이 끝나지 않아 기다림을 멈춤",
                                         commit=rec.get("commit"))
                self.save()
                return
            self.pump(self.st["run_id"])

    def read_cumulative(self, rec):
        rid, tid = self.st["run_id"], rec["tasks"].get("cumulative")
        status = orca.task_list(rid).get(tid, {}).get("status")
        gone = status == "failed" or self.worker_gone(rec, "cumulative")
        if status != "completed" and not gone:
            return
        n = rec["index"]
        path = briefs.cumulative_path(self.work("cumulative"), n)
        problems, first = cumulative_count(path)
        if problems is None:
            rec.update(phase="failed", finished_at=now_iso())
            self.log(f"#{n} 누적 검증이 결과 파일 없이 끝났다 ({path})")
            self.store.alert(self.pkg, "cumulative", f"#{n} 누적 검증이 결과를 남기지 않았다 — 다시 볼지 판단 필요",
                             commit=rec["commit"], path=path)
        else:
            rec.update(phase="done", problems=problems, path=path, finished_at=now_iso())
            if problems:
                self.log(f"#{n} 누적 검증 문제 {problems}건 — 코디네이터에 알림 ({path})")
                self.store.alert(self.pkg, "cumulative", f"#{n} `{rec['title'][:60]}` 까지의 누적 통합 검증에서 문제 "
                                 f"{problems}건{(' — ' + first) if first else ''}", commit=rec["commit"], path=path,
                                 problems=problems)
                self.notify(f"{self.pkg}#{n} 누적 검증 문제 {problems}건")
            else:
                self.log(f"#{n} 누적 검증 문제 없음")
        d = rec["dispatch"].get("cumulative")
        if d:
            orca.worker_release(d)
        self.progress += 1
        self.save()

    # ---- finish
    def finish_ticket(self):
        wt = self.st["worktree"]
        done = [c for c in self.st["checkboxes"] if c.get("phase") == "done"]
        deferred = [c for c in self.st["checkboxes"] if c.get("phase") == "deferred"]
        if deferred:
            self.st["status"] = "box_deferred"
            self.save()
            self.log(f"미룬 체크박스 {len(deferred)}개만 남음")
            return
        self.boundary_rebase()
        prev = self.t.chain_prev
        if prev and self.store.ticket(prev).get("status") not in DONE_STATUSES:
            self.st.update(status=REBASE_PENDING, waiting_on=prev, finished_at=now_iso())
            self.save()
            self.log(f"체크박스 완료 — 체인 앞 {prev} 가 verified 가 아니라 rebase_pending (verified 되면 스케줄러가 다시 띄워 "
                     "그 헤드로 리베이스·재검증한 뒤 verified)")
            return
        self.st.pop("waiting_on", None)
        base = self.st.get("base_sha")
        n = int(git(wt, "rev-list", "--count", f"{base}..HEAD")) if base else len(done)
        if n != len(done):
            self.log(f"커밋 수 {n} ≠ 체크박스 수 {len(done)} — 확인 필요 (브랜치 위 다른 티켓 커밋?)")
        self.st.update(status=VERIFIED, verified_head=git(wt, "rev-parse", "HEAD"), verified_at=now_iso())
        self.save()
        try:
            self.jira.transition(self.t.jira, (self.cfg["jira"] or {}).get("done_status"))
        except DriverError as e:
            self.log(f"Jira 완료 전이 실패 — {e.message}")
        self.log(f"verified {self.st['verified_head'][:10]} — 체크박스 {len(done)}개")
        self.notify(f"{self.pkg} verified")


def verdict_of(path):
    """「판정: 일치 | 불일치 | 판단 불가」 of a scene or contract result's first line, or None."""
    m = re.match(r"판정\s*[:：]\s*(일치|불일치|판단\s*불가)", _first_line(path) or "")
    return re.sub(r"\s+", " ", m.group(1)) if m else None


def integration_count(path):
    """Number of findings from an integration findings file's first line (「판정: 지적 없음 | N」, or a bare 「없음」)."""
    first = _first_line(path)
    if first is None:
        raise DriverError("정체", f"통합 리뷰 findings 가 없다 ({path})")
    if first in ("없음", "판정: 없음"):
        return 0
    m = re.match(r"판정\s*[:：]\s*지적\s*(없음|\d+)", first)
    if not m:
        raise DriverError("정체", f"통합 리뷰 findings 첫 줄이 「판정: 지적 없음|N」 이 아니다 ({path}): {first[:80]}")
    return 0 if m.group(1) == "없음" else int(m.group(1))


def cumulative_count(path):
    """(problems, first problem heading) of a cumulative check result; (None, '') when it has no readable first line."""
    first = _first_line(path)
    m = re.match(r"판정\s*[:：]\s*문제\s*(없음|\d+)", first or "")
    if not m:
        return None, ""
    n = 0 if m.group(1) == "없음" else int(m.group(1))
    with open(path, encoding="utf-8") as f:
        head = next((l.lstrip("#").strip() for l in f if l.startswith("###")), "")
    return n, head[:120]


def _first_line(path):
    try:
        with open(path, encoding="utf-8") as f:
            return next((l.strip() for l in f if l.strip()), "")
    except OSError:
        return None


def _norm(s):
    return re.sub(r"[\s`]+", "", s or "")


def quote_as(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
