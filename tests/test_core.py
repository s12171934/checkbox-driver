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

from cbd import boxes as B  # noqa: E402
from cbd import plan as P  # noqa: E402
from cbd.config import Config  # noqa: E402
from cbd.design import Design, expand_tcs  # noqa: E402
from cbd.gates import Gate, parse_reading  # noqa: E402
from cbd.select import base_for, chain_gaps, select  # noqa: E402
from cbd.state import Store, TicketLock  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "design")


class Env(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.home, "ex"))
        self.conf = {"project": "EX", "design_root": FIXTURE, "ui_tickets": ["B*"], "max_parallel": 2,
                     "repos": {"core": {"test_cmd": ["true", "{tcs}"]}, "web": {"pin_path": "lib"}}}
        self.write_conf()

    def tearDown(self):
        shutil.rmtree(self.home)

    def write_conf(self, **extra):
        with open(os.path.join(self.home, "ex", "config.json"), "w") as f:
            json.dump({**self.conf, **extra}, f)
        self.cfg = Config("ex", self.home)
        self.design = Design(FIXTURE, self.cfg["tc_pattern"])
        self.store = Store(self.cfg)


class DesignTest(Env):
    def test_tickets_chains_branches(self):
        d = self.design
        self.assertEqual(d.order, ["A1", "A2", "B1"])
        self.assertEqual(d.chains, {"core": ["A1", "A2"], "web": ["B1"]})
        a2, b1 = d.tickets["A2"], d.tickets["B1"]
        self.assertEqual((a2.jira, a2.branch, a2.chain_prev, a2.predecessors), ("EX-2", "EX-2-A2-service", "A1", ["A1"]))
        self.assertEqual((b1.repo, b1.base, b1.pin), ("web", "develop", "A1"))
        self.assertIsNone(a2.base)
        self.assertEqual(d.warnings, [])

    def test_boxes_ignore_rule_lines_and_keep_suffix(self):
        a2 = self.design.tickets["A2"]
        self.assertEqual([b.title for b in self.design.tickets["A1"].boxes][0], "test(core): TC-A-01~TC-A-03 빨강")
        self.assertEqual(len(a2.boxes), 3)
        self.assertTrue(a2.boxes[2].checked)
        self.assertEqual(a2.boxes[2].note, "(데스크톱)")
        self.assertEqual(a2.boxes[2].prefix, "verify")

    def test_criteria_ranges_and_layer_phrase(self):
        self.assertEqual(self.design.tickets["A1"].tcs, ["TC-A-01", "TC-A-02", "TC-A-03"])
        # 「tests/M 의 storybook 층 TC-M-* 전부」 — the story table has no 층 column, its 스토리 header marks the layer
        self.assertEqual(self.design.tickets["B1"].tcs, ["TC-M-01", "TC-M-02"])

    def test_expand_tcs(self):
        self.assertEqual(expand_tcs("TC-A-08~TC-A-10 · TC-B-1", r"TC-[A-Z]+-\d+"),
                         ["TC-A-08", "TC-A-09", "TC-A-10", "TC-B-1"])


class BoxTest(Env):
    def test_kinds(self):
        a1, b1 = self.design.tickets["A1"], self.design.tickets["B1"]
        self.assertEqual([B.kind(self.cfg, a1, b) for b in a1.boxes], [B.AGENT, B.AGENT, B.DRIVER_VERIFY])
        # UI ticket verify is an agent box; a pin chore is a driver box
        self.assertEqual([B.kind(self.cfg, b1, b) for b in b1.boxes], [B.DRIVER_PIN, B.AGENT, B.AGENT])

    def test_sessions_and_deps(self):
        rows = B.ticket_sessions(self.cfg, self.design.tickets["A1"])
        keys = [r["key"] for r in rows]
        self.assertEqual(keys[:4], ["A1#1 구현 claude", "A1#1 리뷰 codex", "A1#1 리뷰 claude", "A1#1 판정 claude"])
        self.assertEqual(rows[3]["deps"], ["A1#1 리뷰 codex", "A1#1 리뷰 claude"])
        self.assertEqual(rows[4]["deps"], ["A1#1 판정 claude"])  # next checkbox waits for the previous judge
        self.assertEqual(rows[-1]["key"], "A1#3 드라이버 verify")


