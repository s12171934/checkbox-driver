---
title: checkbox-driver
created: 2026-11-10
updated: 2026-11-11
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
| 티켓 드라이버 | `cbd <키> run <티켓>` (티켓당 한 프로세스·한 터미널·한 Orca Run) | 워크트리 → 태스크 사전 등록 → 체크박스마다 구현·리뷰·판정 → verified |
| 워커 | Orca 오케스트레이션 워커 | 구현(스테이지만) · 리뷰(findings 파일) · 판정(커밋) |

## 입력과 상태

- 설계: `<design_root>/plan/commits.md`(체크박스 = 커밋) · `ticket-index.md`(요약표·티켓 절) · `jira-map.md`(Jira 키·브랜치) ·
  `order.md`(완료 기준·스택 체인) · `tests/*.md`(TC 행). 표는 머리글 이름으로 읽으므로 열 순서가 달라도 된다.
- 설정·상태: `~/.checkbox-driver/<키>/` (`CBD_HOME` 으로 바꿈) — `config.json`, `state/<티켓>.json`, `state/plan.json`(에이전트
  대시보드 `external_plans` 계약), `state/state.md`, `state/replies/<run>/`, `logs/`, `work/<티켓>/<NN>/`(브리프·초안·findings·판정문).

## 규칙 (방식 문서와 같은 것)

- 착수는 DAG 선행(ticket-index 「선행」)이 모두 verified 일 때. 베이스는 체인에서 가장 가까운 verified 티켓의 verified 헤드,
  없으면 저장소 베이스. verified 가 체인의 접두사가 아니면 그 체인을 멈춘다.
- 동시 상한 `max_parallel`(기본 12), 기동 뒤 300초 중복 방지, 죽은 드라이버 재기동 3회까지.
- 체크박스: `verify`(UI 티켓 제외)와 핀 `chore` 는 드라이버가 직접 커밋한다. 나머지는 구현 → 리뷰어 병렬(기본 Codex·Claude)
  → 같은 구현 터미널에서 판정·커밋. 커밋 제목 = `[Jira] <체크박스>`, 커밋 뒤 작업 트리가 깨끗해야 한다.
- 첫 verify 는 체인 앞 티켓이 verified 일 때만(verify 게이트). `box_prereqs` 는 체크박스 단위 선행.
- 사용량 게이트 90%: 막히면 기다리고 리뷰어를 빼거나 바꾸지 않는다. 판독 실패면 마지막 성공 판독, 판독 이력이 없으면 진행.
- 재기동해도 이어서 한다 — 완료 신호는 메일이 아니라 태스크 상태로 확인한다.

## 명령

```sh
cbd <키> init --design-root <경로> --project <이름>   # config.json 틀
cbd <키> check                                        # 설계 읽기 점검 (경고 있으면 exit 1)
cbd <키> select [--json]                              # 지금 띄울 수 있는 티켓과 사유 (jsonl)
cbd <키> schedule [--once [--json]] [--interval 60]   # 스케줄러
cbd <키> launch <티켓> · run <티켓>                   # 하나만 띄우기 · 티켓 드라이버 본체
cbd <키> status · plan                                # 현황 · plan.json/state.md 갱신
cbd <키> approve <티켓> <번호> · resolve <티켓> · defer <티켓> <번호>
```

## 재구성하지 못한 것 (다음 단계)

원본 기록에 있었지만 이번 재생성에 넣지 않았다: 착수 시트 작성 워커, 소비자 착수 실서버 대조, 화면 한 장면 대조, 티켓 통합 리뷰,
finish 때 체인 앞 verified 헤드로의 리베이스(`rebase_pending`), 체크박스 경계 리베이스, 모델별 토큰 집계, 브랜치 구현이력 노트,
run-tc(진행 파일·E2E 잠금). 브리프 원문은 남아 있지 않아 방식 문서 규칙으로 새로 썼다.

## 테스트

```sh
python3 -m unittest discover -s tests -v
```
