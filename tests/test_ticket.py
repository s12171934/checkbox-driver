"""The ticket driver end to end against a fake Orca and a real git repository."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cbd import orca  # noqa: E402
from cbd.config import Config  # noqa: E402
from cbd.design import Design  # noqa: E402
from cbd.state import Store  # noqa: E402
from cbd.ticket import TicketDriver  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "design")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class FakeOrca:
    """Workers act instantly: implement = stage a file + draft, review = findings file, judge = commit."""

    def __init__(self, repo, behaviour=None):
        self.repo = repo
        self.tasks = {}
        self.titles = {}
        self.n = 0
        self.behaviour = behaviour or {}
        self.started = []
        self.starts = []    # (task key, terminal, retry_of) per worker-start
        self.workers = []   # worker-list rows
        self.updates = []

    def run_create(self, objective):
        return "run_1"

    def run_use(self, rid):
        pass

    def task_create(self, rid, title, spec, deps):
        missing = [d for d in deps or [] if d not in self.tasks]
        if missing:  # Orca refuses a dependency from another Run
            raise orca.OrcaError("runtime_error", f"Dependency task {missing[0]} must belong to run {rid}")
        self.n += 1
        tid = f"task_{self.n}"
        self.tasks[tid] = {"id": tid, "status": "pending"}
        self.titles[tid] = (title, spec)
        return tid

    def task_list(self, rid):
        return self.tasks

    def _work(self, spec):
        return os.path.dirname(spec)

    def worker_start(self, rid, tid, wt, agent, model, effort, title, keep_open=False, retry_of=None, terminal=None):
        key, spec = self.titles[tid]
        if terminal and self.behaviour.get("reuse") == "refused":
            raise orca.OrcaError("invalid_argument", f"Terminal {terminal} is still held by finished Dispatch ctx_x.")
        self.started.append(key)
        self.starts.append((key, terminal, retry_of))
        d = f"d_{tid}" + (f"_r{len(self.starts)}" if retry_of else "")
        self.workers.insert(0, {"dispatchId": d, "taskId": tid, "state": "succeeded", "liveness": "exited",
                                "terminal": terminal or f"h_{tid}"})
        self.tasks[tid]["status"] = "dispatched"
        work = self._work(spec)
        if "구현" in key:
            n = key.split("#")[1].split()[0]
            if int(n) not in self.behaviour.get("no_stage", ()):
                with open(os.path.join(wt, f"f{n}.txt"), "w") as f:
                    f.write(n)
                sh(wt, "git", "add", f"f{n}.txt")
            if int(n) not in self.behaviour.get("no_test_result", ()):
                with open(os.path.join(work, "test-result.md"), "w") as f:
                    f.write("통과\n")
            title = _read(spec).split("첫 줄은 정확히 `")[1].split("`")[0]
            with open(os.path.join(work, "commit-message.md"), "w") as f:
                f.write(title + "\n\n코드 변경 없음\n")
            self.tasks[tid]["status"] = "completed"
        elif "리뷰" in key:
            name = key.split()[-1]
            if self.behaviour.get("review") != "fail-no-file":
                with open(os.path.join(work, f"findings-{name}.md"), "w") as f:
                    f.write("없음\n")
            self.tasks[tid]["status"] = "failed" if self.behaviour.get("review", "").startswith("fail") else "completed"
        else:
            self._judge(tid, wt, work)
        return {"dispatch": d, "handle": terminal or f"h_{tid}"}

    def worker_list(self, rid):
        return self.workers

    def task_update(self, rid, tid, status, result=""):
        self.updates.append((tid, status))
        self.tasks[tid]["status"] = status

    def _judge(self, tid, wt, work):
        title = _read(os.path.join(work, "commit-message.md")).strip()
        sh(wt, "git", "commit", "-q", "-m", title)
        self.tasks[tid]["status"] = "completed"

    def live_handles(self):
        return frozenset(f"h_{t}" for t in self.tasks)

    def check_wait(self, rid, ms):
        return []

    def worker_release(self, d):
        pass

    def terminal_close(self, h):
        self.closed = getattr(self, "closed", []) + [h]

    def repo_info(self, name):
        return {"id": "r1", "path": self.repo}

    def worktrees(self):
        return getattr(self, "records", [])

    def find_worktree(self, b):
        return next((w for w in self.worktrees() if w.get("branch") == b), None)

    def worktree_create(self, repo, name, branch, base, parent=None, no_parent=False):
        self.created = getattr(self, "created", []) + [(name, branch, base, parent, no_parent)]
        if branch is None:  # a project worktree: only its Orca record matters here
            path = os.path.join(os.path.dirname(self.repo), name)
            self.records = self.worktrees() + [{"id": f"r1::{path}", "repoId": "r1", "displayName": name,
                                                "path": path, "branch": f"dante/{name}", "parentWorktreeId": None}]
            return path
        self.created_from = base
        sh(self.repo, "git", "checkout", "-q", "-b", branch, base)
        self.records = self.worktrees() + [{"id": f"r1::{self.repo}", "repoId": "r1", "displayName": name,
                                            "path": self.repo, "branch": branch, "parentWorktreeId": parent}]
        return self.repo


class TicketTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.repo = os.path.join(self.home, "repo")
        os.makedirs(self.repo)
        sh(self.repo, "git", "init", "-q", "-b", "main")
        sh(self.repo, "git", "config", "user.email", "t@example.com")
        sh(self.repo, "git", "config", "user.name", "t")
        sh(self.repo, "git", "commit", "-q", "--allow-empty", "-m", "base")
        self.origin = os.path.join(self.home, "origin.git")
        sh(self.home, "git", "init", "-q", "--bare", "-b", "main", self.origin)
        sh(self.repo, "git", "remote", "add", "origin", self.origin)
        sh(self.repo, "git", "push", "-q", "origin", "main")
        os.makedirs(os.path.join(self.home, "ex"))
        with open(os.path.join(self.home, "ex", "config.json"), "w") as f:
            json.dump({"project": "EX", "design_root": FIXTURE, "notify": False, "usage": {"commands": {}},
                       "repos": {"core": {"path": self.repo, "test_cmd": ["true", "{tcs}"]}}}, f)
        self.cfg = Config("ex", self.home)
        self.design = Design(FIXTURE)
        self.store = Store(self.cfg)

    def tearDown(self):
        shutil.rmtree(self.home)

    def drive(self, fake, pkg="A1"):
        names = ["run_create", "run_use", "task_create", "task_list", "worker_start", "worker_list", "task_update",
                 "live_handles", "check_wait", "worker_release", "find_worktree", "worktree_create", "terminal_close",
                 "repo_info", "worktrees"]
        self.sleeps = []
        with mock.patch.multiple(orca, **{n: getattr(fake, n) for n in names}), \
                mock.patch("cbd.plan.git", return_value="deadbeef"), \
                mock.patch("cbd.ticket.time.sleep", side_effect=self.sleeps.append):
            return TicketDriver(self.cfg, self.design, self.store, pkg).run()

    def alerts(self):
        try:
            return [json.loads(l) for l in _read(self.store.p("alerts.jsonl")).splitlines()]
        except FileNotFoundError:
            return []

    def test_whole_ticket(self):
        fake = FakeOrca(self.repo)
        self.assertEqual(self.drive(fake), 0)
        st = self.store.ticket("A1")
        self.assertEqual(st["status"], "verified")
        log = sh(self.repo, "git", "log", "--format=%s", "main..HEAD").splitlines()
        self.assertEqual(log, ["[EX-1] verify: A1 테스트 그린", "[EX-1] feat(core): 모델 추가",
                               "[EX-1] test(core): TC-A-01~TC-A-03 빨강"])
        self.assertEqual(st["verified_head"], sh(self.repo, "git", "rev-parse", "HEAD"))
        # 2 agent boxes x (impl + 2 reviews + judge) precreated, driver verify has no task
        self.assertEqual(len(fake.tasks), 8)
        self.assertEqual(fake.started[:3], ["A1#1 구현 claude", "A1#1 리뷰 codex", "A1#1 리뷰 claude"])
        # the judge is started on the implementing terminal with worker-start --terminal, not dispatch --inject
        impl_task = next(t for t, (k, _) in fake.titles.items() if k == "A1#1 구현 claude")
        self.assertIn(("A1#1 판정 claude", f"h_{impl_task}", None), fake.starts)
        self.assertEqual(len(fake.closed), 2)  # each agent checkbox's implementing terminal is closed after its commit
        # the test commit's TC rows were handed to the implementer
        work = os.path.join(self.cfg.work_dir, "A1", "01")
        self.assertIn("TC-A-02", _read(os.path.join(work, "tc-rows.md")))
        plan = json.loads(_read(self.store.p("plan.json")))
        a1 = next(t for t in plan["tickets"] if t["package"] == "A1")
        self.assertEqual((a1["boxes_done"], a1["status"]), (3, "verified"))
        self.assertTrue(all(s["status"] == "done" for s in a1["sessions"]))

    def advance_origin(self):
        """origin/main gains a commit the local main does not have (a checkout nobody pulled)."""
        other = os.path.join(self.home, "other")
        sh(self.home, "git", "clone", "-q", self.origin, other)
        sh(other, "git", "-c", "user.email=o@example.com", "-c", "user.name=o", "commit", "-q", "--allow-empty",
           "-m", "upstream")
        sh(other, "git", "push", "-q", "origin", "main")
        return sh(other, "git", "rev-parse", "HEAD")

    def test_repo_base_starts_from_fetched_origin_not_stale_local_branch(self):
        upstream = self.advance_origin()
        fake = FakeOrca(self.repo)
        self.assertEqual(self.drive(fake), 0)
        st = self.store.ticket("A1")
        self.assertEqual(fake.created_from, upstream)
        self.assertEqual(st["base"], {"ref": "main", "sha": upstream, "from": None})
        self.assertEqual(st["base_sha"], upstream)
        log = sh(self.repo, "git", "log", "--format=%s", f"{upstream}..HEAD").splitlines()
        self.assertEqual(len(log), 3)

    def test_reused_worktree_without_the_base_blocks(self):
        self.advance_origin()
        sh(self.repo, "git", "checkout", "-q", "-b", "EX-1-A1-model")  # left over from a stale start
        fake = FakeOrca(self.repo)
        fake.find_worktree = lambda b: {"path": self.repo}
        self.assertEqual(self.drive(fake), 2)
        exc = self.store.ticket("A1")["exceptions"][-1]
        self.assertEqual(exc["kind"], "환경")
        self.assertIn("포함하지 않는다", exc["message"])

    def with_project_worktree(self):
        conf = json.loads(_read(self.cfg.path))
        with open(self.cfg.path, "w") as f:
            json.dump({**conf, "project_worktree": "EX-9"}, f)
        self.cfg = Config("ex", self.home)

    def test_ticket_worktree_is_created_under_a_new_project_worktree(self):
        self.with_project_worktree()
        upstream = self.advance_origin()
        fake = FakeOrca(self.repo)
        self.assertEqual(self.drive(fake), 0)
        project = os.path.join(self.home, "EX-9")
        self.assertEqual(fake.created, [("EX-9", None, "refs/remotes/origin/main", None, True),
                                        ("EX-1", "EX-1-A1-model", upstream, f"r1::{project}", False)])

    def test_existing_project_worktree_is_reused(self):
        self.with_project_worktree()
        fake = FakeOrca(self.repo)
        fake.records = [{"id": "r1::/p/EX-9", "repoId": "r1", "displayName": "EX-9", "path": "/p/EX-9",
                         "branch": "dante/EX-9", "parentWorktreeId": None}]
        self.assertEqual(self.drive(fake), 0)
        self.assertEqual([c[0] for c in fake.created], ["EX-1"])
        self.assertEqual(fake.created[0][3], "r1::/p/EX-9")

    def test_reused_ticket_worktree_outside_the_project_blocks(self):
        self.with_project_worktree()
        sh(self.repo, "git", "checkout", "-q", "-b", "EX-1-A1-model")
        fake = FakeOrca(self.repo)
        fake.records = [{"id": "r1::/p/EX-9", "repoId": "r1", "displayName": "EX-9", "path": "/p/EX-9",
                         "branch": "dante/EX-9", "parentWorktreeId": None},
                        {"id": f"r1::{self.repo}", "repoId": "r1", "displayName": "EX-1-A1-model", "path": self.repo,
                         "branch": "EX-1-A1-model", "parentWorktreeId": "r1::/elsewhere"}]
        self.assertEqual(self.drive(fake), 2)
        self.assertIn("프로젝트 워크트리 EX-9 아래가 아니다", self.store.ticket("A1")["exceptions"][-1]["message"])

    def test_new_run_does_not_depend_on_done_checkbox_tasks_of_the_old_run(self):
        head = sh(self.repo, "git", "rev-parse", "HEAD")
        sh(self.repo, "git", "checkout", "-q", "-b", "EX-1-A1-model")
        sh(self.repo, "git", "commit", "-q", "--allow-empty", "-m", "[EX-1] test(core): TC-A-01~TC-A-03 빨강")
        a1 = self.design.tickets["A1"]
        done = {"impl": "x", "review-codex": "x", "review-claude": "x", "judge": "x"}
        boxes = [{"index": b.index, "title": b.title, "phase": "pending", "kind": "agent"} for b in a1.boxes]
        boxes[0].update(phase="done", tasks={k: f"old_{k}" for k in done}, done=done)  # finished in a Run now gone
        boxes[2]["kind"] = "driver-verify"
        self.store.save({"package": "A1", "status": "planned", "worktree": self.repo, "branch": "EX-1-A1-model",
                         "base": {"ref": "main", "sha": head, "from": None}, "base_sha": head, "checkboxes": boxes})
        fake = FakeOrca(self.repo)
        self.assertEqual(self.drive(fake), 0, self.store.ticket("A1").get("exceptions"))
        self.assertEqual(fake.started[0], "A1#2 구현 claude")
        self.assertEqual(self.store.ticket("A1")["status"], "verified")

    def test_question_from_full_check_record_is_saved(self):
        """`orca orchestration check --full --json` returns raw records: from_handle, payload as a JSON string."""
        record = {"id": "msg_q1", "run_id": "run_1", "delivery_contract": "current_delivery",
                  "from_handle": "dispatch:ctx_w1", "to_handle": "run:run_1", "subject": "Question",
                  "body": "service-account.json 이 없다 — 어떻게 할까?", "type": "question", "priority": "normal",
                  "payload": json.dumps({"taskId": "task_7", "dispatchId": "ctx_w1", "question": "…"}), "read": 1}
        status = dict(record, id="msg_s1", type="status", subject="진행", body="진행 중", payload=None)
        self.store.save({"package": "A1", "status": "running", "run_id": "run_1", "checkboxes": []})
        driver = TicketDriver(self.cfg, self.design, self.store, "A1")
        with mock.patch("cbd.orca.call", return_value={"messages": [status, record], "deliveryId": "dlv_1"}):
            driver.pump("run_1")
        qs = self.store.ticket("A1")["questions"]
        self.assertEqual([q["msg_id"] for q in qs], ["msg_q1"])
        text = _read(qs[0]["path"])
        self.assertIn("ctx_w1", text)
        self.assertIn("task_7", text)
        self.assertIn("service-account.json 이 없다", text)

    def test_unexpected_error_blocks_the_ticket_instead_of_leaving_it_running(self):
        fake = FakeOrca(self.repo)
        fake.check_wait = mock.Mock(side_effect=AttributeError("boom"))
        orig = fake.worker_start

        def no_finish(rid, tid, wt, *a, **k):
            r = orig(rid, tid, wt, *a, **k)
            fake.tasks[tid]["status"] = "running"
            return r
        fake.worker_start = no_finish
        with self.assertRaises(AttributeError):
            self.drive(fake)
        st = self.store.ticket("A1")
        self.assertEqual(st["status"], "blocked")
        self.assertEqual(st["exceptions"][-1]["kind"], "드라이버 오류")

    def test_worker_terminal_handle_is_kept_for_the_judge(self):
        with mock.patch("cbd.orca.call", return_value={"dispatchId": "ctx_9", "state": "ready", "terminal": "term_9"}):
            w = orca.worker_start("run_1", "task_1", "/wt", "claude", "m", "high", "t")
        self.assertEqual(w, {"dispatch": "ctx_9", "handle": "term_9"})

    def test_worker_start_on_a_terminal_names_its_worktree_and_no_model(self):
        with mock.patch("cbd.orca.call", return_value={"dispatchId": "ctx_9", "terminal": "term_1"}) as call:
            orca.worker_start("run_1", "task_1", "/wt", "claude", "m", "high", "t", terminal="term_1", retry_of="ctx_0")
        args = list(call.call_args.args)
        self.assertEqual(args[:6], ["orchestration", "worker-start", "--run", "run_1", "--task", "task_1"])
        self.assertIn("--terminal", args)
        self.assertEqual(args[args.index("--worktree") + 1], "path:/wt")
        self.assertEqual(args[args.index("--retry-of") + 1], "ctx_0")
        for flag in ("--agent", "--model", "--effort", "--display-name"):
            self.assertNotIn(flag, args)

    def test_error_classes(self):
        T = orca.transient
        self.assertTrue(T(orca.DriverError("연결 불명", "orca worker-start 응답 없음 (300s)")))
        self.assertTrue(T(orca.OrcaError("runtime_error", "Dependency task x must belong to run y")))
        self.assertTrue(T(orca.OrcaError("invalid_argument", "the start is refused while the release runs")))
        self.assertFalse(T(orca.OrcaError("invalid_argument", "Terminal t is still held by finished Dispatch ctx")))
        self.assertFalse(T(orca.OrcaError("task_not_startable", "deps unfinished")))
        self.assertFalse(T(orca.DriverError("검증 실패", "verify 실패")))
        self.assertFalse(T(orca.DriverError("환경", "orca CLI 가 PATH 에 없다")))

    def test_failed_reviewer_with_findings_file_is_accepted(self):
        self.assertEqual(self.drive(FakeOrca(self.repo, {"review": "fail-with-file"})), 0)
        self.assertEqual(self.store.ticket("A1")["status"], "verified")

    def test_failed_reviewer_without_file_blocks(self):
        self.assertEqual(self.drive(FakeOrca(self.repo, {"review": "fail-no-file"})), 2)
        st = self.store.ticket("A1")
        self.assertEqual(st["status"], "blocked")
        self.assertEqual(st["exceptions"][-1]["kind"], "리뷰어 불가")

    def test_impl_without_stage_blocks(self):
        fake = FakeOrca(self.repo)
        orig = fake.worker_start

        def lazy(rid, tid, wt, *a, **k):
            if "구현" in fake.titles[tid][0]:
                fake.tasks[tid]["status"] = "completed"
                return {"dispatch": "d", "handle": "h"}
            return orig(rid, tid, wt, *a, **k)
        fake.worker_start = lazy
        self.assertEqual(self.drive(fake), 2)
        self.assertIn("스테이지", self.store.ticket("A1")["exceptions"][-1]["message"])

    def override(self, *keys):
        with open(os.path.join(self.home, "ex", "config.json")) as f:
            conf = json.load(f)
        conf["agent_box_overrides"] = list(keys)
        with open(os.path.join(self.home, "ex", "config.json"), "w") as f:
            json.dump(conf, f)
        self.cfg = Config("ex", self.home)
        self.store = Store(self.cfg)

    def test_agent_verify_without_stage_commits_empty_and_skips_review(self):
        self.override("A1#3")
        fake = FakeOrca(self.repo, {"no_stage": {3}})
        self.assertEqual(self.drive(fake), 0)
        st = self.store.ticket("A1")
        self.assertEqual(st["status"], "verified")
        self.assertEqual(sh(self.repo, "git", "log", "-1", "--format=%s"), "[EX-1] verify: A1 테스트 그린")
        self.assertEqual(sh(self.repo, "git", "log", "-1", "--format=%b"), "코드 변경 없음")
        self.assertEqual(sh(self.repo, "git", "diff", "--name-only", "HEAD~1", "HEAD"), "")  # an empty commit
        self.assertEqual([k for k in fake.started if k.startswith("A1#3")], ["A1#3 구현 claude"])
        box3 = next(c for c in st["checkboxes"] if c["index"] == 3)
        skipped = ["review-codex", "review-claude", "judge"]
        self.assertEqual(sorted(box3["skipped"]), sorted(skipped))
        self.assertEqual(sorted(t for t, s in fake.updates if s == "completed"),
                         sorted(box3["tasks"][k] for k in skipped))  # the precreated tasks are closed
        self.assertIn(f"h_{box3['tasks']['impl']}", fake.closed)
        plan = json.loads(_read(self.store.p("plan.json")))
        a1 = next(t for t in plan["tickets"] if t["package"] == "A1")
        self.assertEqual(sorted(s["kind"] for s in a1["sessions"] if s["status"] == "skipped"), sorted(skipped))
        self.assertTrue(any("건너뜀" in e["text"] for e in self.store.events()))

    def test_agent_verify_without_stage_needs_test_result(self):
        self.override("A1#3")
        fake = FakeOrca(self.repo, {"no_stage": {3}, "no_test_result": {3}})
        self.assertEqual(self.drive(fake), 2)
        e = self.store.ticket("A1")["exceptions"][-1]
        self.assertEqual(e["kind"], "정체")
        self.assertIn("test-result.md", e["message"])

    def test_agent_verify_with_stage_keeps_review_and_judge(self):
        self.override("A1#3")
        fake = FakeOrca(self.repo)
        self.assertEqual(self.drive(fake), 0)
        self.assertEqual([k for k in fake.started if k.startswith("A1#3")],
                         ["A1#3 구현 claude", "A1#3 리뷰 codex", "A1#3 리뷰 claude", "A1#3 판정 claude"])
        self.assertNotIn("skipped", next(c for c in self.store.ticket("A1")["checkboxes"] if c["index"] == 3))

    def test_verify_impl_brief_allows_no_stage(self):
        from cbd import briefs
        t = self.design.tickets["A1"]
        verify = briefs.impl(self.cfg, self.design, t, t.boxes[2], "/wt", "/work")
        feat = briefs.impl(self.cfg, self.design, t, t.boxes[1], "/wt", "/work")
        self.assertIn("스테이지 없이", verify)
        self.assertNotIn("스테이지 없이", feat)

    def test_restart_resumes_after_done_signal(self):
        fake = FakeOrca(self.repo)
        orig = fake.worker_start
        calls = {"n": 0}

        def crash_once(rid, tid, *a, **k):
            if "판정" in fake.titles[tid][0]:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise KeyboardInterrupt  # the driver dies while the judge is dispatched
            return orig(rid, tid, *a, **k)
        fake.worker_start = crash_once
        with self.assertRaises(KeyboardInterrupt):
            self.drive(fake)
        st = self.store.ticket("A1")
        c1 = st["checkboxes"][0]
        self.assertEqual(sorted(c1["done"]), ["impl", "review-claude", "review-codex"])
        started = len(fake.started)
        self.assertEqual(self.drive(fake), 0)
        # no implementer or reviewer of box 1 was started again
        self.assertNotIn("A1#1 구현 claude", fake.started[started:])
        self.assertEqual(self.store.ticket("A1")["status"], "verified")

    # ---- judge on the implementing terminal, and its fallback
    def test_judge_falls_back_to_a_new_session_when_terminal_reuse_is_refused(self):
        fake = FakeOrca(self.repo, {"reuse": "refused"})
        self.assertEqual(self.drive(fake), 0, self.store.ticket("A1").get("exceptions"))
        self.assertIn(("A1#1 판정 claude", None, None), fake.starts)
        log = _read(self.cfg.p("logs", "ticket-A1.log"))
        self.assertIn("재사용 실패 — 새 세션으로 판정", log)
        self.assertIn("still held by finished Dispatch", log)

    # ---- transient Orca errors: the same step again, blocked only after three retries
    def test_transient_error_retries_the_same_step(self):
        fake = FakeOrca(self.repo)
        orig, calls = fake.task_list, {"n": 0}

        def flaky(rid):
            calls["n"] += 1
            if calls["n"] in (3, 4):
                raise orca.DriverError("연결 불명", "orca orchestration task-list 응답 없음 (120s)")
            return orig(rid)
        fake.task_list = flaky
        self.assertEqual(self.drive(fake), 0, self.store.ticket("A1").get("exceptions"))
        self.assertEqual(self.sleeps, [60, 120])
        self.assertEqual(self.store.ticket("A1")["status"], "verified")
        self.assertEqual(fake.started.count("A1#1 구현 claude"), 1)

    def test_transient_error_blocks_retryable_after_three_retries_and_alerts(self):
        fake = FakeOrca(self.repo)
        fake.run_create = mock.Mock(side_effect=orca.OrcaError("runtime_unavailable", "Orca runtime is restarting"))
        self.assertEqual(self.drive(fake), 2)
        self.assertEqual(self.sleeps, [60, 120, 240])
        exc = self.store.ticket("A1")["exceptions"][-1]
        self.assertEqual((exc["kind"], exc["retryable"]), ("연결 불명", True))
        self.assertIn("3회 재시도 후에도 실패", exc["message"])
        [alert] = self.alerts()
        self.assertEqual((alert["package"], alert["kind"], alert["retryable"]), ("A1", "blocked", True))
        self.assertIn("Orca runtime is restarting", alert["summary"])

    def test_definite_orca_refusal_blocks_at_once(self):
        fake = FakeOrca(self.repo)
        fake.run_create = mock.Mock(side_effect=orca.OrcaError("invalid_argument", "objective is required"))
        self.assertEqual(self.drive(fake), 2)
        self.assertEqual(self.sleeps, [])
        self.assertFalse(self.store.ticket("A1")["exceptions"][-1]["retryable"])

    def test_a_start_whose_answer_was_lost_is_not_started_twice(self):
        fake = FakeOrca(self.repo)
        orig, lost = fake.worker_start, {"n": 0}

        def started_but_no_answer(rid, tid, *a, **k):
            r = orig(rid, tid, *a, **k)
            if fake.titles[tid][0] == "A1#1 리뷰 codex" and not lost["n"]:
                lost["n"] += 1
                raise orca.DriverError("연결 불명", "orca orchestration worker-start 응답 없음 (300s)")
            return r
        fake.worker_start = started_but_no_answer
        self.assertEqual(self.drive(fake), 0, self.store.ticket("A1").get("exceptions"))
        self.assertEqual(fake.started.count("A1#1 리뷰 codex"), 1)
        self.assertEqual(self.sleeps, [60])

    # ---- failed or lost workers: --retry-of, then blocked
    def test_failed_worker_is_started_again_with_retry_of(self):
        fake = FakeOrca(self.repo)
        orig, failed = fake.worker_start, {"n": 0}

        def impl_fails_once(rid, tid, *a, **k):
            if fake.titles[tid][0] == "A1#1 구현 claude" and not failed["n"]:
                failed["n"] += 1
                fake.started.append("A1#1 구현 claude")
                fake.workers.insert(0, {"dispatchId": "d_bad", "taskId": tid, "state": "failed", "liveness": "exited"})
                fake.tasks[tid]["status"] = "failed"
                return {"dispatch": "d_bad", "handle": "h_bad"}
            return orig(rid, tid, *a, **k)
        fake.worker_start = impl_fails_once
        self.assertEqual(self.drive(fake), 0, self.store.ticket("A1").get("exceptions"))
        retry = [s for s in fake.starts if s[0] == "A1#1 구현 claude"]
        self.assertEqual(retry, [("A1#1 구현 claude", None, "d_bad")])
        self.assertIn("h_bad", fake.closed)  # the failed implementer's kept-open terminal is closed
        self.assertEqual(self.store.ticket("A1")["checkboxes"][0]["retries"], {"impl": 1})

    def test_lost_worker_is_failed_and_retried(self):
        fake = FakeOrca(self.repo)
        orig, lost = fake.worker_start, {"n": 0}

        def impl_lost_once(rid, tid, *a, **k):
            if fake.titles[tid][0] == "A1#1 구현 claude" and not lost["n"]:
                lost["n"] += 1
                fake.workers.insert(0, {"dispatchId": "d_lost", "taskId": tid, "state": "running", "liveness": "exited"})
                fake.tasks[tid]["status"] = "dispatched"
                return {"dispatch": "d_lost", "handle": "h_lost"}
            return orig(rid, tid, *a, **k)
        fake.worker_start = impl_lost_once
        self.assertEqual(self.drive(fake), 0, self.store.ticket("A1").get("exceptions"))
        impl_task = next(t for t, (k, _) in fake.titles.items() if k == "A1#1 구현 claude")
        self.assertIn((impl_task, "failed"), fake.updates)
        self.assertIn(("A1#1 구현 claude", None, "d_lost"), fake.starts)

    def test_worker_failing_every_time_blocks_after_three_retries(self):
        fake = FakeOrca(self.repo)

        def always_fails(rid, tid, *a, retry_of=None, **k):
            fake.starts.append((fake.titles[tid][0], None, retry_of))
            fake.tasks[tid]["status"] = "failed"
            d = f"d_{len(fake.starts)}"
            fake.workers.insert(0, {"dispatchId": d, "taskId": tid, "state": "failed", "liveness": "exited"})
            return {"dispatch": d, "handle": f"h_{len(fake.starts)}"}
        fake.worker_start = always_fails
        self.assertEqual(self.drive(fake), 2)
        self.assertEqual([s[2] for s in fake.starts], [None, "d_1", "d_2", "d_3"])
        exc = self.store.ticket("A1")["exceptions"][-1]
        self.assertEqual(exc["kind"], "정체")
        self.assertIn("3회 재시도 후에도", exc["message"])
        self.assertEqual(self.alerts()[-1]["kind"], "blocked")

    # ---- alerts
    def test_worker_question_is_alerted(self):
        record = {"id": "msg_q1", "from_handle": "dispatch:ctx_w1", "subject": "Question", "type": "question",
                  "body": "키가 없다 — 어떻게 할까?", "payload": json.dumps({"taskId": "task_7"})}
        self.store.save({"package": "A1", "status": "running", "run_id": "run_1", "checkboxes": []})
        driver = TicketDriver(self.cfg, self.design, self.store, "A1")
        with mock.patch("cbd.orca.call", return_value={"messages": [record]}):
            driver.pump("run_1")
        [alert] = self.alerts()
        self.assertEqual((alert["kind"], alert["msg_id"], alert["worker"]), ("question", "msg_q1", "ctx_w1"))
        self.assertTrue(alert["answer"].endswith("msg_q1.md"))
        self.assertIn("키가 없다", alert["summary"])

    def test_alerts_can_be_turned_off(self):
        conf = json.loads(_read(self.cfg.path))
        with open(self.cfg.path, "w") as f:
            json.dump({**conf, "alerts": False}, f)
        self.cfg = Config("ex", self.home)
        self.store = Store(self.cfg)
        fake = FakeOrca(self.repo)
        fake.run_create = mock.Mock(side_effect=orca.OrcaError("invalid_argument", "x"))
        self.assertEqual(self.drive(fake), 2)
        self.assertEqual(self.alerts(), [])

    def test_verify_waits_for_chain_previous(self):
        sh(self.repo, "git", "checkout", "-q", "-b", "EX-1-A1-model")
        self.store.save({"package": "A1", "status": "running"})
        fake = FakeOrca(self.repo)
        self.assertEqual(self.drive(fake, "A2"), 0)
        st = self.store.ticket("A2")
        self.assertEqual((st["status"], st["waiting_on"]), ("box_waiting", "A1"))
        self.assertEqual([c["phase"] for c in st["checkboxes"]], ["done", "done", "pending"])  # [x] in the design is not a commit

    def test_box_prereq_and_approval(self):
        with open(os.path.join(self.home, "ex", "config.json")) as f:
            conf = json.load(f)
        conf["approval_boxes"] = ["A1#2"]
        with open(os.path.join(self.home, "ex", "config.json"), "w") as f:
            json.dump(conf, f)
        self.cfg = Config("ex", self.home)
        self.store = Store(self.cfg)
        self.assertEqual(self.drive(FakeOrca(self.repo)), 2)
        st = self.store.ticket("A1")
        self.assertEqual(st["exceptions"][-1]["kind"], "사람 승인")
        self.assertEqual([c["phase"] for c in st["checkboxes"]][:2], ["done", "pending"])
        self.store.approve("A1", 2)
        self.assertEqual(self.drive(FakeOrca(self.repo)), 0)


if __name__ == "__main__":
    unittest.main()
