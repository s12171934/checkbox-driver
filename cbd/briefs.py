"""Task specs (briefs) for the agents of one agent checkbox. They are files in the checkbox work folder
<work>/<PKG>/<NN>/ and every result comes back as a file there — worker_done carries only short lines.

The original briefs were lost; these restate the loop rules (티켓 체크박스 커밋 리뷰 루프 · 라운드 · 커밋과 지라 갱신)
in the form the driver checks afterwards: staged changes + commit-message.md after implementation, findings-<name>.md
per reviewer, verdict.md + exactly one new commit whose subject is the checkbox title after the judge.
"""

import os

COMMON = """\
## 공통 규칙

- 작업 위치는 워크트리 `{worktree}` (브랜치 `{branch}`) 하나다. 그 밖에는 이 체크박스 작업 폴더 `{work}` 에만 쓴다.
- 워크트리를 지우거나 옮기지 않는다. 다른 브랜치로 checkout·rebase·reset 하지 않는다.
- 테스트는 아래 명령으로만 돌린다(공유 DB·스택을 직접 띄우지 않는다): {test_hint}
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


def impl(cfg, design, ticket, box, worktree, work, tc_rows=""):
    c = context(cfg, design, ticket, box, worktree, work)
    p = paths(work)
    tc = f"\n- 이 체크박스의 TC 행 원문: `{p['tc_rows']}`" if tc_rows else ""
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
2. 변경을 `git add` 로 **스테이지에 올리고 커밋하지 않는다.** 리뷰는 스테이지 상태로 받는다.
3. 커밋 메시지 초안을 `{p['draft']}` 에 쓴다. 첫 줄은 정확히 `{c['subject']}` 이다. 본문에 바꾼 것과 검증한 것을 적는다.
4. 관련 테스트를 돌리고 결과(통과·실패 수, 실패 이름)를 `{p['test']}` 에 적는다.
5. `worker_done` — 스테이지 파일 수 · 초안 경로 · 테스트 결과 한 줄.

{COMMON.format(**c)}
- 이 터미널은 리뷰 뒤 판정에 다시 쓰인다. 끝나도 닫지 않는다.
"""


def review(cfg, design, ticket, box, worktree, work, name):
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
