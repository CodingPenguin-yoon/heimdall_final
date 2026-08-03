---
name: plan-change
description: Heimdall의 DB schema·transaction, 인증·secret, Git/Docker/NGINX 외부 효과, 배포 상태·동시성·복구, 공개 API, 아키텍처 경계를 추가하거나 변경하기 전에 조사하고 승인 가능한 수직 구현 Plan을 작성한다. 승인된 구조 안의 작은 UI·내부 리팩터링·명확한 버그 수정에는 사용하지 않는다.
---

# Plan Change

고위험 변경을 코드보다 먼저 검증 가능한 계획으로 만든다.

## 준비

1. 루트 `AGENTS.md`와 `.agent-harness/workflow.md`를 읽는다.
2. `project-docs/project-profile.md`와 관련 현재 문서만 읽는다.
3. 관련 코드, 테스트, API, DB, 외부 adapter를 조사한다.
4. 확인하지 않은 사실과 새 사용자 결정을 분리한다.

## Plan 작성

`project-docs/plans/YYYY-MM-DD-<task>.md`를 `DRAFT`로 작성한다.

- 현재 동작과 문제
- 목표, 범위, 비범위, 인수 조건
- 데이터·보안·외부 효과와 실패 영향
- 선택한 방향과 감수한 단점
- 작은 수직 단계와 단계별 검증
- rollback 또는 안전한 중단 지점
- 문서 영향과 남은 결정

파일 목록이나 예상 줄 수를 중심으로 계획하지 않는다. 사용자에게 보이는 동작과 상태 전이, 데이터 경계를 중심으로 작성한다.

## 승인과 실행

1. 새 주요 결정이 있으면 추천안과 영향까지 설명하고 한 번에 하나만 확인한다.
2. 사용자가 이미 같은 방향을 승인했으면 반복 승인하지 않는다.
3. 승인 후 Plan을 `APPROVED`로 바꾸고 단계별로 구현한다.
4. 단계마다 관련 검증을 실행하고 결과를 같은 Plan에 누적한다.
5. 범위나 승인 경계가 달라지면 구현을 멈추고 Plan을 갱신한다.
6. 완료 전 `$verify-change`를 사용한다.
