"""chain_serial: a ticket whose chain prev is not a DAG predecessor still waits for it, so it starts on its head."""

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


class ChainSerialTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        with open(os.path.join(FIXTURE, "design.md"), encoding="utf-8") as f:
            doc = f.read()
        # R2 stays on R1 in the chain but no longer depends on it
        doc = doc.replace("| 2 | R2 시도 수 계산 | R1 |", "| 2 | R2 시도 수 계산 | — |")
        doc = doc.replace("| R1 | R-03~R-04 | R1 |", "| R1 | R-03~R-04 | — |")
        os.makedirs(os.path.join(self.tmp, "design"))
        with open(os.path.join(self.tmp, "design", "design.md"), "w", encoding="utf-8") as f:
            f.write(doc)
        os.makedirs(os.path.join(self.tmp, "home", "ex"))
        self.conf = {"project": "EX", "design_root": os.path.join(self.tmp, "design"), "max_parallel": 9,
                     "tc_pattern": r"[RFDPX]-\d{2}", "repos": {"core": {}, "front-common": {}, "dashboard": {}}}

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def sel(self, states, **extra):
        with open(os.path.join(self.tmp, "home", "ex", "config.json"), "w") as f:
            json.dump({**self.conf, **extra}, f)
        cfg = Config("ex", os.path.join(self.tmp, "home"))
        design = Design(cfg["design_root"], cfg["tc_pattern"])
        self.assertEqual((design.tickets["R2"].chain_prev, design.tickets["R2"].predecessors), ("R1", []))
        return {c.package: c for c in select(design, states, cfg, frozenset(), ())}

    def test_off_chain_tickets_start_together_on_the_repo_base(self):
        c = self.sel({})
        self.assertTrue(c["R2"].selected)
        self.assertEqual(c["R2"].base["from"], None)

    def test_on_ticket_waits_for_its_chain_prev_and_starts_on_its_head(self):
        self.assertEqual(self.sel({}, chain_serial=True)["R2"].reason, "선행 대기: R1")
        c = self.sel({"R1": {"status": "verified", "verified_head": "abc", "branch": "EX-11-limit-setting"}},
                     chain_serial=True)["R2"]
        self.assertTrue(c.selected)
        self.assertEqual(c.base, {"ref": "EX-11-limit-setting", "sha": "abc", "from": "R1"})


if __name__ == "__main__":
    unittest.main()
