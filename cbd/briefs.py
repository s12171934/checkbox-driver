"""Task specs (briefs) for the agents of one agent checkbox. They are files in the checkbox work folder
<work>/<PKG>/<NN>/ and every result comes back as a file there — worker_done carries only short lines.

The original briefs were lost; these restate the loop rules (티켓 체크박스 커밋 리뷰 루프 · 라운드 · 커밋과 지라 갱신)
in the form the driver checks afterwards: staged changes + commit-message.md after implementation, findings-<name>.md
per reviewer, verdict.md + exactly one new commit whose subject is the checkbox title after the judge. A verify box
that changed no code leaves no stage, only test-result.md + commit-message.md, and the driver commits it --allow-empty.

Two ticket-level briefs sit beside them (체크박스를 넘는 검증):
- integration review: after the design's last checkbox, every reviewer reads the whole branch once against the ticket's
  검증 목표 and writes <work>/<PKG>/integration/findings-<name>.md whose first line is 「판정: 지적 없음」 or 「판정: 지적 N」.
- cumulative check: after every commit, one GPT session checks base..<commit> (git objects only — the next checkbox is
  already changing the work tree) and writes <work>/<PKG>/cumulative/<NN>.md, first line 「판정: 문제 없음」 or 「판정: 문제 N」.
"""

import os
import re

COMMON = """\
## 공통 규칙

- 작업 위치는 워크트리 `{worktree}` (브랜치 `{branch}`) 하나다. 그 밖에는 이 체크박스 작업 폴더 `{work}` 에만 쓴다.
- 워크트리를 지우거나 옮기지 않는다. 다른 브랜치로 checkout·rebase·reset 하지 않는다.
- 테스트는 아래 명령으로만 돌린다(공유 DB·스택을 직접 띄우지 않는다): {test_hint}
- 셸 명령은 Claude Code 가 그대로 해석할 수 있게 단순하게 쓴다: zsh 배열·`${{(f)…}}`·중첩 `$(…)` 확장 금지. 파일 목록은 파일에 쓰고 `xargs` 로 넘기거나 테스트 러너의 경로·패턴 인자를 쓴다.
- 오래 걸리는 대기 루프를 만들지 않는다. 막히면 질문(question)으로 묻고 답을 기다린다.
- 끝나면 Orca 가 알려 준 방식으로 `worker_done` 을 보낸다. 본문은 3줄 이하, 산출물은 파일 경로로 적는다.
"""


def _test_hint(cfg, ticket):
    rc = cfg["repos"].get(ticket.repo or "", {})
    cmd = rc.get("test_cmd")
    hint = f"`{' '.join(cmd)}` (`{{tcs}}` 자리에 TC ID)" if cmd else "저장소의 표준 테스트 명령"
    if rc.get("typecheck_cmd"):
        hint += f", 타입 검사 `{' '.join(rc['typecheck_cmd'])}`"
    return hint


def goals_text(goals):
    return "\n".join(f"{i}. {g}" for i, g in enumerate(goals, 1)) if goals else \
        "(이 티켓에는 검증 목표가 적혀 있지 않다 — 아래 기본 관점만 본다)"


def integration_path(work, name):
    return os.path.join(work, f"findings-{name}.md")


FIGMA_URL = re.compile(r"https://(?:www\.)?figma\.com/[^\s)>\]`|]+")


def ui_sources(cfg, design, ticket, goals):
    """(snapshot dirs that exist, Figma links named in the ticket's design text and goals)."""
    dirs = [os.path.join(design.root, d.format(pkg=ticket.package)) for d in cfg.get("ui_snapshots") or []]
    links = list(dict.fromkeys(FIGMA_URL.findall("\n".join([ticket.index_section or "", *goals]))))
    return [d for d in dirs if os.path.isdir(d)], links


