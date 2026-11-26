"""Boundary rebase + rebase_pending (같은 저장소 체인), the consumer-start contract check (체크박스를 넘는 검증 §1) and the
UI verify scene with snapshot saving (§2), against the fake Orca of test_ticket and a real git repository."""

import json
import os
import shutil
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import test_ticket as TT  # noqa: E402  (module import: its TestCase is not collected twice)
from test_ticket import FIXTURE, FakeOrca, _read, sh  # noqa: E402

from cbd.config import Config  # noqa: E402
from cbd.design import Design, contract_in  # noqa: E402
from cbd.select import select  # noqa: E402
from cbd.state import Store  # noqa: E402

SINGLE = os.path.join(ROOT, "tests", "fixtures", "single_doc")


class Fake(FakeOrca):
    """+ the contract worker (writes result.md), the UI verify scene (ui-scene.md), and a hook after each judge."""

    def __init__(self, repo, behaviour=None, after_judge=None):
        super().__init__(repo, behaviour)
        self.after_judge = after_judge or {}

    def worker_start(self, rid, tid, wt, agent, model, effort, title, keep_open=False, retry_of=None, terminal=None):
        key, spec = self.titles[tid]
        if "계약 대조" in key:
            self.started.append(key)
            self.starts.append((key, terminal, retry_of))
            d = f"d_{tid}"
            self.workers.insert(0, {"dispatchId": d, "taskId": tid, "state": "succeeded", "liveness": "exited",
                                    "terminal": f"h_{tid}"})
            verdict = self.behaviour.get("contract", "일치")
            if verdict:
                with open(os.path.join(os.path.dirname(spec), "result.md"), "w") as f:
                    f.write(f"판정: {verdict}\n\n| API | 요청 | 계약(설계) | 실제 응답 | 판정 |\n")
            self.tasks[tid]["status"] = "completed"
            return {"dispatch": d, "handle": f"h_{tid}"}
        out = super().worker_start(rid, tid, wt, agent, model, effort, title, keep_open, retry_of, terminal)
        if " 구현 " in key:
            n = int(key.split("#")[1].split()[0])
            scenes = self.behaviour.get("scene", {}).get(n)
            if scenes:
                with open(os.path.join(os.path.dirname(spec), "ui-scene.md"), "w") as f:
                    f.write(f"판정: {scenes.pop(0)}\n\n| 화면 | 프레임 | 캡처 | 차이 |\n")
        return out

    def _judge(self, tid, wt, work):
        super()._judge(tid, wt, work)
        n = int(self.titles[tid][0].split("#")[1].split()[0])
        hook = self.after_judge.get(n)
        if hook:
            hook()


class Base(unittest.TestCase):
    setUp = TT.TicketTest.setUp
    tearDown = TT.TicketTest.tearDown
    drive = TT.TicketTest.drive
    alerts = TT.TicketTest.alerts

    def conf(self, **extra):
        c = json.loads(_read(self.cfg.path))
        c.update(extra)
        with open(self.cfg.path, "w") as f:
            json.dump(c, f)
        self.cfg = Config("ex", self.home)
        self.store = Store(self.cfg)

    def a1_head(self, content="a1"):
        """A1's branch with one commit on main -> its head (A1 itself is not driven here)."""
        sh(self.repo, "git", "checkout", "-q", "-b", "EX-1-A1-model", "main")
        with open(os.path.join(self.repo, "f1.txt" if content != "a1" else "a1.txt"), "w") as f:
            f.write(content)
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-q", "-m", "[EX-1] a1")
        head = sh(self.repo, "git", "rev-parse", "HEAD")
        sh(self.repo, "git", "checkout", "-q", "main")
        return head

    def a1(self, status, head=None):
        self.store.save({"package": "A1", "status": status, "verified_head": head, "branch": "EX-1-A1-model",
                         "worktree": self.repo})

    def subjects(self, rng="main..HEAD"):
        return sh(self.repo, "git", "log", "--format=%s", rng).splitlines()


