# [EX-9] 예시 기능 — 설계 (single-doc 픽스처)

절 번호는 일부러 PRD-95 와 다르게 둔다 — 파서는 머리글 이름과 표 머리글로 찾는다.

## 3. 스토리 인벤토리

| ID | 스토리 | 층 |
| --- | --- | --- |
| F-01 | 테스트 매트릭스 밖의 같은 ID — 읽지 않는다 | wrong |

## 7. 테스트 케이스 매트릭스

### 서버 — `core` / rspec

| ID | 요구 | 기대 | 층 | 시점 |
| --- | --- | --- | --- | --- |
| R-01 | 값 | 참 | unit | DA |
| R-02 | 가드 | 422 | request | DA |
| R-03 | 계산 | 2 | unit | DA |
| R-04 | 잠금 | 409 | integration | DA |

### front-common — Storybook `play`

| ID | 요구 | 기대 | 층 | 시점 |
| --- | --- | --- | --- | --- |
| F-01 | 부품 | 보임 | storybook | DA |
| F-02 | 경계 | 비활성 | storybook | DA |
| F-09 | 시각 대조 | 일치 | 시각 | F1 뒤 |

### 대시보드

| ID | 요구 | 기대 | 층 | 시점 |
| --- | --- | --- | --- | --- |
| D-01 | 저장 | 값 | unit | F3 뒤 |

## 8. 구현 순서

| 단계 | 패키지 | 선행 | 테스트 |
| --- | --- | --- | --- |
| 1 | R1 한도 설정(가드·모델) | 설계 승인 | R-01 · R-02 |
| 1 | F0 공통 선행 — 로케일 | 설계 승인 | — (테스트 없음) |
| 1 | F1 교사 부품 | F0 | F-01 · F-02 |
| 2 | R2 시도 수 계산 | R1 | R-03~R-04 |
| 2 | F3 시각 대조 | F1 | F-09 |
| 3 | D1 대시보드 | R1 · F3 | D-01 |
| 4 | V1 구현 완료 검증 | D1 | — |

## 9. Jira 티켓 맵

### 9.1 트리

| 티켓 | 저장소 | 브랜치 | 베이스/스택 부모 | 대상 테스트 | is blocked by |
| --- | --- | --- | --- | --- | --- |
| EX-11 R1 한도 설정 — 가드·모델 (§4) | `core` | `EX-11-limit-setting` | `develop` | R-01~R-02 | — |
| EX-12 R2 시도 수 계산 (§5) | `core` | `EX-12-attempt-count` | R1 | R-03~R-04 | R1 |
| EX-13 F0 공통 선행 — 로케일 | `front-common` | `EX-13-locale` | `main` | — | — |
| EX-14 F1 교사 부품 (§6) | `front-common` | `EX-14-teacher` | F0 | F-01~F-02 | F0 |
| EX-15 F3 시각 대조 | `front-common` | — (브랜치 없음) | — | F-09 | F1 |
| EX-16 D1 대시보드 | `dashboard` | `EX-16-dashboard` | `develop` | D-01 | R1 · F3 |
| EX-17 V1 구현 완료 검증 | — | — (브랜치 없음) | — | — | D1 |

### 9.2 저장소별 스택 체인

| 저장소 | 체인 (베이스 ← 아래층 ← 위층) |
| --- | --- |
| `core` | `develop` ← R1 `EX-11-limit-setting` ← R2 `EX-12-attempt-count` |
| `front-common` | `main` ← F0 `EX-13-locale` ← F1 `EX-14-teacher` |
| `dashboard` | `develop` ← D1 `EX-16-dashboard` |

### 9.3 티켓별 체크리스트

R1 — EX-11 · `EX-11-limit-setting`

```
[ ] test(core): 한도 spec 추가 (R-01 · R-02)
[ ] feat(core): 한도 판정
[ ] verify(core): R1 대상 spec 초록
[ ] 완료
```

R2 — EX-12 · `EX-12-attempt-count`

```
[x] test(core): 시도 수 spec 추가 (R-03 · R-04)
[ ] feat(core): 시도 수 계산
[ ] verify(core): R2 대상 spec 초록
[ ] 완료
```

F0 — EX-13 · `EX-13-locale`

```
[ ] chore(i18n): 로케일 받기
[ ] 완료
```

F1 — EX-14 · `EX-14-teacher`

```
[ ] test(activity): 부품 스토리 play 추가 (F-01 · F-02)
[ ] feat(activity): 부품
[ ] verify(activity): F1 대상 스토리 초록
[ ] 완료
```

F3 — EX-15 (브랜치·커밋 없음 — 검증 항목)

```
[ ] 교사 부품(F1) 스토리를 Figma 와 대조 (F-09)
[ ] 완료
```

D1 — EX-16 · `EX-16-dashboard`

```
[ ] test(dashboard): 저장 단위 테스트 추가 (D-01)
[ ] feat(dashboard): 저장
[ ] verify(dashboard): D1 대상 테스트 초록
[ ] 완료
```

V1 — EX-17 (브랜치·커밋 없음 — 검증 항목)

```
[ ] 테스트 ID ↔ 실행 결과 대조
[ ] 완료
```
