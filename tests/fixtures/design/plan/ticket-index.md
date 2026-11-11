# plan/ticket-index

## 요약표

| 티켓 | 저장소 · 베이스 | 층 | 선행 | 체인 앞 | 핀 |
| --- | --- | --- | --- | --- | --- |
| A1 | core · `main` | 0 | — | — | — |
| A2 | core · A1 위 | 1 | A1 | A1 | — |
| B1 (M) | web · `develop` | 1 | A1 | — | A1 |

## core

### A1 모델

- 필수: [domain](../domain.md)
- 테스트: [tests/A](../tests/A.md) TC-A-01~TC-A-03

### A2 서비스

- 테스트: [tests/A](../tests/A.md) TC-A-04

### B1 화면

- 테스트: tests/M
