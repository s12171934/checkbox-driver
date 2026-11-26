---
title: checkbox-driver
created: 2026-11-10
updated: 2026-11-27
tags: [domain/인공지능, area/ai에이전트, type/설계, agent/claude-d8702234-c5f1-4d44-ab58-9d35f1de6a58]
source: ai
status: active
---

# checkbox-driver

「티켓 체크박스 커밋 리뷰」 방식(implementation-approach 스킬)을 LLM 없이 모는 스크립트 드라이버다.
프로젝트별 드라이버를 거쳐 공통 `checkbox-driver` 로 이어진 드라이버의 원본이 사라져, 볼트 기록과 방식
문서로 동작을 재구성해 다시 만들었다(2026-11-10). 평시에는 LLM 턴이 0 이고, 멈춤·질문·승인만 사람(또는 최상위 코디네이터)에게 간다.

## 구성

| 층 | 실체 | 하는 일 |
| --- | --- | --- |
| 스케줄러 | `cbd <키> schedule` (또는 캔버스 「구현 ① 프로젝트」의 드라이버 경로 + 자동화 — 이때 상시 스케줄러는 띄우지 않는다) | 선별 → 티켓마다 Orca 터미널을 열어 티켓 드라이버 기동, 끝난 티켓 터미널 닫기, plan.json 갱신 |
| 티켓 드라이버 | `cbd <키> run <티켓>` (티켓당 한 프로세스·한 터미널·한 Orca Run) | 워크트리 → 태스크 사전 등록 → (소비자) 실서버 대조 → 체크박스마다 경계 리베이스·구현·리뷰·판정(+ 커밋마다 누적 검증, UI verify 는 화면 대조) → 티켓 통합 리뷰(→ 수정 체크박스·재측정) → verified (체인 앞이 아직이면 rebase_pending) |
| 워커 | Orca 오케스트레이션 워커 | 구현(스테이지만) · 리뷰(findings 파일) · 판정(커밋) · 통합 리뷰 · 누적 검증 · 실서버 대조(결과 파일) |

## 입력과 상태

- 설계: `<design_root>/plan/commits.md`(체크박스 = 커밋) · `ticket-index.md`(요약표·티켓 절) · `jira-map.md`(Jira 키·브랜치) ·
  `order.md`(완료 기준·스택 체인) · `tests/*.md`(TC 행). 표는 머리글 이름으로 읽으므로 열 순서가 달라도 된다.
- single-doc 설계: `plan/commits.md` 가 없고 `<design_root>/design.md` 가 있으면 그 한 장에서 같은 것을 읽는다 — 절 번호가 아니라
  머리글 낱말(「구현 순서」·「체크리스트」·「테스트 케이스」)과 표 머리글(트리 `티켓 · 저장소 · 브랜치 · 베이스/스택 부모 · 대상 테스트 ·
  is blocked by`, 스택 체인 `저장소 · 체인`)로 찾는다. 체크리스트는 「R1 — PSD-1 · \`브랜치\`」 아래 코드 블록의 `[ ] <커밋 제목>`
  줄이고 `[ ] 완료` 줄은 버린다. 브랜치가 「—」인 검증 티켓은 드라이버가 띄우지 않으며(`launch` 도 안 됨), 그 티켓을 선행으로 둔
  티켓은 사람이 검증을 마치고 `start_waived` 에 적을 때까지 기다린다.
- single-doc 브리프의 읽기 허용: 브리프 「읽을 문서」는 티켓의 체크리스트·트리 행이고 「그 밖은 열지 않는다」고 하므로, single-doc
  이면 그 아래 「읽어도 되는 설계 절」 목록을 붙인다 — (a) 티켓 글·체크리스트 제목·이번 체크박스 제목이 가리키는 `§N`·`§N.M`
  절(`§11.2 F3` 이면 그 절의 F3 부분), (b) 티켓 TC ID 의 테스트 케이스 표 행, (c) 머리글에 「인벤토리」가 든 표에서 첫 열(또는
  `패키지`·`티켓` 열)이 그 티켓인 행, (d) 트랙 절 안에서 그 티켓 패키지 ID 로 시작하는 굵은 블록(`**F4 활동 정보 …**` 줄부터
  다음 `**<다른 패키지> …` 줄이나 머리글 전까지, 코드 블록 안은 보지 않음) — 「블록이 든 절 머리글 + 블록 첫 줄 번호」, (e) 그
  절이 패키지 블록·표·코드가 아닌 문단으로 시작하면 그 첫 문단(절의 공통 규칙, 줄 번호). 제목에 `§11.2 F4` 인용이 없어도 트랙
  블록을 읽게 하려는 것이다(PRD-94 F4#1 질문). 굵은 패키지 블록이 없는 설계(PRD-83 처럼 `####` 머리글과 제목 § 인용을 쓰는
  설계)는 (a)~(c) 만 붙는다. design.md 경로와 머리글 이름·줄 번호만 적고 본문은 붙이지 않는다. 구현·리뷰·통합 리뷰·누적
  검증·실서버 대조 브리프에 같은 목록이 들어간다. 다문서 설계는 ticket-index 절 그대로다.
