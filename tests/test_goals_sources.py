"""검증 목표 sources other than the checklist's 「검증 목표:」 list: a 「티켓 · 검증 목표」 table in a single design.md,
and the inline 「검증 목표: <text>」 paragraph of a Jira description."""

import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cbd.design import Design  # noqa: E402
from cbd.jira import goals_of  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "single_doc")
TC = r"[RFDPX]-\d{2}"

GOAL_TABLE = """
### 9.4 티켓별 검증 목표

| 티켓 | 검증 목표 |
| --- | --- |
| [EX-11](https://example.atlassian.net/browse/EX-11) R1 | 한도 설정이 저장되고 `limit` 밖 값은 400 이다. |
| R2 | — |
"""


class GoalTableTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        shutil.copy(os.path.join(FIXTURE, "design.md"), self.tmp)
        with open(os.path.join(self.tmp, "design.md"), "a", encoding="utf-8") as f:
            f.write(GOAL_TABLE)
        self.design = Design(self.tmp, TC)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_row_with_jira_key_gives_the_package_its_goal(self):
        self.assertEqual(self.design.tickets["R1"].goals, ["한도 설정이 저장되고 limit 밖 값은 400 이다."])

    def test_dash_cell_is_no_goal(self):
        self.assertEqual(self.design.tickets["R2"].goals, [])
        self.assertEqual(self.design.warnings, [])


def para(*texts):
    return {"type": "paragraph", "content": [{"type": "text", "text": t} for t in texts]}


class JiraInlineGoalTest(unittest.TestCase):
    def test_inline_paragraph_is_one_goal_and_checklist_is_not(self):
        desc = {"type": "doc", "content": [
            para("설계 문서: ", "https://example/pr/1"),
            para("검증 목표: ", "브랜치가 한도를 지킨다. R-01 green."),
            para("대상 테스트: R-01"),
            {"type": "taskList", "content": [{"type": "taskItem", "content": [{"type": "text", "text": "feat: 한도"}]}]},
        ]}
        self.assertEqual(goals_of(desc), ["브랜치가 한도를 지킨다. R-01 green."])

    def test_label_paragraph_still_reads_the_list_under_it(self):
        desc = {"type": "doc", "content": [
            para("검증 목표"),
            {"type": "bulletList", "content": [{"type": "listItem", "content": [para("목표 1")]},
                                               {"type": "listItem", "content": [para("목표 2")]}]},
            {"type": "heading", "content": [{"type": "text", "text": "체크리스트"}]},
            para("무관"),
        ]}
        self.assertEqual(goals_of(desc), ["목표 1", "목표 2"])


if __name__ == "__main__":
    unittest.main()