def ui_compare(cfg, design, ticket, worktree, work, base, goals):
    """The Storybook ↔ Figma comparison a codex integration reviewer runs on a UI ticket: snapshots first, the Figma
    file itself (Figma MCP) only when the design has no snapshot for the ticket."""
    snaps, links = ui_sources(cfg, design, ticket, goals)
    rc = cfg["repos"].get(ticket.repo or "", {})
    sb = f"`{' '.join(rc['storybook_cmd'])}`" if rc.get("storybook_cmd") else "저장소의 표준 Storybook 개발 서버 명령"
    out = os.path.join(work, "ui")
    if snaps:
        src = "디자인 스냅숏(정본) — " + " · ".join(f"`{d}`" for d in snaps) + \
              "\n- 스냅숏에 없는 화면·상태만 아래 Figma 링크를 Figma MCP 로 직접 조회한다."
    else:
        src = "디자인 스냅숏이 없다 — Figma MCP 로 프레임을 직접 조회한다(`get_screenshot` 으로 그림, `get_design_context`" \
              "·`get_variable_defs` 로 치수·색·타이포 값)."
    fl = "\n".join(f"- {u}" for u in links) or "- (티켓 설계에 Figma 링크가 없다 — 스냅숏이 없으면 이 대조는 「판단 불가」 지적이다)"
    return f"""
## UI 대조 — Storybook ↔ Figma (codex 리뷰어 몫, UI 티켓)

이 티켓은 화면을 만든다. 코드 리뷰에 더해 **바뀐 화면을 디자인과 하나씩 상세 대조**한다.

- 디자인 기준: {src}
- Figma 링크:

{fl}

1. `git diff --name-only {base}..HEAD` 에서 바뀐 컴포넌트와 그 스토리(`*.stories.*`)를 고른다. 바뀐 컴포넌트에 스토리가
   없으면 그것도 지적이다.
2. Storybook 을 이 워크트리에서 {sb} 로 **비어 있는 포트**에 띄운다(다른 티켓·E2E 와 포트를 나눠 쓴다). 끝나면 반드시 끈다.
3. 스토리마다(상태·변형·뷰포트별) 캡처를 `{out}/` 에 남기고 대응 프레임과 대조한다. 대조 항목: 크기·여백·간격, 색(토큰 이름과
   값), 타이포(글꼴·크기·굵기·줄 높이), 모서리·그림자·테두리, 아이콘·이미지, 문구, 상태(hover·focus·disabled·error·empty·loading),
   반응형 줄바꿈.
4. findings 파일에 `## UI 대조` 표를 더한다 — `| 스토리 | 프레임(링크·노드 id 또는 스냅숏 파일) | 캡처 | 항목 | 디자인 값 | 구현 값 | 판정 |`.
   판정은 `일치 · 불일치 · 판단 불가`. 「불일치」·「판단 불가」 행은 지적으로 센다(위치는 `파일:줄`, 고칠 방향 포함).
5. 시안끼리 모순이라 고칠 수 없는 차이는 지적으로 남기되 「사람 판정 필요」라고 적는다 — 리뷰어가 스스로 수용하지 않는다.
"""