- 검증 목표: 티켓마다 통합 리뷰·누적 검증의 기준. ticket-index 티켓 절의 `- 검증 목표:`(인라인 한 줄 또는 아래 들여쓴 목록),
  single-doc 은 체크리스트의 티켓 줄과 다음 티켓 줄 사이 `검증 목표:` 목록 또는 「티켓 · 검증 목표」 표. 설계에 없으면 Jira 본문의
  「검증 목표」 절(제목·라벨 문단 아래 목록, 또는 `검증 목표: <본문>` 한 문단)을 읽는다. 티켓을 만들 때 설계의 목표를 본문에도
  적는다. 목표가 없는 티켓은 `check` 가 `~` 로 알리지만(종료 코드는 그대로) 그대로 진행하고, 통합 리뷰는 기본 관점만 본다.
- 실서버 대조: 다른 저장소의 서버 계약을 소비하는 티켓은 `실서버 대조: <생산자> — <API>` 한 줄을 둔다(예
  `실서버 대조: R1 — M4 · M5 (§7)`). single-doc 은 체크리스트의 티켓 줄 아래(검증 목표와 같은 자리), ticket-index 는 티켓 절의
  `- 실서버 대조:`. 대시 앞의 패키지가 생산자, 뒤가 대조할 API 다. config `contract_checks.<티켓>`
  (`{"producer": "R1", "apis": "M4 · M5 (§7)"}`)이 있으면 그것이 이기고, `false` 면 설계 줄이 있어도 하지 않는다. 계획에 없는
  생산자는 `check` 경고다.
- 설정·상태: `~/.checkbox-driver/<키>/` (`CBD_HOME` 으로 바꿈) — `config.json`, `state/<티켓>.json`, `state/plan.json`(에이전트
  대시보드 `external_plans` 계약), `state/state.md`, `state/replies/<run>/`, `state/ui-accept/`(화면 불일치 사람 수용), `state/fix-requests/<티켓>/`(add-fix 요청 — 적용 뒤 `applied/`·`rejected/`), `logs/`,
  `work/<티켓>/<NN>/`(브리프·초안·findings·판정문, UI verify 는 `ui-scene.md`), `work/<티켓>/통합/`(통합 리뷰 findings·UI 캡처),
  `work/<티켓>/cumulative/<NN>.md`(누적 검증 결과), `work/<티켓>/contract/result.md`(실서버 대조 결과).
- `config.json` 은 재기동 없이 반영된다: 스케줄러는 매 회차, 티켓 드라이버는 체크박스 경계마다 다시 읽는다(진행 중 체크박스의 세션은
  그대로, JSON 오류면 직전 설정을 유지하고 경고 로그). 드라이버 기동(멈춘 뒤 재기동 포함) 때도 같은 처리(`sync_kinds`)를 한 번 거친다 —
  끝나지 않았고 dispatch 가 없는 체크박스의 종류(에이전트·드라이버 verify·드라이버 핀)를 현재 설정으로 다시 계산하고, 설정에서 빠진 세션
  태스크는 닫는다. 예: UI verify 에서 멈춘 사이 티켓을 `ui_tickets` 에서 빼면 재기동 뒤 그 칸은 드라이버 verify 로 돈다.

## 규칙 (방식 문서와 같은 것)

- 착수는 DAG 선행(ticket-index 「선행」)이 모두 verified 일 때. 베이스는 체인에서 가장 가까운 verified 티켓의 verified 헤드,
  없으면 저장소 베이스. verified 가 체인의 접두사가 아니면 그 체인을 멈춘다.
