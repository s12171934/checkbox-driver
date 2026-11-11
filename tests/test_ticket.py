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

    def run_create(self, objective):
        return "run_1"

    def run_use(self, rid):
        pass

    def task_create(self, rid, title, spec, deps):
        self.n += 1
        tid = f"task_{self.n}"
        self.tasks[tid] = {"id": tid, "status": "pending"}
        self.titles[tid] = (title, spec)
        return tid

    def task_list(self, rid):
        return self.tasks

    def _work(self, spec):
        return os.path.dirname(spec)

    def worker_start(self, rid, tid, wt, agent, model, effort, title, keep_open=False, retry_of=None):
        key, spec = self.titles[tid]
        self.started.append(key)
        work = self._work(spec)
        if "구현" in key:
            n = key.split("#")[1].split()[0]
            with open(os.path.join(wt, f"f{n}.txt"), "w") as f:
                f.write(n)
            sh(wt, "git", "add", f"f{n}.txt")
            title = _read(spec).split("첫 줄은 정확히 `")[1].split("`")[0]
            with open(os.path.join(work, "commit-message.md"), "w") as f:
                f.write(title + "\n")
            self.tasks[tid]["status"] = "completed"
        elif "리뷰" in key:
            name = key.split()[-1]
            if self.behaviour.get("review") != "fail-no-file":
                with open(os.path.join(work, f"findings-{name}.md"), "w") as f:
                    f.write("없음\n")
            self.tasks[tid]["status"] = "failed" if self.behaviour.get("review", "").startswith("fail") else "completed"
        else:
            self._judge(tid, wt, work)
        return {"dispatch": f"d_{tid}", "handle": f"h_{tid}"}

    def _judge(self, tid, wt, work):
        title = _read(os.path.join(work, "commit-message.md")).strip()
        sh(wt, "git", "commit", "-q", "-m", title)
        self.tasks[tid]["status"] = "completed"

    def dispatch(self, rid, tid, handle):
        key, spec = self.titles[tid]
        self._judge(tid, self.repo, self._work(spec))
        return f"d_{tid}"

    def live_handles(self):
        return frozenset(f"h_{t}" for t in self.tasks)

    def check_wait(self, rid, ms):
        return []

    def worker_release(self, d):
        pass

    def terminal_close(self, h):
        self.closed = getattr(self, "closed", []) + [h]

    def worktree_for_branch(self, b):
        return None

    def worktree_create(self, repo, branch, base):
        sh(self.repo, "git", "checkout", "-q", "-b", branch)
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
        os.makedirs(os.path.join(self.home, "ex"))
        with open(os.path.join(self.home, "ex", "config.json"), "w") as f:
            json.dump({"project": "EX", "design_root": FIXTURE, "notify": False, "usage": {"commands": {}},
                       "repos": {"core": {"test_cmd": ["true", "{tcs}"]}}}, f)
        self.cfg = Config("ex", self.home)
        self.design = Design(FIXTURE)
        self.store = Store(self.cfg)

    def tearDown(self):
        shutil.rmtree(self.home)

    def drive(self, fake, pkg="A1"):
        names = ["run_create", "run_use", "task_create", "task_list", "worker_start", "dispatch", "live_handles",
                 "check_wait", "worker_release", "worktree_for_branch", "worktree_create", "terminal_close"]
        with mock.patch.multiple(orca, **{n: getattr(fake, n) for n in names}), \
                mock.patch("cbd.plan.git", return_value="deadbeef"):
            return TicketDriver(self.cfg, self.design, self.store, pkg).run()

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
        self.assertEqual(len(fake.closed), 2)  # each agent checkbox's implementing terminal is closed after its commit
        # the test commit's TC rows were handed to the implementer
        work = os.path.join(self.cfg.work_dir, "A1", "01")
        self.assertIn("TC-A-02", _read(os.path.join(work, "tc-rows.md")))
        plan = json.loads(_read(self.store.p("plan.json")))
        a1 = next(t for t in plan["tickets"] if t["package"] == "A1")
        self.assertEqual((a1["boxes_done"], a1["status"]), (3, "verified"))
        self.assertTrue(all(s["status"] == "done" for s in a1["sessions"]))

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

    def test_restart_resumes_after_done_signal(self):
        fake = FakeOrca(self.repo)
        orig = fake.dispatch
        calls = {"n": 0}

        def crash_once(rid, tid, handle):
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyboardInterrupt  # the driver dies while the judge is dispatched
            return orig(rid, tid, handle)
        fake.dispatch = crash_once
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
