---
title: checkbox-driver
created: 2026-11-10
updated: 2026-11-26
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
| 티켓 드라이버 | `cbd <키> run <티켓>` (티켓당 한 프로세스·한 터미널·한 Orca Run) | 워크트리 → 태스크 사전 등록 → 체크박스마다 구현·리뷰·판정(+ 커밋마다 누적 검증) → 티켓 통합 리뷰(→ 수정 체크박스·재측정) → verified |
| 워커 | Orca 오케스트레이션 워커 | 구현(스테이지만) · 리뷰(findings 파일) · 판정(커밋) · 통합 리뷰 · 누적 검증(결과 파일) |

## 입력과 상태

- 설계: `<design_root>/plan/commits.md`(체크박스 = 커밋) · `ticket-index.md`(요약표·티켓 절) · `jira-map.md`(Jira 키·브랜치) ·
  `order.md`(완료 기준·스택 체인) · `tests/*.md`(TC 행). 표는 머리글 이름으로 읽으므로 열 순서가 달라도 된다.
- single-doc 설계: `plan/commits.md` 가 없고 `<design_root>/design.md` 가 있으면 그 한 장에서 같은 것을 읽는다 — 절 번호가 아니라
  머리글 낱말(「구현 순서」·「체크리스트」·「테스트 케이스」)과 표 머리글(트리 `티켓 · 저장소 · 브랜치 · 베이스/스택 부모 · 대상 테스트 ·
  is blocked by`, 스택 체인 `저장소 · 체인`)로 찾는다. 체크리스트는 「R1 — PSD-1 · \`브랜치\`」 아래 코드 블록의 `[ ] <커밋 제목>`
  줄이고 `[ ] 완료` 줄은 버린다. 브랜치가 「—」인 검증 티켓은 드라이버가 띄우지 않으며(`launch` 도 안 됨), 그 티켓을 선행으로 둔
  티켓은 사람이 검증을 마치고 `start_waived` 에 적을 때까지 기다린다.
- 검증 목표: 티켓마다 통합 리뷰·누적 검증의 기준. ticket-index 티켓 절의 `- 검증 목표:`(인라인 한 줄 또는 아래 들여쓴 목록),
  single-doc 은 체크리스트의 티켓 줄과 다음 티켓 줄 사이 `검증 목표:` 목록 또는 「티켓 · 검증 목표」 표. 설계에 없으면 Jira 본문의
  「검증 목표」 절(제목·라벨 문단 아래 목록, 또는 `검증 목표: <본문>` 한 문단)을 읽는다. 티켓을 만들 때 설계의 목표를 본문에도
  적는다. 목표가 없는 티켓은 `check` 가 `~` 로 알리지만(종료 코드는 그대로) 그대로 진행하고, 통합 리뷰는 기본 관점만 본다.
- 설정·상태: `~/.checkbox-driver/<키>/` (`CBD_HOME` 으로 바꿈) — `config.json`, `state/<티켓>.json`, `state/plan.json`(에이전트
  대시보드 `external_plans` 계약), `state/state.md`, `state/replies/<run>/`, `logs/`, `work/<티켓>/<NN>/`(브리프·초안·findings·판정문), `work/<티켓>/통합/`(통합 리뷰 findings·UI 캡처),
  `work/<티켓>/cumulative/<NN>.md`(누적 검증 결과).
- `config.json` 은 재기동 없이 반영된다: 스케줄러는 매 회차, 티켓 드라이버는 체크박스 경계마다 다시 읽는다(진행 중 체크박스의 세션은
  그대로, JSON 오류면 직전 설정을 유지하고 경고 로그).

## 규칙 (방식 문서와 같은 것)

- 착수는 DAG 선행(ticket-index 「선행」)이 모두 verified 일 때. 베이스는 체인에서 가장 가까운 verified 티켓의 verified 헤드,
  없으면 저장소 베이스. verified 가 체인의 접두사가 아니면 그 체인을 멈춘다.
  `chain_serial: true` 면 체인 앞 티켓도 착수 선행으로 기다린다 — 드라이버에 경계 리베이스가 없어, 끄면 선행만 같은 체인 티켓들이 각자
  저장소 베이스에서 시작해 스택이 어긋난다(기본 false).
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
- UI 티켓(`ui_tickets`)의 통합 리뷰 codex 리뷰어는 Storybook ↔ Figma 상세 대조를 한다: 설계 루트의 스냅숏 폴더
  (`ui_snapshots`, 기본 `snapshots/<티켓>`·`design-snapshots/<티켓>`)가 먼저, 없으면 티켓 설계·목표에 적힌 Figma 링크를 Figma MCP 로
  직접 조회한다. Storybook 명령은 `repos.<이름>.storybook_cmd`(없으면 저장소 표준). 결과는 findings 의 `UI 대조` 표.
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
| (c) 사람 판단 | 확정 코드(`invalid_argument`·`task_not_startable`·`not_found`·권한), 검증 실패·리베이스 충돌·승인 대기·정체·워커 질문 | 바로 멈춤 또는 대기 |

- 워커 실패(태스크 failed, 또는 워커가 worker_done 없이 exited/lost — `worker-list` 로 본다)는 태스크를 failed 로 둔 뒤
  `worker-start --retry-of <dispatch>` 로 3회 다시 띄우고, 그래도 실패면 멈춘다. 실패한 구현 터미널은 닫는다. 리뷰어가 findings
  파일을 남겼으면 실패여도 받아들인다.
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
```

- 처음 띄울 때 `--coordinator-session` 에 감독하는 코디네이터 세션 id 를 준다. 값은 `scheduler.json` 에 남아 재기동 때 생략해도 된다. 없으면 대시보드가 plan.json 을 코디네이터에 붙이지 못해 워커 계획·묶음이 안 보인다.

## 재구성하지 못한 것 (다음 단계)

원본 기록에 있었지만 이번 재생성에 넣지 않았다: 착수 시트 작성 워커, 소비자 착수 실서버 대조, 화면 한 장면 대조,
finish 때 체인 앞 verified 헤드로의 리베이스(`rebase_pending`), 체크박스 경계 리베이스, 모델별 토큰 집계, 브랜치 구현이력 노트,
run-tc(진행 파일·E2E 잠금). 브리프 원문은 남아 있지 않아 방식 문서 규칙으로 새로 썼다.

## 테스트

```sh
python3 -m unittest discover -s tests -v
```