- 체크박스 경계 리베이스: 착수 때 체인 앞이 진행 중이면 더 먼 verified 헤드(없으면 저장소 베이스)에서 시작한다 — 예: 체인
  D1 ← D2 ← D3 ← D4 에서 D4 의 계약 의존이 D2 뿐이면 D3 가 끝나기 전에 D2 헤드에서 시작한다. 체크박스를 띄우기 전마다(그 체크박스의
  세션이 아직 없을 때) 더 가까운 verified 체인 헤드가 생겼는지 보고, 생겼으면 `git rebase --onto <새 헤드> <옛 베이스>` 로 옮긴다.
  옮기기 전에 완료 기준 테스트·타입 검사를 재서 초록이던 것만 옮긴 뒤 다시 잰다(빨강이면 `검증 실패` 멈춤). 충돌이면 abort 해
  브랜치를 그대로 두고 `리베이스 충돌` 로 멈춘다 — 사람이 리베이스한 뒤 resolve. 체크박스 커밋 해시는 새 커밋으로 옮겨 적고,
  이력은 state `rebases`. 리뷰·판정 세션이 도는 동안에는 옮기지 않는다(누적 검증은 git 객체만 읽어 상관없다).
- verify 게이트는 드라이버 verify 와 에이전트 verify(UI 티켓) 모두에 건다 — verify 는 체인 앞 verified 뒤라, 보통 그 경계에서 체인
  앞 헤드로 옮긴 뒤 verify 를 잰다. verify 없이 체크박스를 다 끝냈는데 체인 앞이 verified 가 아니면 `rebase_pending` 으로
  둔다. 스케줄러는 체인 앞이 verified 가 되면 그 티켓을 다시 띄우고(`체인 앞 … verified — 리베이스·재검증`), 드라이버가 그
  헤드로 옮기고 다시 잰 뒤 verified 로 만든다. rebase_pending 은 완료가 아니어서 그 위 티켓의 베이스가 되지 않는다.
- `chain_serial: true` 면 체인 앞 티켓도 착수 선행으로 기다려 늘 체인 앞 verified 헤드에서 시작한다 — 경계 리베이스가 필요 없는
  엄격한 직렬 스택이다(기본 false: 계약 의존(「선행」)만 기다리고 체인 앞은 경계 리베이스로 따라간다). D4 처럼 체인 앞과 병렬로
  돌릴 티켓이 있으면 끈다.
- 저장소 베이스는 착수 때 그 저장소에서 `git fetch origin <ref>` 한 `origin/<ref>` sha 로 워크트리를 만든다(로컬 같은 이름
  브랜치는 쓰지 않는다). 같은 브랜치 워크트리를 재사용할 때 HEAD 가 베이스를 포함하지 않으면 멈춘다. 저장소 경로는
  `repos.<이름>.path`, 없으면 Orca 저장소 목록.
- config `project_worktree`(예 `"PRD-95"`)가 있으면 저장소마다 그 이름의 프로젝트 워크트리(Orca 기본 접두 브랜치,
  `refs/remotes/origin/<저장소 베이스>`, 부모 없음)를 찾거나 만들고, 티켓 워크트리는 `--name <Jira 키>` · `--branch <설계 브랜치>` 로
  그 하위에 만든다. 재사용할 워크트리가 그 하위가 아니면 멈춘다.
- 동시 상한 `max_parallel`(기본 12), 기동 뒤 300초 중복 방지, 죽은 드라이버 재기동 3회까지.
  드라이버 생존은 터미널이 열려 있고 state 의 pid 가 `cbd … run <티켓>` 일 때다(멈추거나 죽은 드라이버의 터미널은 셸로 남는다).
  재기동 때 옛 드라이버 터미널을 닫는다. 드라이버의 예상 밖 예외는 `멈춤 [드라이버 오류]` 로 남긴다.
