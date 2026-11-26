"""Single-doc briefs name the design.md parts a ticket's text cites (PRD-94 F3#1 stopped asking to read §11.2 F3 · §13).
Multi-doc briefs stay as they were."""

import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from cbd import briefs  # noqa: E402
from cbd.config import Config  # noqa: E402
from cbd.design import Design  # noqa: E402

MULTI = os.path.join(ROOT, "tests", "fixtures", "design")
TC = r"T[A-Z]-\d{2}"

DOC = """\
# [EX-94] 예시 — single-doc

## 5. 핵심 파이프라인

### 5.2 `Assign` 서비스 (신규)

```ruby
# 9. 코드 안의 주석은 머리글이 아니다
```

## 11. 클라이언트 트랙

### 11.2 front-common (presentational only)

**F3 `Consent`** 계약.

## 13. Storybook 컴포넌트 인벤토리

| 부품 | 스토리 | 대표 노드 |
| --- | --- | --- |
| `Visibility` (F1) | Default | `1:1` |
| `Consent` · `ConsentCard` (F3) | No · Yes | `1:2` |
| `RAInputStep` (F3 슬롯) | Passage · Listening(F1) | `1:3` |
| `Guide` (F30) | Default | `1:4` |

## 14. 테스트 케이스 매트릭스

### 14.1 서버 — rspec

| ID | 요구 | 패키지 |
| --- | --- | --- |
| TR-01 | 배정 | R1 |

### 14.2 front-common — Storybook `play`

| ID | 요구 | 패키지 |
| --- | --- | --- |
| TF-01 | 열람 | F1 |
| TF-20 | 블록 | F3 |
| TF-21 | 에러 | F3 |

## 15. 구현 순서

| 단계 | 패키지 | 선행 | 테스트 |
| --- | --- | --- | --- |
| 1 | R1 서버 | — | TR-01 |
| 1 | F1 열람 | — | TF-01 |
| 2 | F3 교과서 블록 | F1 | TF-20~TF-21 |

## 16. Jira 티켓 맵

| 티켓 | 저장소 | 브랜치 | 베이스/스택 부모 | 대상 테스트 | is blocked by |
| --- | --- | --- | --- | --- | --- |
| EX-1 R1 서버 | `core` | `EX-1-r1` | `develop` | TR-01 | — |
| EX-2 F1 열람 | `fc` | `EX-2-f1` | `main` | TF-01 | — |
| EX-3 F3 교과서 블록 | `fc` | `EX-3-f3` | F1 | TF-20~TF-21 | — |

### 16.2 커밋 체크리스트

R1 — EX-1 · `EX-1-r1`

```
[ ] test(core): TR-01 빨강
[ ] feat(core): Assign 서비스 (§5.2)
[ ] 완료
```

F1 — EX-2 · `EX-2-f1`

```
[ ] feat(fc): Visibility (§11.2 F1)
[ ] 완료
```

F3 — EX-3 · `EX-3-f3`

검증 목표:

- 블록이 시안대로 (TF-20)

```
[ ] test(create): TF-20~TF-21 빨강
[ ] feat(create): Consent (§11.2 F3)
[ ] verify: TF-20~TF-21 그린 + Storybook↔Figma 대조(§13) + §99 없는 절
[ ] 완료
```
"""


def config(home, root):
    os.makedirs(os.path.join(home, "ex"), exist_ok=True)
    with open(os.path.join(home, "ex", "config.json"), "w") as f:
        json.dump({"project": "EX", "design_root": root, "notify": False, "usage": {"commands": {}},
                   "repos": {"core": {"path": home}, "fc": {"path": home}}}, f)
    return Config("ex", home)


class ReadAllowanceTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.root = os.path.join(self.home, "design")
        os.makedirs(self.root)
        with open(os.path.join(self.root, "design.md"), "w", encoding="utf-8") as f:
            f.write(DOC)
        self.design = Design(self.root, TC)
        self.cfg = config(self.home, self.root)
        self.f3 = self.design.tickets["F3"]

    def tearDown(self):
        shutil.rmtree(self.home)

    def test_cited_sections_tc_rows_and_inventory_rows(self):
        allow = self.design.read_allowance(self.f3)
        self.assertEqual(allow, [
            "「11.2 front-common (presentational only)」 중 F3 부분 (§11.2 F3)",
            "「13. Storybook 컴포넌트 인벤토리」 (§13)",
            "§99 — design.md 에 이 번호의 머리글이 없다",
            "「11.2 front-common (presentational only)」 의 **F3 `Consent`** 블록 (:15 부터 다음 굵은 패키지 블록·머리글 전까지)",
            "「14.2 front-common — Storybook `play`」 표의 TF-20 · TF-21 행 (테스트 케이스)",
            "「13. Storybook 컴포넌트 인벤토리」 표의 F3 행 — `Consent` · `ConsentCard` (F3) / `RAInputStep` (F3 슬롯)",
        ])

    def test_other_tickets_get_their_own_parts_only(self):
        self.assertEqual(self.design.read_allowance(self.design.tickets["R1"]), [
            "「5.2 `Assign` 서비스 (신규)」 (§5.2)",
            "「14.1 서버 — rspec」 표의 TR-01 행 (테스트 케이스)",
        ])
        f1 = self.design.read_allowance(self.design.tickets["F1"])
        self.assertIn("「13. Storybook 컴포넌트 인벤토리」 표의 F1 행 — `Visibility` (F1)", f1)  # not Listening(F1)

    def test_box_title_outside_the_checklist_adds_its_section(self):
        allow = self.design.read_allowance(self.f3, ["fix: 통합 리뷰 지적 (§5.2)"])
        self.assertIn("「5.2 `Assign` 서비스 (신규)」 (§5.2)", allow)

    def test_headings_inside_code_fences_are_skipped(self):
        self.assertNotIn("9", self.design.numbered_headings())

    def test_every_brief_with_the_design_section_carries_the_allowance(self):
        wt, work, box = self.home, os.path.join(self.home, "work"), self.f3.boxes[0]
        texts = {
            "impl": briefs.impl(self.cfg, self.design, self.f3, box, wt, work),
            "review": briefs.review(self.cfg, self.design, self.f3, box, wt, work, "codex"),
            "integration": briefs.integration_review(self.cfg, self.design, self.f3, wt, work, "codex", "main", []),
            "cumulative": briefs.cumulative(self.cfg, self.design, self.f3, wt, work, 1, box.title, "main", "abc", []),
            "contract": briefs.contract(self.cfg, self.design, self.f3, wt, work, {"apis": "M1"}, []),
        }
        for kind, text in texts.items():
            self.assertIn("읽어도 되는 설계 절", text, kind)
            self.assertIn(os.path.join(self.root, "design.md"), text, kind)
            self.assertIn("「11.2 front-common (presentational only)」 중 F3 부분", text, kind)
            self.assertNotIn("**F3 `Consent`** 계약", text, kind)  # names only, the section text is not pasted


TRACK_DOC = """\
# [EX-95] 예시 — 트랙 블록

## 10. 서버 트랙 (`core`)

| 파일 | 변경 |
| --- | --- |
| `app/x.rb` | 신규 |

## 11. 클라이언트 트랙

### 11.2 front-common (presentational only)

fc 부품은 상태를 갖지 않는다. 문구의 정본은 §13 이다.

**F1 `Visibility`** (신규)

```ts
# 주석이지 머리글이 아니다
export interface VisibilityProps {}
```

**F4 활동 정보·학생 준비 팝업** (수정·신규)

```ts
// **F9 이건 코드 안 — 블록 끝이 아니다
export interface GuideProps {}
```

- F4 본문 줄

**F40 다른 패키지** (신규)

### 11.3 대시보드 (`dash`)

**D1 세션 보내기** — `Dialog.tsx`

- D1 본문 (**D0** 은 줄 첫머리가 아니다)

**D2 관찰** — `Observe.tsx`

## 13. Storybook 컴포넌트 인벤토리

| 부품 | 스토리 | 대표 노드 |
| --- | --- | --- |
| `Guide` (F4) | Default | `1:1` |

## 16. Jira 티켓 맵

| 티켓 | 저장소 | 브랜치 | 베이스/스택 부모 | 대상 테스트 | is blocked by |
| --- | --- | --- | --- | --- | --- |
| EX-1 F1 열람 | `fc` | `EX-1-f1` | `main` | — | — |
| EX-2 F4 활동 정보 | `fc` | `EX-2-f4` | F1 | — | — |
| EX-3 D1 세션 보내기 | `dash` | `EX-3-d1` | `develop` | — | — |
| EX-4 R1 서버 | `core` | `EX-4-r1` | `develop` | — | — |

### 16.2 커밋 체크리스트

F1 — EX-1 · `EX-1-f1`

```
[ ] feat(fc): Visibility
[ ] 완료
```

F4 — EX-2 · `EX-2-f4`

```
[ ] feat(fc): 활동 정보 Guide
[ ] verify: Storybook↔Figma 대조(§13)
[ ] 완료
```

D1 — EX-3 · `EX-3-d1`

```
[ ] feat(dash): 세션 보내기
[ ] 완료
```

R1 — EX-4 · `EX-4-r1`

```
[ ] feat(core): 서버
[ ] 완료
```
"""