def integration_review(cfg, design, ticket, worktree, work, name, base, goals):
    rc = {"pkg": ticket.package, "jira": ticket.jira or "(Jira 없음)", "worktree": worktree, "branch": ticket.branch,
          "work": work, "test_hint": _test_hint(cfg, ticket)}
    out = integration_path(work, name)
    section = ticket.index_section or "(ticket-index 절 없음)"
    agent = next((r["agent"] for r in cfg["reviewers"] if r["name"] == name), name)
    ui = ui_compare(cfg, design, ticket, worktree, work, base, goals) \
        if agent == "codex" and cfg.is_ui(ticket.package) else ""
    return f"""# {rc['pkg']} 티켓 통합 리뷰 ({name})

티켓 {rc['pkg']} ({rc['jira']}) 의 체크박스가 모두 커밋됐다. 체크박스 리뷰는 커밋 하나의 스테이지만 봤으므로, 이번에는
**브랜치 전체**를 이 티켓의 검증 목표에 비추어 한 번 본다. 코드를 고치지 않고 커밋하지 않는다(읽기 전용, 1 라운드).

## 입력

- 변경 전체: 워크트리 `{rc['worktree']}` 에서 `git diff {base}..HEAD`, 커밋 목록 `git log --oneline {base}..HEAD`
- 설계 루트 `{design.root}` 의 읽을 문서:

{section}

## 검증 목표

{goals_text(goals)}
{ui}
## 관점 (체크박스 리뷰가 볼 수 없던 것만)

- 검증 목표마다 브랜치가 그 목표를 이루는지 — 근거(`파일:줄`, 테스트 이름)를 들어 「충족 · 미충족 · 판단 불가」로 판정한다.
- 같은 상태·판정의 정본이 여러 곳에 생겼는지, 같은 동작의 경로가 둘 이상인데 일부만 처리됐는지.
- 커밋 사이 계약(타입 · 필수 prop · 직렬화 필드 · 에러 코드)의 어긋남.
- 비기능 상한(입력 크기 · 소요 시간 · 락 안 작업), 설계 범위 밖 경로로 새는 변화.
- 테스트가 단언하지 않는 동작, 체크박스 문구와 다른 구현.

## 할 일

1. `{out}` 의 **첫 줄**을 정확히 `판정: 지적 없음` 또는 `판정: 지적 <개수>` 로 쓴다. 「미충족」·「판단 불가」 목표도 지적 하나로 센다.
2. 그 아래 `## 검증 목표` 절에 목표마다 한 줄(번호 · 판정 · 근거), `## 지적` 절에 지적마다 `### <번호>. <한 줄 요약>`
   — 심각도(차단·중요·사소), 위치(`파일:줄`), 근거, 고칠 방향을 쓴다.
3. 테스트는 아래 명령으로 돌려 확인할 수 있다. 스테이지·작업 트리를 바꾸지 않는다.
4. `worker_done` — findings 경로와 지적 수.

{COMMON.format(**rc)}
"""


def cumulative_path(work, n):
    return os.path.join(work, f"{n:02d}.md")


def cumulative(cfg, design, ticket, worktree, work, n, title, base, commit, goals):
    rc = {"pkg": ticket.package, "jira": ticket.jira or "(Jira 없음)", "worktree": worktree, "branch": ticket.branch,
          "work": work, "test_hint": "이 검증에서는 테스트를 돌리지 않는다"}
    out = cumulative_path(work, n)
    left = "\n".join(f"- {b.index}. `{b.title}`" for b in ticket.boxes if b.index > n) or "- (없음 — 마지막 체크박스)"
    return f"""# {rc['pkg']}#{n} 누적 통합 검증 — {title}

티켓 {rc['pkg']} ({rc['jira']}) 의 {n}번 체크박스가 커밋됐다(`{commit[:12]}`). **지금까지 커밋된 구현 전체**가 서로 맞물려
동작하는지, 검증 목표로 가는 길에서 벗어나지 않았는지를 본다. 체크박스 진행과 따로 도는 검증이며 결과는 코디네이터에게 간다.

## 읽는 법 — git 객체만

- 다음 체크박스의 구현이 이미 같은 워크트리 `{rc['worktree']}` 의 작업 트리를 바꾸고 있다. **작업 트리 파일을 읽지 말고**
  `git diff {base}..{commit}`, `git log --oneline {base}..{commit}`, `git show {commit}:<경로>` 로만 읽는다.
- 코드를 고치지 않고, 스테이지·작업 트리·브랜치를 건드리지 않고, 테스트를 돌리지 않는다.
- 설계 루트 `{design.root}` 의 읽을 문서:

{ticket.index_section or "(ticket-index 절 없음)"}

## 검증 목표

{goals_text(goals)}

## 아직 커밋되지 않은 체크박스 (이것이 할 일은 문제로 세지 않는다)

{left}

## 관점

- 지금까지의 커밋끼리 계약(타입 · 필수 prop · 직렬화 필드 · 에러 코드 · 호출 순서)이 맞물리는지.
- 같은 상태·판정의 정본이 둘 이상 생겼는지, 같은 동작의 경로 중 일부만 처리됐는지.
- 이미 커밋된 부분이 검증 목표와 어긋나 남은 체크박스로는 바로잡을 수 없게 됐는지.
- 앞 커밋이 만든 동작을 뒤 커밋이 깨뜨렸는지(테스트 없이 코드로 짚는다).

## 할 일

1. `{out}` 의 **첫 줄**을 정확히 `판정: 문제 없음` 또는 `판정: 문제 <개수>` 로 쓴다.
2. 문제마다 `### <번호>. <한 줄 요약>` — 심각도(차단·중요·사소), 위치(`<커밋>:<파일>:<줄>`), 근거, 어느 체크박스에서
   다뤄야 하는지(남은 체크박스 번호 또는 「수정 체크박스 필요」).
3. `worker_done` — 결과 경로와 문제 수.

{COMMON.format(**rc)}
"""