- 체크박스: `verify`(UI 티켓 제외)와 핀 `chore` 는 드라이버가 직접 커밋한다.
  핀 대상은 ticket-index 「핀」 열, single-doc 은 체크박스 제목 `chore(…): 핀 → <패키지> …` 의 패키지다. 핀은 `repos.<저장소>.pin_path`
  (서브모듈 경로, 예 `front-common`) 를 생산자 verified 헤드로 옮긴다 — 생산자 브랜치는 구현 중 push 하지 않으므로 생산자 워크트리의
  브랜치에서 먼저 받고, 없으면 origin 에서 받는다. 이미 그 헤드면(체인 앞과 같은 핀) 빈 커밋을 남긴다.
  나머지는 구현 → 리뷰어 병렬(기본 Codex·Claude)
  → 같은 구현 터미널에서 판정·커밋. 커밋 제목 = `[Jira] <체크박스>`, 커밋 뒤 작업 트리가 깨끗해야 한다.
  에이전트가 도는 `verify`(UI 티켓·`agent_box_overrides`)가 스테이지 없이 `test-result.md`·초안만 남기면 리뷰·판정을
  건너뛰고(태스크는 completed, plan 은 skipped) 드라이버가 초안으로 `--allow-empty` 커밋한다. 그 밖의 박스는 스테이지가
  없으면 정체다.
  판정은 `worker-start --task <판정> --terminal <구현 터미널> --worktree path:<워크트리>` 로 구현 터미널에 넣는다(끝난
  dispatch 가 잡은 터미널은 `dispatch --to` 로는 거부된다). 터미널이 닫혔거나 재사용이 어떤 이유로든 거부되면 그 사유를 로그에
  남기고 새 세션(`impl` 모델)으로 판정한다. 체크박스가 커밋되면 구현 터미널을 닫는다.
- 티켓 통합 리뷰(config `integration_review`, 기본 켬): 설계의 마지막 체크박스 뒤 리뷰어 전원이 새 세션·1 라운드로
  `base_sha..HEAD` 전체를 검증 목표에 비추어 본다(태스크 `<티켓>#통합 리뷰 <이름>` 은 착수 때 사전 등록). findings 첫 줄은
  `판정: 지적 없음|N`. 지적 0 이면 verified 로, 있으면 끝에 수정 체크박스(`integration_fix_title`, 기본
  `fix: 티켓 통합 리뷰 지적 반영`, Jira 체크리스트에도 추가)를 더해 에이전트 박스로 돌리고, 커밋 뒤 완료 기준 테스트와
  타입 검사를 다시 재기만 한다(빨강이면 `검증 실패` 멈춤). 통합 리뷰는 다시 돌지 않는다.
- UI 대조의 디자인 기준(2026-11-26 결정): 설계 루트의 스냅숏 폴더(`ui_snapshots`, 기본 `snapshots/<티켓>`·`design-snapshots/<티켓>`)
  가 먼저다. 스냅숏에 없는 프레임만 티켓 설계(「Figma 대조 프레임」 목록 등)·검증 목표의 Figma 링크를 Figma MCP 로 조회하고,
  **대조 전에 첫 패턴 폴더(`<설계 루트>/snapshots/<티켓>/`)에 저장한다** — 노드마다 `<노드 id>.png`·`<노드 id>.md`(프레임 이름·링크·
  조회 시각·값), `index.md` 표 한 줄. 이미 있는 파일은 덮어쓰지 않으므로 다음 대조부터는 스냅숏이 기준이다. 링크도 스냅숏도
  없는 화면은 「판단 불가」.
- UI 티켓(`ui_tickets`)의 통합 리뷰 codex 리뷰어는 위 기준으로 Storybook ↔ Figma 상세 대조를 한다. Storybook 명령은
  `repos.<이름>.storybook_cmd`(없으면 저장소 표준). 결과는 findings 의 `UI 대조` 표.
