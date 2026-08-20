# 전역 배포 활동 Plan

- 상태: `APPROVED`
- 구현 결과: `COMPLETE`
- 날짜: `2026-08-06`
- 승인 근거: 사용자가 전역 배포 활동을 다음 작업 1순위로 선택하고 구현 시작을 승인함

## 현재 동작과 문제

프로젝트 상세에서는 해당 프로젝트의 최근 배포 이력과 배포 상세 화면을 제공하지만, 사이드바의
`배포 활동`은 비활성 상태이며 `곧 제공`으로 표시된다. Backend도 프로젝트별
`GET /api/projects/{projectId}/deployments`만 제공하므로 여러 프로젝트의 배포를 한곳에서 조회할
수 없다.

Frontend에서 프로젝트마다 목록 API를 반복 호출해 합치는 방식은 프로젝트 수만큼 요청이 늘고,
최근 활동의 전역 정렬과 조회 상한을 일관되게 보장하기 어렵다.

## 목표

- 사이드바의 배포 활동을 실제 `/deployments` 화면으로 연결한다.
- 모든 프로젝트의 최근 배포 100건을 최신순으로 조회한다.
- 프로젝트명, commit, source, 상태, 요청·종료 시각을 한 화면에서 확인한다.
- 전체·진행 중·성공·실패 상태와 프로젝트별 필터를 제공한다.
- 각 행에서 기존 `/deployments/{deploymentId}` 상세 화면으로 이동한다.
- 진행 중 배포가 있으면 목록을 자동 갱신한다.

## 범위

- 읽기 전용 `GET /api/deployments` 공개 API
- 기존 `DeploymentRead`와 `DeploymentList` 응답 형식 재사용
- deployment repository의 전역 최근 100건 최신순 조회
- Frontend 전역 deployment query와 `/deployments` route
- 기존 project 목록을 이용한 project ID와 이름 매핑
- 요약 카드, 상태·프로젝트 필터, loading·error·empty 상태와 반응형 목록
- Backend 계약 테스트와 Frontend 화면·필터·상세 링크 테스트

## 비범위

- DB schema나 migration
- project 이름을 포함하는 새 deployment 응답 DTO 또는 feature 간 repository join
- server-side 검색, cursor pagination, 사용자별 권한
- deployment event 로그 또는 application stdout/stderr
- 배포 생성·취소·재시도 동작 변경

## 데이터·보안·외부 효과

- 조회는 Control PostgreSQL의 `deployments`만 읽으며 상태를 변경하지 않는다.
- API는 기존 Deployment 공개 필드만 반환하고 config snapshot, environment, secret을 노출하지 않는다.
- project 이름은 기존 `GET /api/projects` 결과와 Frontend에서 결합해 deployment feature가 project
  repository를 침범하지 않는다.
- 목록은 최신 100건으로 제한해 무제한 DB·네트워크 응답을 만들지 않는다.
- 자동 갱신은 terminal이 아닌 배포가 목록에 있을 때만 1초 간격으로 수행한다.

## 선택한 방향과 감수한 단점

- 전역 endpoint는 기존 응답 형식을 재사용한다. API가 단순하고 호환성이 좋지만 Frontend가 project
  목록을 함께 읽어 이름을 매핑해야 한다.
- 첫 버전은 최근 100건과 client-side 필터를 사용한다. 현재 단일 관리자·단일 호스트 범위에는
  충분하지만 전체 과거 이력 탐색은 후속 cursor pagination이 필요하다.
- 표준 상태 그룹은 진행 중을 terminal이 아닌 모든 상태로 정의한다. Worker 상태가 추가되어도
  terminal 집합만 유지하면 필터가 자연스럽게 동작한다.

## 수직 단계와 검증

### 1. 전역 조회 계약

- repository와 service에 최근 100건 조회를 추가한다.
- `GET /api/deployments`가 최신순 기존 Deployment DTO 목록을 반환한다.
- 단위·API 계약 테스트와 Backend Ruff·pytest를 실행한다.

안전한 중단 지점: 읽기 API만 추가된 상태로 기존 프로젝트별 화면은 변하지 않는다.

### 2. 배포 활동 화면

- sidebar link와 `/deployments` route를 활성화한다.
- 전역 query, 요약, 필터, 목록, 상세 링크와 상태별 UI를 구현한다.
- 진행 중 배포가 있을 때만 polling한다.
- Testing Library로 필터와 상세 이동 계약을 검증한다.

### 3. 집계 검증과 브라우저 확인

- Backend 전체 Ruff·pytest와 Frontend `pnpm verify`를 실행한다.
- 실제 로컬 API와 화면에서 전역 최신순 목록, active navigation, 필터, 상세 이동을 확인한다.

## 인수 조건

- 사이드바 `곧 제공` 표시가 제거되고 배포 활동 링크가 활성화된다.
- `/deployments`에서 최근 배포가 프로젝트 구분과 함께 최신순으로 보인다.
- 상태와 프로젝트 필터가 함께 적용되며 필터 결과 수가 표시된다.
- 진행 중 배포가 있으면 목록이 자동 갱신되고 terminal 목록만 있으면 polling하지 않는다.
- 배포 행은 정확한 기존 상세 URL로 연결된다.
- API는 최대 100건과 기존 Deployment 공개 필드만 반환한다.
- 기존 프로젝트별 배포 이력과 상세 화면이 회귀하지 않는다.

## rollback과 안전한 중단

- DB migration과 write 동작이 없으므로 endpoint, route와 화면 코드를 되돌리면 기존 상태로 복원된다.
- Frontend 단계가 중단되어도 새 endpoint는 읽기 전용이며 기존 client에 영향을 주지 않는다.

## 문서 영향

- `README.md`: 전역 배포 활동 화면과 최근 100건 조회 범위
- `project-docs/project-profile.md`: 단일 관리자 전역 활동 조회 규칙

## 남은 결정

- 후속 후보: 최근 100건을 넘는 이력의 cursor pagination
- 후속 완료: 배포 상세의 구조화 event log 스트림은
  `2026-08-10-deployment-event-sse-and-log-controls.md`에서 구현·검증했다.

## 구현 검증 기록

- `GET /api/deployments` 공개 DTO 계약 테스트와 deployment service 관련 테스트가 통과했다.
- 새 화면의 프로젝트 매핑, 상태·프로젝트 필터, 기존 상세 URL 연결 테스트가 통과했다.
- 실제 로컬 API는 최근 배포 11건을 최신순으로 반환했다.
- 실제 브라우저에서 요약 `11/0/5/6`, 실패 필터 6건, `heimdall test` 결합 필터 5건과 배포 상세
  이동을 확인했다.
- Backend 집계 gate는 Ruff format·lint와 pytest `76 passed, 8 skipped`로 통과했다.
- Frontend `pnpm verify`는 9개 test file·14개 test, typecheck와 production build까지 통과했다.
