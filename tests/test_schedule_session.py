"""cbd <key> schedule refuses to start without a coordinator session (argument or state/scheduler.json)."""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cbd.cli import require_coordinator_session  # noqa: E402
from cbd.util import DriverError  # noqa: E402


class FakeStore:
    def __init__(self, saved):
        self.saved = saved

    def scheduler(self):
        return self.saved


def args(once=False, session=None):
    return types.SimpleNamespace(once=once, coordinator_session=session)


class ScheduleSessionTest(unittest.TestCase):
    def test_refuses_without_argument_or_saved_value(self):
        with self.assertRaises(DriverError) as e:
            require_coordinator_session(args(), FakeStore({"scheduler_terminal": "term_x"}))
        self.assertEqual(e.exception.kind, "환경")
        self.assertIn("--coordinator-session 필요", e.exception.message)

    def test_argument_is_enough(self):
        require_coordinator_session(args(session="6bdc"), FakeStore({}))

    def test_saved_value_is_enough_on_restart(self):
        require_coordinator_session(args(), FakeStore({"coordinator_session": "6bdc"}))

    def test_once_is_exempt(self):
        require_coordinator_session(args(once=True), FakeStore({}))


if __name__ == "__main__":
    unittest.main()