class BoundaryRebaseTest(Base):
    def test_chain_prev_verified_mid_ticket_moves_the_branch_at_the_next_boundary(self):
        head = self.a1_head()
        self.a1("running")
        fake = Fake(self.repo, after_judge={1: lambda: self.a1("verified", head)})
        self.assertEqual(self.drive(fake, "A2"), 0, self.store.ticket("A2").get("exceptions"))
        st = self.store.ticket("A2")
        self.assertEqual(st["status"], "verified")
        self.assertEqual(st["base"], {"ref": "EX-1-A1-model", "sha": head, "from": "A1"})
        self.assertEqual(st["base_sha"], head)
        self.assertEqual(sh(self.repo, "git", "merge-base", "--is-ancestor", head, "HEAD"), "")
        self.assertEqual(self.subjects(f"{head}..HEAD"), ["[EX-2] verify: A2 테스트 그린", "[EX-2] feat(core): 서비스",
                                                          "[EX-2] test(core): TC-A-04 빨강"])
        (rb,) = st["rebases"]
        self.assertEqual((rb["from"]["from"], rb["onto"]["from"]), (None, "A1"))
        self.assertTrue(any("rebase-after" in x for x in rb["after"]))  # green before -> measured again after
        # the done checkbox's commit hash follows the rebase
        self.assertEqual(sh(self.repo, "git", "rev-list", "--count", f"{st['checkboxes'][0]['commit']}..HEAD"), "2")

    def test_conflicting_rebase_stops_and_leaves_the_branch(self):
        head = self.a1_head(content="other")  # A1 adds f1.txt, A2's first checkbox adds f1.txt too
        self.a1("running")
        fake = Fake(self.repo, after_judge={1: lambda: self.a1("verified", head)})
        self.assertEqual(self.drive(fake, "A2"), 2)
        st = self.store.ticket("A2")
        self.assertEqual(st["exceptions"][-1]["kind"], "리베이스 충돌")
        self.assertEqual(sh(self.repo, "git", "rev-parse", "HEAD"), st["checkboxes"][0]["commit"])
        self.assertEqual(sh(self.repo, "git", "status", "--porcelain", "--untracked-files=no"), "")
        self.assertNotIn("rebases", st)

    def test_finished_before_chain_prev_is_rebase_pending_then_rebased_and_verified(self):
        design = os.path.join(self.home, "design")
        shutil.copytree(FIXTURE, design)
        p = os.path.join(design, "plan", "commits.md")
        with open(p) as f:
            text = f.read().replace("- [x] `verify: A2 테스트 그린`(데스크톱)\n", "")
        with open(p, "w") as f:
            f.write(text)
        self.design = Design(design)
        head = self.a1_head()
        self.a1("running")
        self.assertEqual(self.drive(Fake(self.repo), "A2"), 0)
        st = self.store.ticket("A2")
        self.assertEqual((st["status"], st["waiting_on"]), ("rebase_pending", "A1"))
        c = {x.package: x for x in select(self.design, self.store.all(self.design.order), self.cfg)}
        self.assertEqual(c["A2"].reason, "리베이스 대기: 체인 앞 A1 verified 뒤")
        self.a1("verified", head)
        c = {x.package: x for x in select(self.design, self.store.all(self.design.order), self.cfg)}
        self.assertTrue(c["A2"].selected, c["A2"].reason)
        self.assertEqual(self.drive(Fake(self.repo), "A2"), 0)
        st = self.store.ticket("A2")
        self.assertEqual(st["status"], "verified")
        self.assertEqual(st["base"]["from"], "A1")
        self.assertEqual(self.subjects(f"{head}..{st['verified_head']}"),
                         ["[EX-2] feat(core): 서비스", "[EX-2] test(core): TC-A-04 빨강"])