class SelectTest(Env):
    def sel(self, states, alive=frozenset(), forced=()):
        return {c.package: c for c in select(self.design, states, self.cfg, alive, forced)}

    def test_first_wave(self):
        c = self.sel({})
        self.assertTrue(c["A1"].selected)
        self.assertEqual(c["A1"].base, {"ref": "main", "sha": None, "from": None})
        self.assertEqual(c["A2"].reason, "선행 대기: A1")
        self.assertEqual(c["B1"].reason, "선행 대기: A1")

    def test_base_is_nearest_verified_chain_head(self):
        states = {"A1": {"status": "verified", "verified_head": "abc", "branch": "EX-1-A1-model"}}
        c = self.sel(states)
        self.assertTrue(c["A2"].selected)
        self.assertEqual(c["A2"].base, {"ref": "EX-1-A1-model", "sha": "abc", "from": "A1"})

    def test_parallel_limit_counts_live_drivers_only(self):
        self.write_conf(max_parallel=1)
        states = {"A1": {"status": "verified", "verified_head": "abc"}}
        c = self.sel(states)
        self.assertEqual([p for p, x in c.items() if x.selected], ["A2"])
        self.assertTrue(c["B1"].reason.startswith("기동 대기"))

    def test_running_dead_driver_is_restarted_until_limit(self):
        states = {"A1": {"status": "running", "driver_terminal_handle": "t1", "worktree": "/x", "base": {"ref": "main"}}}
        self.assertEqual(self.sel(states, alive={"t1"})["A1"].reason, "실행 중")
        self.assertTrue(self.sel(states)["A1"].restart)
        states["A1"]["restarts"] = 3
        self.assertIn("재기동", self.sel(states)["A1"].reason)

    def test_hold_and_force(self):
        self.write_conf(hold={"A1": "사람 로그인"})
        self.assertEqual(self.sel({})["A1"].reason, "보류: 사람 로그인")
        self.assertTrue(self.sel({}, forced={"A1"})["A1"].selected)

    def test_waived_predecessor(self):
        self.write_conf(start_waived={"B1": ["A1"]})
        self.assertTrue(self.sel({})["B1"].selected)

    def test_chain_gap_stops_the_chain(self):
        states = {"A2": {"status": "verified", "verified_head": "def"}}
        self.assertIn("A2", chain_gaps(self.design, states)["core"])
        self.assertTrue(self.sel(states)["A1"].reason.startswith("체인 결함"))

    def test_blocked_shows_reason(self):
        states = {"A1": {"status": "blocked", "exceptions": [{"kind": "정체", "message": "워커 무응답"}]}}
        self.assertEqual(self.sel(states)["A1"].reason, "멈춤: [정체] 워커 무응답")

    def test_jsonl_shape(self):
        d = self.sel({})["A1"].as_dict()
        self.assertEqual(set(d), {"item", "selected", "label", "reason", "base"})


class PlanTest(Env):
    def test_plan_contract(self):
        with mock.patch("cbd.plan.git", return_value="deadbeef"):
            plan = P.write(self.cfg, self.design, self.store)
        self.assertEqual(plan["driver"], "checkbox-driver")
        t = {x["package"]: x for x in plan["tickets"]}
        self.assertEqual(t["A1"]["status"], "ready")
        self.assertEqual(t["A2"]["status"], "planned")
        self.assertEqual(t["A1"]["boxes_total"], 3)
        s = t["A1"]["sessions"][0]
        self.assertEqual({k for k in s}, {"key", "box", "kind", "agent", "model", "status", "deps", "title"})
        self.assertEqual(t["A2"]["worktree"]["trigger"]["chain_previous"], "A1")
        self.assertTrue(os.path.exists(self.store.p("state.md")))
        with open(self.store.p("plan.json")) as f:
            self.assertEqual(json.load(f)["design_commit"], "deadbeef")

    def test_session_status_from_state(self):
        st = {"package": "A1", "status": "running", "checkboxes": [
            {"index": 1, "title": "x", "phase": "running", "tasks": {"impl": "t1", "review-codex": "t2"},
             "dispatch": {"impl": "d1"}, "done": {"impl": "now"}}]}
        self.assertEqual(P.session_status(st, {"box": 1, "kind": "impl"}), "done")
        self.assertEqual(P.session_status(st, {"box": 1, "kind": "review-codex"}), "created")
        self.assertEqual(P.session_status(st, {"box": 2, "kind": "impl"}), "planned")


class GateTest(Env):
    def test_parse_claude_usage_raw(self):
        data = {"snapshot": {"limits": [{"bucket": "session", "percent": 93, "resets_kst": "x"}]},
                "raw": {"five_hour": {"utilization": 93.0, "resets_at": "2030-01-01T00:00:00+00:00"},
                        "limits": [{"kind": "x", "percent": 99, "scope": {"model": {"display_name": "Opus"}}}]}}
        pct, reset = parse_reading(data)
        self.assertEqual(pct, 93.0)  # the model-only 99 % does not block the pool
        self.assertIsNotNone(reset)

    def test_reading_failure_reuses_last_then_proceeds(self):
        g = Gate(self.cfg, self.store, lambda m: None)
        g.commands = {"claude": ["false"]}
        with mock.patch("cbd.gates.time.sleep"):
            self.assertEqual(g.blocked("claude")[0], False)  # never read: proceed
            from cbd.util import write_json, now_ts
            write_json(self.store.p("usage-claude.json"), {"at": 0, "pct": 95, "reset": now_ts() + 600})
            self.assertTrue(g.blocked("claude")[0])  # last reading 95 % before reset: wait
            write_json(self.store.p("usage-claude.json"), {"at": 0, "pct": 95, "reset": now_ts() - 1})
            self.assertFalse(g.blocked("claude")[0])  # reset passed: 0 %


class LockTest(Env):
    def test_one_driver_per_ticket_and_dead_owner_takeover(self):
        a, b = TicketLock(self.store, "A1"), TicketLock(self.store, "A1")
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire())
        a.release()
        self.assertTrue(b.acquire())
        b.release()
        os.makedirs(self.store.p("locks", "A2"))
        with open(self.store.p("locks", "A2", "pid"), "w") as f:
            f.write("999999")
        self.assertTrue(TicketLock(self.store, "A2").acquire())


class CliTest(Env):
    def test_select_jsonl(self):
        env = {**os.environ, "CBD_HOME": self.home, "PATH": "/usr/bin:/bin"}  # no orca: no live terminals
        r = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "cbd"), "ex", "select", "--json"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = [json.loads(l) for l in r.stdout.splitlines()]
        self.assertEqual([x["item"] for x in rows if x["selected"]], ["A1"])


if __name__ == "__main__":
    unittest.main()