class TrackBlockTest(unittest.TestCase):
    """PRD-94 F4#1: the F4 checklist cites only §13, so the brief allowed §13 and the worker stopped to ask whether it
    may read the F4 part of §11.2. The ticket's bold track block (and its section's common first paragraph) is allowed."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.root = os.path.join(self.home, "design")
        os.makedirs(self.root)
        with open(os.path.join(self.root, "design.md"), "w", encoding="utf-8") as f:
            f.write(TRACK_DOC)
        self.design = Design(self.root, TC)
        self.cfg = config(self.home, self.root)

    def tearDown(self):
        shutil.rmtree(self.home)

    def line(self, text):
        return TRACK_DOC.splitlines().index(text) + 1

    def test_block_and_common_paragraph_are_allowed(self):
        allow = self.design.read_allowance(self.design.tickets["F4"])
        head = "「11.2 front-common (presentational only)」"
        self.assertEqual(allow, [
            "「13. Storybook 컴포넌트 인벤토리」 (§13)",
            f"{head} 의 **F4 활동 정보·학생 준비 팝업** 블록 (:{self.line('**F4 활동 정보·학생 준비 팝업** (수정·신규)')}"
            " 부터 다음 굵은 패키지 블록·머리글 전까지)",
            f"{head} 첫 문단 (:{self.line('fc 부품은 상태를 갖지 않는다. 문구의 정본은 §13 이다.')} — 이 절 패키지 블록의 공통 규칙)",
            "「13. Storybook 컴포넌트 인벤토리」 표의 F4 행 — `Guide` (F4)",
        ])

    def test_block_bounds(self):
        blocks = self.design.track_blocks()
        f4 = blocks["F4"][0]
        start = self.line("**F4 활동 정보·학생 준비 팝업** (수정·신규)")
        self.assertEqual((f4["line"], f4["end"]), (start, self.line("- F4 본문 줄")))  # trailing blank lines dropped
        d1 = blocks["D1"][0]
        self.assertEqual(d1["end"], self.line("- D1 본문 (**D0** 은 줄 첫머리가 아니다)"))
        self.assertNotIn("D0", blocks)       # inline bold, not a block
        self.assertNotIn("F9", blocks)       # inside a code fence
        self.assertEqual(blocks["F40"][0]["end"], self.line("**F40 다른 패키지** (신규)"))

    def test_section_without_common_paragraph_adds_only_the_block(self):
        allow = self.design.read_allowance(self.design.tickets["D1"])
        self.assertEqual(allow, [
            f"「11.3 대시보드 (`dash`)」 의 **D1 세션 보내기** 블록 (:{self.line('**D1 세션 보내기** — `Dialog.tsx`')}"
            " 부터 다음 굵은 패키지 블록·머리글 전까지)",
        ])

    def test_ticket_without_block_is_unchanged(self):
        self.assertEqual(self.design.read_allowance(self.design.tickets["R1"]), [])

    def test_impl_brief_names_the_block_without_its_text(self):
        f4 = self.design.tickets["F4"]
        text = briefs.impl(self.cfg, self.design, f4, f4.boxes[0], self.home, os.path.join(self.home, "work"))
        self.assertIn("**F4 활동 정보·학생 준비 팝업** 블록", text)
        self.assertIn("첫 문단", text)
        self.assertNotIn("F4 본문 줄", text)


class MultiDocUnchangedTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.design = Design(MULTI)
        self.cfg = config(self.home, MULTI)

    def tearDown(self):
        shutil.rmtree(self.home)

    def test_reading_is_the_ticket_index_section(self):
        for t in self.design.tickets.values():
            self.assertEqual(self.design.read_allowance(t), [])
            self.assertEqual(briefs.reading(self.design, t, [b.title for b in t.boxes]),
                             t.index_section or "(ticket-index 절 없음)")

    def test_impl_brief_has_no_allowance(self):
        t = self.design.tickets["A1"]
        text = briefs.impl(self.cfg, self.design, t, t.boxes[0], self.home, os.path.join(self.home, "w"))
        self.assertNotIn("읽어도 되는 설계 절", text)
        self.assertIn(t.index_section, text)


if __name__ == "__main__":
    unittest.main()