class ContractCheckTest(Base):
    def setUp(self):
        super().setUp()
        self.conf(contract_checks={"A2": {"producer": "A1", "apis": "GET /api/a (§7)"}})

    def test_match_lets_the_ticket_start(self):
        self.a1("verified", sh(self.repo, "git", "rev-parse", "main"))
        fake = Fake(self.repo)
        self.assertEqual(self.drive(fake, "A2"), 0)
        st = self.store.ticket("A2")
        self.assertEqual((st["status"], st["contract"]["phase"], st["contract"]["verdict"]), ("verified", "done", "일치"))
        self.assertEqual(fake.started[0], "A2#계약 대조 claude")
        brief = _read(os.path.join(self.cfg.work_dir, "A2", "contract", "contract-brief.md"))
        for s in ("격리 스택", "GET /api/a (§7)", st["contract"]["producers"]["A1"], "판정: 불일치"):
            self.assertIn(s, brief)

    def test_mismatch_stops_before_the_first_checkbox_and_resolve_checks_again(self):
        self.a1("verified", sh(self.repo, "git", "rev-parse", "main"))
        fake = Fake(self.repo, {"contract": "불일치"})
        self.assertEqual(self.drive(fake, "A2"), 2)
        st = self.store.ticket("A2")
        self.assertEqual(st["exceptions"][-1]["kind"], "계약 불일치")
        self.assertFalse([k for k in fake.started if "구현" in k])
        self.store.resolve(st)
        fake.behaviour["contract"] = "일치"
        self.assertEqual(self.drive(fake, "A2"), 0)
        self.assertEqual(sum(1 for k in fake.started if "계약 대조" in k), 2)
        self.assertEqual(self.store.ticket("A2")["status"], "verified")

    def test_waits_for_the_producer(self):
        self.a1("running")
        fake = Fake(self.repo)
        self.assertEqual(self.drive(fake, "A2"), 0)
        st = self.store.ticket("A2")
        self.assertEqual((st["status"], st["waiting_on"]), ("box_waiting", "A1"))
        self.assertEqual(fake.started, [])

    def test_config_false_turns_a_design_line_off(self):
        self.conf(contract_checks={"A2": False})
        self.a1("verified", sh(self.repo, "git", "rev-parse", "main"))
        fake = Fake(self.repo)
        self.assertEqual(self.drive(fake, "A2"), 0)
        self.assertFalse([k for k in fake.started if "계약" in k])