- UI 티켓 verify 의 화면 한 장면 대조: verify 구현 세션이 측정에 더해 장면 하나(「Figma 대조 프레임」 첫 프레임 — 공용 UI 저장소는
  Storybook, 앱은 격리 스택 실화면)를 위 기준으로 대조해 `ui-scene.md` 첫 줄 `판정: 일치|불일치|판단 불가` 를 남긴다.
  - 일치 → 평소대로 커밋(코드 변경이 없으면 드라이버가 빈 커밋). 초안 본문에 `화면 대조: 일치 — <경로>`.
  - 불일치 → verify 앞에 수정 체크박스(`ui_scene_fix_title`, 기본 `fix: 화면 대조 불일치 반영`, Jira 체크리스트에도 추가)를 넣고
    에이전트 박스로 돌린 뒤 verify 를 처음부터 다시 돈다(옛 결과는 `ui-scene-<N>.md`). 수정 체크박스가 `ui_scene_max_fix`(기본 2)개
    쌓여도 불일치면 `사람 판정` 으로 멈춘다. 판단 불가도 `사람 판정`.
  - 고치지 않을 불일치는 사람만 받아들인다: `cbd <키> accept-ui <티켓> <번호> <사유>` 뒤 resolve — verify 가 다시 돌고 수용
    사유·시각이 verify 커밋 본문에 들어간다. 불일치·판단 불가인데 스테이지가 있으면 `검증 실패`.
  - 수용은 그 verify 를 위해 넣은 수정 체크박스 중 커밋되지 않은 것을 **수용으로 닫힘**(phase `accepted`)으로 처리한다: 커밋 없음,
    체크박스에 `accepted`(대상 verify·시각·누가·사유) 기록, 미완료 Orca 태스크는 사유와 함께 completed, Jira 체크리스트 항목은
    체크하고 끝에 ` — 수용으로 닫힘: <사유>` 를 한 번 붙인다. plan.json 은 세션 `status: accepted`·`accepted`, 티켓
    `boxes_accepted` 로 구분한다(`boxes_done` 에는 안 든다). 이 칸은 다시 집지 않고, `box_deferred` 로 세우지 않으며, 끝의
    커밋 수 비교에서 빠진다. 드라이버가 멈춰 있으면 `accept-ui` 가 바로 닫고, 돌고 있으면 드라이버가 다음 체크박스 경계에서 닫는다
    (그 칸에 dispatch 가 있고 작업 트리에 변경이 있으면 닫지 않고 평소대로 리뷰·커밋한다). 코디네이터 터미널이 다른 Run 에 묶여
    task-update 가 막히면(consumer_fenced) 태스크는 드라이버가 기동 때 닫는다.
  - 수정 체크박스 구현이 스테이지 없이 끝났는데 그 verify 의 수용 기록이 이미 있으면 `정체` 로 멈추지 않고 위와 같이 수용으로 닫는다.
- 소비자 착수 실서버 대조(「실서버 대조:」 줄이 있는 티켓): 착수 직후·첫 체크박스 전에 세션 하나(`impl` 모델, 태스크
  `<티켓>#계약 대조 <이름>`)가 생산자 verified 헤드의 워크트리에서 격리 스택(빈 포트·대조 전용 DB, 기동 명령
  `repos.<생산자 저장소>.stack_cmd` 또는 저장소 표준)을 띄워 API 를 실제 요청으로 계약과 대조한다. 결과 첫 줄
  `판정: 일치|불일치|판단 불가`. 일치면 진행, 불일치면 `계약 불일치`, 판단 불가는 `환경`, 결과 없음은 `정체` 로 멈추고 resolve
  하면 대조를 처음부터 다시 한다. 생산자가 verified 가 아니면 `box_waiting`. `vault.records_dir` 가 있으면 세션이 볼트 기록도
  남긴다. 커밋·코드 변경은 없다.
- 누적 검증(config `cumulative_check`, 기본 `{"enabled": true, "reviewer": "codex"}` — 리뷰어 이름 또는 `agent`·`model` 직접 지정):
  코드를 바꾼 커밋마다 GPT 세션 하나가 `base_sha..<커밋>` 을 git 객체로만 읽어 검증 목표·커밋 사이 계약을 본다. 루프는 기다리지
  않는다(사용량 게이트가 막히면 `gated` 로 두고 다음 경계에서 띄움). 결과 첫 줄은 `판정: 문제 없음|N`. 문제·결과 없음은
  `alerts.jsonl` 의 `cumulative` 로 코디네이터에 알리고 티켓은 멈추지 않는다. verified 전에는 남은 누적 검증을 기다린다.
- 첫 verify 는 체인 앞 티켓이 verified 일 때만(verify 게이트). `box_prereqs` 는 체크박스 단위 선행.
- 사용량 게이트 90%: 막히면 기다리고 리뷰어를 빼거나 바꾸지 않는다. 판독 실패면 마지막 성공 판독, 판독 이력이 없으면 진행.
- 재기동해도 이어서 한다 — 완료 신호는 메일이 아니라 태스크 상태로 확인한다.

## 멈춤과 자가 복구

한 번의 예상 밖 Orca 오류가 티켓을 영구 blocked 로 만들지 않도록 오류를 셋으로 나눈다(`cbd/orca.py` `transient`).

