"""The stall (정체) timer does not count the wait for a worker question's answer (PRD-94 W1#1: 6h question → stall)."""

import datetime
import json
import os
import shutil
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
from cbd.ticket import TicketDriver, question_wait  # noqa: E402
from cbd.util import DriverError  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "design")
T0 = datetime.datetime(2026, 11, 26, 17, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=9))).timestamp()
H = 3600


def iso(ts):
    return datetime.datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


class QuestionWaitTest(unittest.TestCase):
    def test_answered_question_is_taken_out(self):
        qs = [{"at": iso(T0 + 600), "answered": iso(T0 + 6 * H)}]
        self.assertEqual(question_wait(qs, T0, T0 + 7 * H), 6 * H - 600)

    def test_open_question_counts_up_to_now(self):
        self.assertEqual(question_wait([{"at": iso(T0 + H)}], T0, T0 + 3 * H), 2 * H)

    def test_question_before_the_timer_started_is_clipped(self):
        qs = [{"at": iso(T0 - 5 * H), "answered": iso(T0 + H)}]
        self.assertEqual(question_wait(qs, T0, T0 + 2 * H), H)

    def test_overlapping_questions_count_once(self):
        qs = [{"at": iso(T0), "answered": iso(T0 + 2 * H)}, {"at": iso(T0 + H), "answered": iso(T0 + 3 * H)}]
        self.assertEqual(question_wait(qs, T0, T0 + 4 * H), 3 * H)

    def test_no_or_broken_records(self):
        self.assertEqual(question_wait(None, T0, T0 + H), 0)
        self.assertEqual(question_wait([{"at": "?"}, {}], T0, T0 + H), 0)


class WaitTaskQuietTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.home, "ex"))
        with open(os.path.join(self.home, "ex", "config.json"), "w") as f:
            json.dump({"project": "EX", "design_root": FIXTURE, "notify": False, "usage": {"commands": {}},
                       "repos": {"core": {"path": self.home}}}, f)
        self.cfg = Config("ex", self.home)
        self.store = Store(self.cfg)
        self.design = Design(FIXTURE)

    def tearDown(self):
        shutil.rmtree(self.home)

    def wait(self, questions, clock):
        """wait_task over a task that is still running at the first look and done at the second one."""
        self.store.save({"package": "A1", "status": "running", "run_id": "run_1", "checkboxes": [],
                         "questions": questions})
        driver = TicketDriver(self.cfg, self.design, self.store, "A1")
        c = {"index": 1, "tasks": {"impl": "task_1"}, "dispatch": {}}
        looks = iter([{"task_1": {"status": "running"}}, {"task_1": {"status": "completed"}}])
        with mock.patch.object(orca, "task_list", side_effect=lambda rid: next(looks)), \
                mock.patch.object(orca, "check_wait", return_value=[]), \
                mock.patch("cbd.ticket.now_ts", side_effect=clock):
            driver.wait_task(c, "impl")
        return c

    def test_answer_after_a_long_question_does_not_stall(self):
        # started at T0, asked at +10m, answered at +6h; the first look after the answer is at +6h05m: 15 quiet minutes
        qs = [{"msg_id": "m1", "path": "-", "at": iso(T0 + 600), "answered": iso(T0 + 6 * H)}]
        c = self.wait(qs, iter([T0, T0 + 6 * H + 300, T0 + 6 * H + 300]).__next__)
        self.assertIn("impl", c["done"])

    def test_quiet_time_outside_questions_still_stalls(self):
        # the same question, but 2h10m of quiet besides it: 정체
        qs = [{"msg_id": "m1", "path": "-", "at": iso(T0 + 600), "answered": iso(T0 + 6 * H)}]
        with self.assertRaises(DriverError) as e:
            self.wait(qs, iter([T0, T0 + 8 * H + 600, T0 + 8 * H + 600]).__next__)
        self.assertEqual(e.exception.kind, "정체")


if __name__ == "__main__":
    unittest.main()