def fix_extra(integration_files, goals, review=False):
    fs = "\n".join(f"- `{f}`" for f in integration_files)
    what = ("스테이지된 변경이 아래 통합 리뷰 findings 의 지적(「미충족」·「판단 불가」 검증 목표 포함)을 모두 고쳤는지도 본다."
            " 빠진 지적은 결함으로 적는다." if review else
            "아래 통합 리뷰 findings 의 지적(「미충족」·「판단 불가」 검증 목표 포함)을 모두 이 커밋 하나로 고친다. 기존 커밋을"
            " 고쳐 쓰지 않는다(fixup·rebase 금지) — 수정은 이 체크박스의 새 커밋이다.")
    return f"""
## 이 체크박스는 티켓 통합 리뷰 지적의 수정이다

{what}

{fs}

검증 목표:

{goals_text(goals)}
"""


def paths(work):
    return {k: os.path.join(work, f) for k, f in {
        "impl": "impl-brief.md", "draft": "commit-message.md", "test": "test-result.md", "tc_rows": "tc-rows.md",
        "verdict": "verdict.md", "judge": "judge-brief.md", "note": "commit-note-path.txt"}.items()}


def findings_path(work, name):
    return os.path.join(work, f"findings-{name}.md")


def context(cfg, design, ticket, box, worktree, work):
    others = "\n".join(f"{'→' if b.index == box.index else ' '} {b.index}. `{b.title}`" for b in ticket.boxes) or "-"
    return {
        "pkg": ticket.package, "n": box.index, "title": box.title, "jira": ticket.jira or "(Jira 없음)",
        "worktree": worktree, "branch": ticket.branch, "work": work, "design": design.root,
        "section": ticket.index_section or "(ticket-index 절 없음)", "others": others,
        "test_hint": _test_hint(cfg, ticket), "subject": f"[{ticket.jira}] {box.title}" if ticket.jira else box.title,
        "records": (cfg.get("vault") or {}).get("records_dir") or "",
    }


def impl(cfg, design, ticket, box, worktree, work, tc_rows="", extra=""):
    c = context(cfg, design, ticket, box, worktree, work)
    p = paths(work)
    tc = f"\n- 이 체크박스의 TC 행 원문: `{p['tc_rows']}`" if tc_rows else ""
    verify = (f"\n\n   **verify 체크박스에서 코드 변경이 없으면 스테이지 없이 끝낸다** — 테스트 결과와 초안만 남기면 리뷰·판정 없이"
              f" 드라이버가 초안으로 `git commit --allow-empty` 한다. 시각 대조 산출물 등 파일을 바꿨으면 위처럼 스테이지한다."
              if box.prefix == "verify" else "")
    return f"""# {c['pkg']}#{c['n']} 구현 — {c['title']}

티켓 {c['pkg']} ({c['jira']}) 의 체크박스 **{c['n']}번 하나**를 구현한다. 커밋 하나 = 체크박스 하나다.

## 범위

- 체크박스: `{c['title']}`
- 설계 루트: `{c['design']}` — 아래 「읽을 문서」 밖은 열지 않는다. 의존 노드는 첫 10줄만.{tc}
- 이 티켓의 체크박스 순서 (→ 가 이번 것, 앞 번호는 이미 커밋됨, 뒤 번호는 하지 않는다):

{c['others']}

## 읽을 문서 (ticket-index)

{c['section']}

## 할 일

1. 이 체크박스 범위만 구현한다. 다음 체크박스의 일을 당겨 하지 않는다. 테스트 커밋이면 테스트만 쓰고 빨강을 확인한다.
2. 변경을 `git add` 로 **스테이지에 올리고 커밋하지 않는다.** 리뷰는 스테이지 상태로 받는다.{verify}
3. 커밋 메시지 초안을 `{p['draft']}` 에 쓴다. 첫 줄은 정확히 `{c['subject']}` 이다. 본문에 바꾼 것과 검증한 것을 적는다.
4. 관련 테스트를 돌리고 결과(통과·실패 수, 실패 이름)를 `{p['test']}` 에 적는다.
5. `worker_done` — 스테이지 파일 수 · 초안 경로 · 테스트 결과 한 줄.

{extra}
{COMMON.format(**c)}
- 이 터미널은 리뷰 뒤 판정에 다시 쓰인다. 끝나도 닫지 않는다.
"""


