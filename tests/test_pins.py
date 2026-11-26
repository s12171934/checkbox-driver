"""Front-common pin boxes: the single-doc design names the producer in the box title, and the driver takes the producer's
verified head from its worktree (producer branches are not pushed while implementing)."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cbd.design import Design  # noqa: E402
from cbd.state import VERIFIED  # noqa: E402
from cbd.ticket import TicketDriver  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "single_doc")
TC = r"[RFDPX]-\d{2}"


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class SingleDocPinTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        with open(os.path.join(FIXTURE, "design.md"), encoding="utf-8") as f:
            doc = f.read()
        doc, n = re.subn(r"(^D1 — [^\n]*\n\n```\n)", r"\1[ ] chore(front-common): 핀 → F3 브랜치 헤드\n", doc, flags=re.M)
        self.assertEqual(n, 1)
        with open(os.path.join(self.tmp, "design.md"), "w", encoding="utf-8") as f:
            f.write(doc)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_pin_box_title_names_the_producer(self):
        d = Design(self.tmp, TC)
        self.assertEqual(d.tickets["D1"].pin, "F3")
        self.assertEqual(d.tickets["R1"].pin, None)
        self.assertEqual(d.warnings, [])


class PinBoxTest(unittest.TestCase):
    """pin_box against real repositories: the producer's head exists only in its worktree, not on origin."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        env_git = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
        self.producer = os.path.join(self.tmp, "producer")
        os.makedirs(self.producer)
        sh(self.producer, "git", "init", "-q", "-b", "main")
        sh(self.producer, "git", *env_git, "commit", "-q", "--allow-empty", "-m", "a")
        self.origin = os.path.join(self.tmp, "origin.git")
        sh(self.tmp, "git", "clone", "-q", "--bare", self.producer, self.origin)
        sh(self.producer, "git", "checkout", "-q", "-b", "feat")
        sh(self.producer, "git", *env_git, "commit", "-q", "--allow-empty", "-m", "b (unpushed)")
        self.head = sh(self.producer, "git", "rev-parse", "HEAD")
        self.consumer = os.path.join(self.tmp, "consumer")
        os.makedirs(self.consumer)
        sh(self.consumer, "git", "init", "-q", "-b", "main")
        sh(self.consumer, "git", "-c", "protocol.file.allow=always", "submodule", "add", "-q", self.origin, "lib")
        sh(self.consumer, "git", *env_git, "commit", "-q", "-m", "base")
        for k, v in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
            sh(self.consumer, "git", "config", k, v)
        self.driver = types.SimpleNamespace(
            t=types.SimpleNamespace(pin="F1", repo="web"),
            design=types.SimpleNamespace(tickets={"F1": types.SimpleNamespace(branch="feat")}),
            cfg=types.SimpleNamespace(repo=lambda name: {"pin_path": "lib"}),
            st={"worktree": self.consumer},
            subject=lambda c: "[EX-1] chore(front-common): 핀 → F1 브랜치 헤드")
        self.states = {"F1": {"status": VERIFIED, "verified_head": self.head, "worktree": self.producer}}

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def gitlink(self, rev):
        return sh(self.consumer, "git", "rev-parse", f"{rev}:lib")

    def test_pins_the_unpushed_producer_head_from_its_worktree(self):
        c = {}
        TicketDriver.pin_box(self.driver, c, self.states)
        self.assertEqual(self.gitlink("HEAD"), self.head)
        self.assertEqual(c["commit"], sh(self.consumer, "git", "rev-parse", "HEAD"))

    def test_pin_already_at_head_is_an_empty_commit(self):
        TicketDriver.pin_box(self.driver, {}, self.states)
        first = sh(self.consumer, "git", "rev-parse", "HEAD")
        c = {}
        TicketDriver.pin_box(self.driver, c, self.states)
        self.assertNotEqual(c["commit"], first)
        self.assertEqual(sh(self.consumer, "git", "rev-parse", "HEAD^{tree}"), sh(self.consumer, "git", "rev-parse", f"{first}^{{tree}}"))


if __name__ == "__main__":
    unittest.main()
