"""Design read from a single design.md (no plan/ files): order · tree · chain · checklist tables found by name."""

import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cbd.config import Config  # noqa: E402
from cbd.design import Design  # noqa: E402
from cbd.select import select  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "single_doc")
TC = r"[RFDPX]-\d{2}"


class SingleDocDesignTest(unittest.TestCase):
    def setUp(self):
        self.design = Design(FIXTURE, TC)

    def test_order_follows_implementation_order_table(self):
        self.assertEqual(self.design.order, ["R1", "F0", "F1", "R2", "F3", "D1", "V1"])
        self.assertEqual(self.design.warnings, [])

    def test_tree_row_jira_repo_base_branch_predecessors(self):
        t = self.design.tickets
        r1, r2, f1, d1 = t["R1"], t["R2"], t["F1"], t["D1"]
        self.assertEqual((r1.jira, r1.repo, r1.base, r1.chain_prev, r1.branch, r1.predecessors),
                         ("EX-11", "core", "develop", None, "EX-11-limit-setting", []))
        self.assertEqual((r2.jira, r2.repo, r2.base, r2.chain_prev, r2.branch, r2.predecessors),
                         ("EX-12", "core", None, "R1", "EX-12-attempt-count", ["R1"]))
        self.assertEqual((f1.base, f1.chain_prev), (None, "F0"))
        self.assertEqual(d1.predecessors, ["R1", "F3"])
        self.assertEqual(r1.title, "한도 설정 — 가드·모델")

    def test_chains_from_stack_chain_table(self):
        self.assertEqual(self.design.chains, {"core": ["R1", "R2"], "front-common": ["F0", "F1"], "dashboard": ["D1"]})
        self.assertEqual((self.design.tickets["R2"].chain_position, self.design.tickets["R2"].chain_length), (2, 2))

    def test_branchless_tickets_are_verification_only(self):
        t = self.design.tickets
        self.assertEqual((t["F3"].repo, t["F3"].branch, t["F3"].verification_only), ("front-common", "", True))
        self.assertEqual((t["V1"].repo, t["V1"].branch, t["V1"].verification_only), (None, "", True))
        self.assertFalse(t["R1"].verification_only)

    def test_checklist_boxes_drop_the_done_line(self):
        r1, r2 = self.design.tickets["R1"], self.design.tickets["R2"]
        self.assertEqual([b.title for b in r1.boxes],
                         ["test(core): 한도 spec 추가 (R-01 · R-02)", "feat(core): 한도 판정", "verify(core): R1 대상 spec 초록"])
        self.assertEqual([b.checked for b in r2.boxes], [True, False, False])
        self.assertEqual(r2.boxes[2].prefix, "verify")
        self.assertEqual(len(self.design.tickets["F3"].boxes), 1)

    def test_criteria_from_tree_target_tests(self):
        t = self.design.tickets
        self.assertEqual(t["R1"].tcs, ["R-01", "R-02"])
        self.assertEqual(t["R2"].tcs, ["R-03", "R-04"])
        self.assertEqual(t["F0"].tcs, [])
        self.assertEqual(t["V1"].tcs, [])

    def test_tc_rows_only_from_the_test_matrix(self):
        rows = self.design.tc_rows()
        self.assertEqual(sorted(rows), ["D-01", "F-01", "F-02", "F-09", "R-01", "R-02", "R-03", "R-04"])
        self.assertEqual(rows["F-01"]["layer"], "storybook")  # not the decoy table above the matrix
        self.assertEqual(rows["R-04"]["layer"], "integration")

    def test_index_section_has_checklist_tree_row_and_source(self):
        sec = self.design.tickets["R2"].index_section
        self.assertIn("R2 — EX-12 · `EX-12-attempt-count`", sec)
        self.assertIn("[ ] feat(core): 시도 수 계산", sec)
        self.assertIn("| EX-12 R2 시도 수 계산 (§5) |", sec)
        self.assertIn("정본: design.md", sec)
        self.assertIn("9.3 티켓별 체크리스트", sec)

    def test_missing_design_still_raises(self):
        from cbd.util import DriverError
        empty = tempfile.mkdtemp()
        try:
            with self.assertRaises(DriverError):
                Design(empty, TC)
        finally:
            shutil.rmtree(empty)


class SingleDocSelectTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.home, "ex"))
        self.conf = {"project": "EX9", "design_root": FIXTURE, "tc_pattern": TC,
                     "repos": {"core": {}, "front-common": {}, "dashboard": {}}}
        self.write_conf()

    def tearDown(self):
        shutil.rmtree(self.home)

    def write_conf(self, **extra):
        with open(os.path.join(self.home, "ex", "config.json"), "w") as f:
            json.dump({**self.conf, **extra}, f)
        self.cfg = Config("ex", self.home)
        self.design = Design(FIXTURE, self.cfg["tc_pattern"])

    def sel(self, states, forced=()):
        return {c.package: c for c in select(self.design, states, self.cfg, frozenset(), forced)}

    def test_first_wave_and_verification_tickets_never_start(self):
        c = self.sel({})
        self.assertEqual(sorted(p for p, x in c.items() if x.selected), ["F0", "R1"])
        self.assertEqual(c["R1"].base, {"ref": "develop", "sha": None, "from": None})
        self.assertTrue(c["F3"].reason.startswith("브랜치 없는 검증 티켓"))
        self.assertTrue(c["V1"].reason.startswith("저장소 없음"))
        states = {p: {"status": "verified", "verified_head": "h"} for p in ("F0", "F1")}
        self.assertFalse(self.sel(states, forced={"F3"})["F3"].selected)  # not even by `cbd launch`

    def test_dependent_of_verification_ticket_waits_until_waived(self):
        states = {"R1": {"status": "verified", "verified_head": "abc", "branch": "EX-11-limit-setting"}}
        self.assertEqual(self.sel(states)["D1"].reason, "선행 대기: F3 (브랜치 없는 검증 티켓 — 끝나면 start_waived)")
        self.write_conf(start_waived={"D1": ["F3"]})
        c = self.sel(states)["D1"]
        self.assertTrue(c.selected)
        self.assertEqual(c.base, {"ref": "develop", "sha": None, "from": None})


if __name__ == "__main__":
    unittest.main()