def review(cfg, design, ticket, box, worktree, work, name, extra=""):
    c = context(cfg, design, ticket, box, worktree, work)
    p = paths(work)
    out = findings_path(work, name)
    return f"""# {c['pkg']}#{c['n']} 리뷰 ({name}) — {c['title']}

티켓 {c['pkg']} 체크박스 {c['n']}번의 **스테이지된 변경**을 리뷰한다. 코드를 고치지 않는다(읽기 전용).

## 입력

- 변경: 워크트리 `{c['worktree']}` 에서 `git diff --cached`
- 커밋 메시지 초안: `{p['draft']}`
- 체크박스: `{c['title']}` — 이 범위를 넘는 구현, 빠진 구현을 둘 다 본다.
- 테스트 결과: `{p['test']}`{f"{chr(10)}- TC 행 원문: `{p['tc_rows']}`" if os.path.exists(p['tc_rows']) else ''}
- 설계 루트 `{c['design']}` 의 읽을 문서:

{c['section']}

## 할 일

1. 결함마다 `{out}` 에 `### <번호>. <한 줄 요약>` 절을 쓴다 — 심각도(차단·중요·사소), 위치(`파일:줄`), 근거, 고칠 방향.
2. 결함이 없으면 `없음` 한 줄만 쓴다. 파일은 반드시 만든다.
3. 테스트는 위 명령으로 돌려 확인할 수 있다. 스테이지·작업 트리를 바꾸지 않는다.
4. `worker_done` — findings 경로와 결함 수.
{extra}
{COMMON.format(**c)}
"""


def judge(cfg, design, ticket, box, worktree, work):
    c = context(cfg, design, ticket, box, worktree, work)
    p = paths(work)
    fs = "\n".join(f"- `{findings_path(work, r['name'])}`" for r in cfg["reviewers"])
    note = (f"\n5. 볼트 `{c['records']}` 에 커밋 정리 노트를 쓴다 — 프론트매터(title·created·updated·tags·source: ai),"
            f" 체크박스·커밋 해시·바꾼 것·리뷰 판정 요약·남은 기각. 경로를 `{p['note']}` 에 한 줄로 적는다.") if c["records"] else ""
    return f"""# {c['pkg']}#{c['n']} 판정 — {c['title']}

네가 구현한 체크박스 {c['n']}번의 리뷰가 끝났다. 지적을 판정하고 반영해 **커밋 하나**로 마무리한다.

## 입력

{fs}

## 할 일

1. 지적마다 승인 또는 기각을 정하고 근거를 `{p['verdict']}` 에 쓴다 (`### <리뷰어> <번호>` · 판정 · 근거).
2. 승인한 지적을 반영하고 다시 스테이지한다. 테스트를 다시 돌려 `{p['test']}` 를 갱신한다.
3. 앞 테스트 커밋의 결함이면 `git commit --fixup` 뒤 같은 브랜치 안에서만 autosquash 한다.
4. 커밋한다. 제목은 정확히 `{c['subject']}`, 본문은 `{p['draft']}` 를 고쳐 쓴다. 작업 트리를 깨끗하게 남긴다.{note}
6. `worker_done` — 커밋 해시 · 판정문 경로 · 기각 수.

{COMMON.format(**c)}
"""


def tc_rows_text(design, tcs):
    rows = design.tc_rows()
    return "\n".join(rows[tc]["row"] for tc in tcs if tc in rows)