| 분류 | 무엇 | 드라이버의 대응 |
| --- | --- | --- |
| (a) 일시·불명 | Orca 가 확정 거부를 주지 않은 것: 응답 없음(timeout)·JSON 아닌 출력·아래 확정 코드가 아닌 오류 코드, 확정 코드라도 지나가는 상태를 말하는 것(release runs·not ready·try again…) | 같은 단계를 60·120·240초 뒤 다시(단계가 재진입 가능하므로 처음부터 다시 돈다). 진척 없이 4번째 실패면 `retryable` 표시로 멈춤 |
| (b) 대안 경로 있음 | 구현 터미널 재사용 거부 | 즉시 새 세션 판정 |
| (c) 사람 판단 | 확정 코드(`invalid_argument`·`task_not_startable`·`not_found`·권한), 검증 실패·리베이스 충돌·계약 불일치·사람 판정(화면 대조)·승인 대기·정체·워커 질문 | 바로 멈춤 또는 대기 |

- 워커 실패(태스크 failed, 또는 워커가 worker_done 없이 exited/lost — `worker-list` 로 본다)는 태스크를 failed 로 둔 뒤
  `worker-start --retry-of <dispatch>` 로 3회 다시 띄우고, 그래도 실패면 멈춘다. 실패한 구현 터미널은 닫는다. 리뷰어가 findings
  파일을 남겼으면 실패여도 받아들인다.
- 정체(`quiet_limit_sec`, 기본 7200초)는 워커 질문이 답을 기다린 구간(`questions[].at` → `answered`, 열린 질문은 지금까지)을
  뺀 시간으로 잰다. 답장을 보낸 뒤부터 다시 세므로 오래 기다린 질문에 답하자마자 정체로 멈추지 않는다.
- 기동 응답을 잃은 worker-start 를 다시 부르기 전에 태스크 상태를 보고, 이미 dispatched/completed 면 그 워커를 이어 받는다(한
  워크트리에 워커 둘 금지).
- 스케줄러는 `retryable` 멈춤만 `auto_resolve.after_sec`(기본 900초) 뒤 자동 resolve·재기동한다(티켓당 `auto_resolve.max`,
  기본 3회). 나머지 멈춤은 사람이 `cbd <키> resolve <티켓>` 한다. resolve 는 재기동 횟수(`max_restarts`)도 되돌린다.
