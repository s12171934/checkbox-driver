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
import unittest.mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cbd.design import Design  # noqa: E402
from cbd.state import VERIFIED  # noqa: E402
from cbd.ticket import TicketDriver  # noqa: E402
from cbd.util import DriverError  # noqa: E402

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


class FirstInitTest(unittest.TestCase):
    """PRD-94 D3: the consumer worktree is made from the chain prev's verified head, whose gitlink is the producer's
    unpushed head; the submodule is not initialized yet, and origin does not have that commit ("not our ref")."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "protocol.file.allow", "GIT_CONFIG_VALUE_0": "always"}
        self.env = unittest.mock.patch.dict(os.environ, env)
        self.env.start()
        g = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
        # producer repo: main is pushed to its bare origin, feat (the pinned head) is not
        self.producer = os.path.join(self.tmp, "producer")
        os.makedirs(self.producer)
        sh(self.producer, "git", "init", "-q", "-b", "main")
        sh(self.producer, "git", *g, "commit", "-q", "--allow-empty", "-m", "a")
        self.lib_origin = os.path.join(self.tmp, "lib-origin.git")
        sh(self.tmp, "git", "clone", "-q", "--bare", self.producer, self.lib_origin)
        sh(self.producer, "git", "checkout", "-q", "-b", "feat")
        with open(os.path.join(self.producer, "x.txt"), "w") as f:
            f.write("unpushed\n")
        sh(self.producer, "git", "add", "x.txt")
        sh(self.producer, "git", *g, "commit", "-q", "-m", "b (unpushed)")
        self.head = sh(self.producer, "git", "rev-parse", "HEAD")
        # consumer repo (its own bare origin): base pins lib at main; the chain prev D1 pins lib at the unpushed head
        seed = os.path.join(self.tmp, "seed")
        os.makedirs(seed)
        sh(seed, "git", "init", "-q", "-b", "main")
        sh(seed, "git", "submodule", "add", "-q", self.lib_origin, "lib")
        sh(seed, "git", *g, "commit", "-q", "-m", "base")
        self.app_origin = os.path.join(self.tmp, "app-origin.git")
        sh(self.tmp, "git", "clone", "-q", "--bare", seed, self.app_origin)
        self.main = os.path.join(self.tmp, "app")
        sh(self.tmp, "git", "clone", "-q", "--recurse-submodules", self.app_origin, self.main)
        for k, v in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
            sh(self.main, "git", "config", k, v)
        self.d1 = os.path.join(self.tmp, "wt-d1")
        sh(self.main, "git", "worktree", "add", "-q", "-b", "d1", self.d1, "main")
        sh(self.d1, "git", "submodule", "update", "--init", "-q", "lib")
        sh(os.path.join(self.d1, "lib"), "git", "fetch", "-q", self.producer, "refs/heads/feat")
        sh(os.path.join(self.d1, "lib"), "git", "checkout", "-q", self.head)
        sh(self.d1, "git", "add", "lib")
        sh(self.d1, "git", "commit", "-q", "-m", "D1 pin")
        self.d1_head = sh(self.d1, "git", "rev-parse", "HEAD")
        # D3: a fresh worktree from D1's verified head — lib not initialized
        self.d3 = os.path.join(self.tmp, "wt-d3")
        sh(self.main, "git", "worktree", "add", "-q", "-b", "d3", self.d3, self.d1_head)
        self.assertFalse(os.path.exists(os.path.join(self.d3, "lib", ".git")))
        self.repos = {"app": {"pin_path": "lib"}, "lib": {"path": self.producer}}
        self.driver = types.SimpleNamespace(
            t=types.SimpleNamespace(pin="F2", repo="app", chain_prev="D1"),
            design=types.SimpleNamespace(tickets={"F2": types.SimpleNamespace(branch="feat", repo="lib"),
                                                  "D1": types.SimpleNamespace(branch="d1", repo="app")}),
            cfg=types.SimpleNamespace(repo=lambda name: self.repos[name]),
            st={"worktree": self.d3},
            subject=lambda c: "[EX-3] chore(front-common): 핀 → F2 브랜치 헤드")
        self.states = {"F2": {"status": VERIFIED, "verified_head": self.head, "worktree": self.producer},
                       "D1": {"status": VERIFIED, "verified_head": self.d1_head, "worktree": self.d1}}

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp)

    def assert_pinned_clean(self):
        lib = os.path.join(self.d3, "lib")
        self.assertEqual(sh(lib, "git", "rev-parse", "HEAD"), self.head)
        self.assertEqual(sh(lib, "git", "status", "--porcelain"), "")
        self.assertEqual(sh(self.d3, "git", "status", "--porcelain", "--untracked-files=no"), "")
        self.assertEqual(sh(self.d3, "git", "rev-parse", "HEAD:lib"), self.head)

    def test_origin_lacks_the_gitlink(self):
        r = subprocess.run(["git", "-C", self.d3, "submodule", "update", "--init", "lib"], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)  # the reproduction: plain init fetches the sha from origin and fails

    def test_first_init_takes_the_pin_from_the_producer_worktree(self):
        c = {}
        TicketDriver.pin_box(self.driver, c, self.states)
        self.assert_pinned_clean()
        self.assertEqual(c["commit"], sh(self.d3, "git", "rev-parse", "HEAD"))

    def test_first_init_takes_the_pin_from_the_chain_prev_worktree(self):
        self.states["F2"]["worktree"] = os.path.join(self.tmp, "gone")  # producer worktree already removed
        self.repos["lib"] = {"path": os.path.join(self.tmp, "gone")}
        TicketDriver.pin_box(self.driver, {}, self.states)
        self.assert_pinned_clean()

    def test_first_init_takes_the_pin_from_the_producer_main_checkout(self):
        self.states["F2"]["worktree"] = None
        self.states["D1"]["worktree"] = None
        TicketDriver.pin_box(self.driver, {}, self.states)
        self.assert_pinned_clean()

    def test_no_source_has_the_pin(self):
        self.states["F2"]["worktree"] = None
        self.states["D1"]["worktree"] = None
        self.repos["lib"] = {"path": os.path.join(self.tmp, "gone")}
        with self.assertRaises(DriverError) as e:
            TicketDriver.pin_box(self.driver, {}, self.states)
        self.assertIn(self.head[:10], e.exception.message)

    def test_rebase_moves_the_gitlink_of_an_initialized_submodule(self):
        sh(self.main, "git", "submodule", "update", "--init", "-q", "lib")  # main: lib initialized at the base pin
        self.driver.st = {"worktree": self.main}
        sh(self.main, "git", "merge", "-q", "--ff-only", self.d1_head)  # the rebase: HEAD moves, lib stays behind
        self.assertNotEqual(sh(self.main, "git", "status", "--porcelain", "--untracked-files=no"), "")
        TicketDriver.sync_pin_checkout(self.driver, self.states)
        self.assertEqual(sh(os.path.join(self.main, "lib"), "git", "rev-parse", "HEAD"), self.head)
        self.assertEqual(sh(self.main, "git", "status", "--porcelain", "--untracked-files=no"), "")

    def test_rebase_leaves_an_uninitialized_submodule(self):
        TicketDriver.sync_pin_checkout(self.driver, self.states)
        self.assertFalse(os.path.exists(os.path.join(self.d3, "lib", ".git")))


if __name__ == "__main__":
    unittest.main()
