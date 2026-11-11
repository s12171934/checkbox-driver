"""The ticket driver: `cbd <project> run <PKG>`, one process per ticket in its own Orca terminal.

start_ticket -> ensure_run -> precreate_tasks -> process_checkbox x N -> finish_ticket

Every step is resumable: the checkbox record keeps its phase, task ids, dispatches and finished roles, so a restarted
driver continues where the last one stopped (a worker_done received before a restart is read back from the task
status, not from the mailbox).
"""

import os
import re
import shlex

from cbd import boxes as B
from cbd import briefs, orca
from cbd import plan as P
from cbd.design import expand_tcs
from cbd.gates import Gate
from cbd.jira import Jira
from cbd.select import base_for
from cbd.state import BLOCKED, BOX_WAITING, DONE_STATUSES, RUNNING, VERIFIED, TicketLock
from cbd.util import DriverError, git, now_iso, now_ts, run, write_text


class BoxWait(Exception):
    def __init__(self, packages, why):
        super().__init__(why)
        self.packages = packages
        self.why = why


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

    # ---- bookkeeping
    def log(self, text):
        self.store.event(self.pkg, text)
        print(f"{now_iso()} {self.pkg} {text}", flush=True)

    def save(self):
        self.store.save(self.st)

    def box(self, n):
        return next(c for c in self.st["checkboxes"] if c["index"] == n)

    def work(self, n):
        d = os.path.join(self.cfg.work_dir, self.pkg, f"{n:02d}")
        os.makedirs(d, exist_ok=True)
        return d

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
            self.start_ticket()
            self.ensure_run()
            self.precreate_tasks()
            for c in self.st["checkboxes"]:
                if c.get("phase") in ("done", "deferred"):
                    continue
                self.process_checkbox(c)
            self.finish_ticket()
            return 0
        except BoxWait as w:
            self.st.update(status=BOX_WAITING, waiting_on=",".join(w.packages))
            self.save()
            self.log(f"체크박스 선행 대기 — {w.why}")
            return 0
        except DriverError as e:
            self.block(e)
            return 2
        finally:
            lock.release()
            try:
                P.write(self.cfg, self.design, self.store, orca.live_handles())
            except Exception as e:  # the plan is a view; never hide the real outcome behind it
                print(f"plan.json 갱신 실패: {e}")

    def block(self, e):
        self.st.setdefault("exceptions", []).append({"kind": e.kind, "message": e.message, "at": now_iso()})
        self.st["status"] = BLOCKED
        self.save()
        self.log(f"멈춤 [{e.kind}] {e.message}")
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
        path = orca.worktree_for_branch(t.branch)
        if path:
            self.log(f"워크트리 재사용 {path}")
        else:
            ref = base.get("sha") or base["ref"]
            path = orca.worktree_create(repo.get("orca_repo") or t.repo, t.branch, ref)
            self.log(f"워크트리 생성 {path} (베이스 {base['ref']}{' @' + base['sha'][:10] if base.get('sha') else ''})")
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

    def ensure_run(self):
        rid = self.st.get("run_id")
        if rid:
            try:
                orca.run_use(rid)
                return
            except orca.OrcaError as e:
                self.log(f"Run {rid} 을 이어 쓸 수 없다 ({e.code or e.message[:60]}) — 새 Run 으로 다시 등록")
                for c in self.st["checkboxes"]:
                    if c.get("phase") != "done":
                        c.pop("tasks", None)
                        c.pop("dispatch", None)
                        c.pop("done", None)
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
        for c in self.st["checkboxes"]:  # a task the Run no longer has is created again (unless that role finished)
            for k in [k for k, tid in (c.get("tasks") or {}).items() if tid not in known and not (c.get("done") or {}).get(k)]:
                c["tasks"].pop(k)
                (c.get("dispatch") or {}).pop(k, None)
        for c in self.st["checkboxes"]:
            for k, tid in (c.get("tasks") or {}).items():
                key_to_task[(c["index"], k)] = tid
        rows = B.ticket_sessions(self.cfg, self.t, self._boxes())
        by_key = {r["key"]: r for r in rows}
        created = 0
        for r in rows:
            c = self.box(r["box"])
            if r["kind"] == "driver" or c.get("phase") in ("done", "deferred") or (c.get("tasks") or {}).get(r["kind"]):
                continue
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
            text = briefs.impl(self.cfg, self.design, self.t, box, wt, work, rows)
        elif kind == "judge":
            text = briefs.judge(self.cfg, self.design, self.t, box, wt, work)
        else:
            text = briefs.review(self.cfg, self.design, self.t, box, wt, work, kind.removeprefix("review-"))
        path = os.path.join(work, f"{kind}-brief.md")
        write_text(path, text)
        return path

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
        if c["kind"] == B.DRIVER_VERIFY:
            self.verify_box(c, states)
        elif c["kind"] == B.DRIVER_PIN:
            self.pin_box(c, states)
        else:
            self.agent_box(c)
        c["phase"] = "done"
        c["finished_at"] = now_iso()
        self.save()
        try:
            self.jira.check_box(self.t.jira, c["title"])
        except DriverError as e:
            self.log(f"#{c['index']} Jira 체크 실패 — {e.message}")
        self.log(f"#{c['index']} 완료 {c.get('commit', '')[:10]}")
        P.write(self.cfg, self.design, self.store, orca.live_handles())

    def subject(self, c):
        return f"[{self.t.jira}] {c['title']}" if self.t.jira else c["title"]

    def verify_box(self, c, states):
        prev = self.t.chain_prev
        if prev and states.get(prev, {}).get("status") not in DONE_STATUSES:
            raise BoxWait([prev], f"첫 verify 는 체인 앞 {prev} verified 뒤 (verify 게이트)")
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
        git(sp, "fetch", "--quiet", "origin", timeout=600)
        git(sp, "checkout", "--quiet", head)
        git(wt, "add", sub)
        r = run(["git", "-C", wt, "commit", "-m", self.subject(c), "-m", f"{sub} → {producer} verified head {head}"])
        if r.returncode != 0:
            raise DriverError("환경", f"핀 커밋 실패 (훅?): {(r.stderr or r.stdout).strip()[-300:]}")
        c["commit"] = git(wt, "rev-parse", "HEAD")

    # ---- agent checkbox: implement -> review x N -> judge
    def agent_box(self, c):
        wt, rid = self.st["worktree"], self.st["run_id"]
        work = self.work(c["index"])
        p = briefs.paths(work)
        done, disp = c.setdefault("done", {}), c.setdefault("dispatch", {})
        impl = self.cfg["impl"]
        if not done.get("impl"):
            if not disp.get("impl"):
                c["notes"] = self.gate.wait([impl.get("pool", impl["agent"])], f"#{c['index']} 착수")
                c["head_before"] = git(wt, "rev-parse", "HEAD")
                w = orca.worker_start(rid, c["tasks"]["impl"], wt, impl["agent"], impl["model"], impl.get("effort"),
                                      f"{self.pkg}#{c['index']} 구현", keep_open=True)
                disp["impl"], c["impl_handle"] = w["dispatch"], w["handle"]
                c["dispatched_at"] = now_iso()
                self.save()
            self.wait_task(c, "impl")
        if run(["git", "-C", wt, "diff", "--cached", "--quiet"]).returncode == 0:
            raise DriverError("정체", f"{c['index']}번 구현이 스테이지를 남기지 않았다")
        if not os.path.exists(p["draft"]):
            raise DriverError("정체", f"{c['index']}번 구현이 커밋 메시지 초안을 남기지 않았다 ({p['draft']})")
        reviewers = self.cfg["reviewers"]
        pending = [r for r in reviewers if not done.get(f"review-{r['name']}")]
        if pending:
            c.setdefault("notes", []).extend(self.gate.wait(sorted({r.get("pool", r["agent"]) for r in pending}),
                                                            f"#{c['index']} 리뷰"))
            for r in pending:
                k = f"review-{r['name']}"
                if not disp.get(k):
                    w = orca.worker_start(rid, c["tasks"][k], wt, r["agent"], r["model"], r.get("effort"),
                                          f"{self.pkg}#{c['index']} 리뷰 {r['name']}")
                    disp[k] = w["dispatch"]
                    self.save()
            for r in pending:
                self.wait_task(c, f"review-{r['name']}", accept_file=briefs.findings_path(work, r["name"]))
        if not done.get("judge"):
            if not disp.get("judge"):
                if c.get("impl_handle") in orca.live_handles():
                    disp["judge"] = orca.dispatch(rid, c["tasks"]["judge"], c["impl_handle"])
                else:
                    self.log(f"#{c['index']} 구현 터미널이 닫혀 판정을 새 세션으로 띄운다")
                    disp["judge"] = orca.worker_start(rid, c["tasks"]["judge"], wt, impl["agent"], impl["model"],
                                                      impl.get("effort"), f"{self.pkg}#{c['index']} 판정")["dispatch"]
                c["dispatched_at"] = now_iso()
                self.save()
            self.wait_task(c, "judge")
        self.check_commit(c)
        for k, d in disp.items():
            if k != "judge" and d:
                orca.worker_release(d)
        if c.get("impl_handle"):  # kept open for the judge; the checkbox is over, so is its implementer (루프 10 종료)
            orca.terminal_close(c["impl_handle"])

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
        signal received before a restart is not lost. Questions are saved to files and answered from files."""
        rid, tid = self.st["run_id"], c["tasks"][kind]
        started = now_ts()
        while True:
            task = orca.task_list(rid).get(tid, {})
            status = task.get("status")
            if status == "completed":
                c.setdefault("done", {})[kind] = now_iso()
                self.save()
                return
            if status == "failed":
                if accept_file and os.path.exists(accept_file):
                    self.log(f"#{c['index']} {kind} 가 실패로 끝났지만 결과 파일이 있어 수용 ({accept_file})")
                    c.setdefault("done", {})[kind] = now_iso()
                    self.save()
                    return
                kind_name = "리뷰어 불가" if kind.startswith("review") else "정체"
                raise DriverError(kind_name, f"#{c['index']} {kind} 태스크 실패: {str(task.get('result', ''))[:200]}")
            if now_ts() - started > int(self.cfg["quiet_limit_sec"]) and not self._has_open_question():
                raise DriverError("정체", f"#{c['index']} {kind} 가 {int(self.cfg['quiet_limit_sec']) // 60}분 동안 끝나지 않았다")
            self.pump(rid)

    def _has_open_question(self):
        return any(not q.get("answered") for q in self.st.get("questions") or [])

    def pump(self, rid):
        """One mailbox wait: store questions, send answers that appeared as files, close tasks finished outside."""
        for m in orca.check_wait(rid, int(self.cfg["mail_wait_ms"])):
            if m.get("type") in ("question", "escalation"):
                d = self.store.reply_dir(rid)
                path = os.path.join(d, f"{m['id']}.question.md")
                write_text(path, f"# {m.get('subject', '')}\n\n- 보낸 이: {m.get('from')}\n- 태스크: "
                                 f"{(m.get('payload') or {}).get('taskId', '')}\n\n{m.get('body', '')}\n")
                self.st.setdefault("questions", []).append({"msg_id": m["id"], "path": path, "at": now_iso()})
                self.save()
                self.log(f"질문 대기 — {path}")
                self.notify(f"{self.pkg} 워커 질문: {m.get('subject', '')[:60]}")
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


def _norm(s):
    return re.sub(r"[\s`]+", "", s or "")


def quote_as(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