- 알림: 티켓 멈춤·워커 질문/에스컬레이션·자동 재개·재기동 상한 초과마다 `state/alerts.jsonl` 에 한 줄을 덧붙인다(config
  `"alerts": false` 로 끔). 데스크톱 알림(`notify`)은 그대로다.

  ```json
  {"at": "2026-11-19T17:00:00+09:00", "project": "PRD-95", "package": "R1", "kind": "blocked",
   "summary": "[연결 불명] 3회 재시도 후에도 실패 — …", "exception_kind": "연결 불명", "retryable": true, "terminal": "term_…"}
  {"at": "…", "project": "PRD-95", "package": "F0", "kind": "question", "summary": "<질문 본문>",
   "question": "…/replies/<run>/<msg>.question.md", "answer": "…/replies/<run>/<msg>.md", "msg_id": "msg_…", "worker": "ctx_…"}
  ```

  `kind`: `blocked` · `question` · `escalation` · `auto_resolved` · `restart_limit` · `integration`(통합 리뷰 지적 → 수정 체크박스) ·
  `ui_scene`(UI verify 화면 불일치 → 수정 체크박스) · `add_fix`(add-fix 요청 거부 — #n 이 그 사이 커밋됨 등) ·
  `cumulative`(누적 검증 문제 — `problems`·`commit`·`path`, 또는 결과 없음·기동 실패). 질문은 `answer` 경로에 답을 쓰면 드라이버가 보낸다.

## 명령

```sh
cbd <키> init --design-root <경로> --project <이름>   # config.json 틀
cbd <키> check                                        # 설계 읽기 점검 (경고 있으면 exit 1)
cbd <키> select [--json]                              # 지금 띄울 수 있는 티켓과 사유 (jsonl)
cbd <키> schedule --coordinator-session <세션 id> [--once [--json]] [--interval 60]   # 스케줄러
cbd <키> launch <티켓> · run <티켓>                   # 하나만 띄우기 · 티켓 드라이버 본체
cbd <키> status · plan                                # 현황 · plan.json/state.md 갱신
cbd <키> approve <티켓> <번호> · resolve <티켓> · defer <티켓> <번호>
cbd <키> accept-ui <티켓> <번호> <사유>               # UI verify 화면 불일치를 사람이 받아들임 (뒤에 resolve)
cbd <키> add-fix <티켓> <번호> <지시 파일> [--title <제목>]   # #번호(보통 verify) 앞에 에이전트 수정 체크박스
```

- `add-fix` 는 verify 처럼 코드를 고치지 않는 칸이 다른 원인(예: 핀 뒤 다른 티켓 범위 파일이 컴파일되지 않음)으로 넘어가지 못할 때
  코디네이터가 그 앞에 수정 칸을 넣는 명령이다. 새 칸은 `fix_source: coordinator`·`fix_for: <번호>` 의 에이전트 박스로, 지시 파일은
  `work/<티켓>/<새 번호>/fix-order.md` 로 복사돼 구현·리뷰 브리프에 들어간다. 제목 기본값은 지시 파일 첫 줄(머리글 `#` 제거)이고,
  이미 `fix(app): …` 처럼 커밋 제목 꼴이면 그대로, 아니면 `fix: <첫 줄>`. Jira 체크리스트에도 줄을 더한다.
  - #번호는 화면 대조 수정 때와 같은 방식(reset)으로 pending 이 되고 다시 처음부터 돈다. 그 칸의 미완료 Orca 태스크는 completed 로 닫고,
    그 태스크를 deps 로 기다리던 아직 시작 안 한 세션(뒤 칸·사전 등록된 `<티켓>#통합 리뷰 *`)도 닫은 뒤 새 태스크에 deps 를 걸어 다시
    등록한다 — Orca 는 태스크의 deps 를 바꿀 수 없어서다. 손으로 state 에서 태스크를 지우면 옛 태스크가 열린 채 남아 그것을 기다리는
    태스크가 `task_not_startable` 로 선다(PRD-94 D1).
  - 드라이버가 돌고 있으면 요청 파일만 남기고 드라이버가 다음 체크박스 경계에서 넣는다(그 사이 #번호가 커밋되면 `rejected/` 와
    `add_fix` 알림). 멈춰 있으면 바로 넣고 resolve 를 안내한다. 태스크 닫기(task-update)는 Run 에 묶인 드라이버 터미널 밖에서는
    막힐 수 있어(consumer_fenced) 닫지 못한 것은 state `stale_tasks` 에 남기고 드라이버가 기동 때 닫는다.
  - 이미 done·accepted·deferred 인 칸, 진행분 변경이 작업 트리에 남은 칸 앞에는 넣지 않는다.

- `defer` 는 그 칸을 `deferred` 로 두고 미완료 Orca 태스크를 completed(`미룸`)로 닫는다 — 열어 두면 뒤 칸 태스크가 그 태스크를 dep 으로
  가져 pending(`task_not_startable`)으로 멈춘다. 닫기가 막히면(consumer_fenced) 드라이버가 기동 때 닫는다. 미룬 칸이 남으면 티켓은
  끝에 `box_deferred` 로 선다.

- 처음 띄울 때 `--coordinator-session` 에 감독하는 코디네이터 세션 id 를 준다. 값은 `scheduler.json` 에 남아 재기동 때 생략해도 된다. 없으면 대시보드가 plan.json 을 코디네이터에 붙이지 못해 워커 계획·묶음이 안 보인다.
  그래서 인자도 저장된 값도 없으면 상시 스케줄러는 기동을 거부한다(`실패 [환경] --coordinator-session 필요 …`, 종료 코드 2).
  `--once`(캔버스 자동화)는 검사하지 않는다.

## 재구성하지 못한 것 (다음 단계)

원본 기록에 있었지만 이번 재생성에 넣지 않았다: 착수 시트 작성 워커, 모델별 토큰 집계, 브랜치 구현이력 노트,
run-tc(진행 파일·E2E 잠금). 경계 리베이스·`rebase_pending`·화면 한 장면 대조·소비자 착수 실서버 대조는 2026-11-26 에 새로 넣었다
(실서버 대조·화면 대조 세션은 아직 plan.json 세션 행에 나오지 않는다 — 로그·state 로 본다). 브리프 원문은 남아 있지 않아 방식 문서 규칙으로 새로 썼다.

## 테스트

```sh
python3 -m unittest discover -s tests -v
```
