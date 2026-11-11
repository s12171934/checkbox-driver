## 스택 체인

| 저장소 | 베이스 | 체인 | 브랜치 이름 |
| --- | --- | --- | --- |
| core | `main` | A1 → A2 | `<Jira>-A1-model` · `<Jira>-A2-service` |
| web | `develop` | B1 | `<Jira>-B1-screen` |

## 0층

| 패키지 | 선행 | 완료 기준 |
| --- | --- | --- |
| A1 모델 | — | TC-A-01~TC-A-03 |
| A2 서비스 | A1 | TC-A-04 |
| B1 (M) 화면 | A1 | [tests/M](../tests/M.md) 의 storybook 층 TC-M-* 전부 |