class ContractDesignTest(unittest.TestCase):
    def test_line_format(self):
        self.assertEqual(contract_in("실서버 대조: R1 — M4 · M5 (§7)"), {"producers": ["R1"], "apis": "M4 · M5 (§7)"})
        self.assertEqual(contract_in("- **실서버 대조**: R1 · R2"), {"producers": ["R1", "R2"], "apis": "R1 · R2"})
        self.assertIsNone(contract_in("실서버 대조: —"))

    def test_single_doc_line_under_the_ticket_and_unknown_producer_warning(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(SINGLE, "design.md"), encoding="utf-8") as f:
                doc = f.read()
            doc = doc.replace("D1 — EX-16 · `EX-16-dashboard`\n", "D1 — EX-16 · `EX-16-dashboard`\n\n"
                              "실서버 대조: R1 — M4 · M5 (§7)\n")
            doc = doc.replace("F1 — EX-14 · `EX-14-teacher`\n", "F1 — EX-14 · `EX-14-teacher`\n\n실서버 대조: Z9 — x\n")
            with open(os.path.join(tmp, "design.md"), "w", encoding="utf-8") as f:
                f.write(doc)
            d = Design(tmp, r"[RFDPX]-\d{2}")
            self.assertEqual(d.tickets["D1"].contract, {"producers": ["R1"], "apis": "M4 · M5 (§7)"})
            self.assertIsNone(d.tickets["R1"].contract)
            self.assertTrue(any("F1: 실서버 대조의 생산자 Z9" in w for w in d.warnings))
        finally:
            shutil.rmtree(tmp)

    def test_ticket_index_line(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        try:
            design = os.path.join(tmp, "d")
            shutil.copytree(FIXTURE, design)
            p = os.path.join(design, "plan", "ticket-index.md")
            with open(p) as f:
                text = f.read().replace("- 테스트: [tests/A](../tests/A.md) TC-A-04\n",
                                        "- 테스트: [tests/A](../tests/A.md) TC-A-04\n- 실서버 대조: A1 — GET /a\n")
            with open(p, "w") as f:
                f.write(text)
            self.assertEqual(Design(design).tickets["A2"].contract, {"producers": ["A1"], "apis": "GET /a"})
        finally:
            shutil.rmtree(tmp)


class UiSceneTest(Base):
    def setUp(self):
        super().setUp()
        self.conf(ui_tickets=["A1"])

    def test_mismatch_inserts_a_fix_checkbox_before_verify_and_runs_verify_again(self):
        fake = Fake(self.repo, {"no_stage": (3,), "scene": {3: ["불일치", "일치"]}})
        self.assertEqual(self.drive(fake), 0, self.store.ticket("A1").get("exceptions"))
        st = self.store.ticket("A1")
        self.assertEqual(st["status"], "verified")
        self.assertEqual([c["index"] for c in st["checkboxes"]], [1, 2, 4, 3])
        fix = st["checkboxes"][2]
        self.assertEqual((fix["fix_source"], fix["fix_for"], fix["title"]), ("ui_scene", 3, "fix: 화면 대조 불일치 반영"))
        self.assertEqual(self.subjects(), ["[EX-1] verify: A1 테스트 그린", "[EX-1] fix: 화면 대조 불일치 반영",
                                           "[EX-1] feat(core): 모델 추가", "[EX-1] test(core): TC-A-01~TC-A-03 빨강"])
        self.assertEqual(sum(1 for k in fake.started if k == "A1#3 구현 claude"), 2)
        work3 = os.path.join(self.cfg.work_dir, "A1", "03")
        self.assertTrue(os.path.exists(os.path.join(work3, "ui-scene-1.md")))
        self.assertIn(os.path.join(work3, "ui-scene-1.md"), _read(os.path.join(self.cfg.work_dir, "A1", "04",
                                                                                 "impl-brief.md")))
        verify_brief = _read(os.path.join(work3, "impl-brief.md"))
        self.assertIn("화면 한 장면 대조", verify_brief)
        self.assertIn(os.path.join(FIXTURE, "snapshots", "A1"), verify_brief)
        self.assertIn("조회한 프레임은 대조 전에 스냅숏으로 저장한다", verify_brief)
        self.assertIn("ui_scene", [a["kind"] for a in self.alerts()])

    def test_mismatch_beyond_the_fix_limit_waits_for_a_person_who_may_accept_it(self):
        self.conf(ui_scene_max_fix=0)
        fake = Fake(self.repo, {"no_stage": (3,), "scene": {3: ["불일치", "불일치"]}})
        self.assertEqual(self.drive(fake), 2)
        st = self.store.ticket("A1")
        self.assertEqual(st["exceptions"][-1]["kind"], "사람 판정")
        self.assertEqual(st["checkboxes"][2]["phase"], "pending")
        self.store.ui_accept("A1", 3, "시안끼리 모순 — 1-B 와 1-H 의 간격이 다르다")
        self.store.resolve(st)
        self.assertEqual(self.drive(fake), 0)
        body = sh(self.repo, "git", "log", "-1", "--format=%B")
        self.assertIn("사람 수용", body)
        self.assertIn("시안끼리 모순", body)
        self.assertEqual(self.store.ticket("A1")["status"], "verified")

    def test_cannot_judge_stops_for_a_person(self):
        fake = Fake(self.repo, {"no_stage": (3,), "scene": {3: ["판단 불가"]}})
        self.assertEqual(self.drive(fake), 2)
        self.assertEqual(self.store.ticket("A1")["exceptions"][-1]["kind"], "사람 판정")

    def test_restart_recomputes_kind_after_the_ticket_leaves_ui_tickets(self):
        """Stopped on a UI verify (agent kind, tasks cleared), then dropped from ui_tickets: the restart follows the
        current config — #3 becomes a driver verify instead of an agent box without tasks (KeyError 'tasks')."""
        fake = Fake(self.repo, {"no_stage": (3,), "scene": {3: ["판단 불가"]}})
        self.assertEqual(self.drive(fake), 2)
        st = self.store.ticket("A1")
        c3 = next(c for c in st["checkboxes"] if c["index"] == 3)
        self.assertEqual((c3["kind"], c3["phase"], c3.get("tasks")), ("agent", "pending", None))
        self.conf(ui_tickets=[])
        self.store.resolve(st)
        started = len(fake.started)
        self.assertEqual(self.drive(fake), 0, self.store.ticket("A1").get("exceptions"))
        st = self.store.ticket("A1")
        c3 = next(c for c in st["checkboxes"] if c["index"] == 3)
        self.assertEqual(c3["kind"], "driver-verify")
        self.assertNotIn("A1#3 구현 claude", fake.started[started:])
        self.assertEqual(st["status"], "verified")

    def test_non_ui_ticket_verify_has_no_scene(self):
        self.conf(ui_tickets=[], agent_box_overrides=["A1#3"])
        fake = Fake(self.repo, {"no_stage": (3,)})
        self.assertEqual(self.drive(fake), 0)
        self.assertNotIn("화면 한 장면 대조", _read(os.path.join(self.cfg.work_dir, "A1", "03", "impl-brief.md")))


if __name__ == "__main__":
    unittest.main()
