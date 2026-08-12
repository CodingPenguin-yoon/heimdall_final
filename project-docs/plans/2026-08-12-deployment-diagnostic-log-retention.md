# 배포 실패 진단 로그 보존 Plan

- 상태: `COMPLETED`
- 날짜: `2026-08-12`
- 승인 근거: 사용자가 개인 프로젝트 운영 수준에 맞춰 기존 배포·NGINX rollback 흐름은 유지하고,
  실패 진단을 최대한 저장한 뒤 실패한 새 자원을 정리하는 단순화한 범위로 구현 시작을 승인함

## 현재 동작과 문제

Heimdall은 새 코드를 받아 Docker image를 만들고, service container를 실행하고, health check를 한 뒤
NGINX Preview를 새 버전으로 전환한다.

현재 배포가 실패하면 DB에는 `IMAGE_BUILD_FAILED`, `SERVICE_START_FAILED`,
`SERVICE_HEALTH_TIMEOUT` 같은 짧은 코드만 남는다. Docker 명령의 stdout/stderr는 최대 256KiB까지
메모리에서 읽지만 실패를 `RuntimeFailure`로 바꿀 때 버린다. 실행된 service의 최근 200줄 로그는
container가 존재할 때만 조회할 수 있고 저장하지 않는다.

일반 실패 처리는 candidate cleanup 뒤에 retry 또는 terminal event를 기록한다. container를 삭제하면
Docker service log도 함께 사라지므로 사용자는 실제 실패 원인을 사후에 확인할 수 없다.

NGINX 전환 직후 Worker가 종료된 극히 드문 경우에는 DB와 실제 Preview가 다를 수 있다. 현재
`RECOVERY_STATE_UNCERTAIN` 보호 로직은 이때 실제 서비스 중일 수 있는 새 container를 함부로 삭제하지
않기 위한 별도 안전장치다.

## 목표

- 일반 배포 실패에서 오류 출력과 존재하는 service log를 cleanup 전에 저장한다.
- 진단 기록이 DB에 commit되면 실패한 새 container·network·image를 즉시 정리한다.
- NGINX 전환 후 실패에서는 사용자 접속을 이전 배포로 복구하고 실제 Preview를 확인하는 현재 동작을
  로그 저장보다 먼저 수행한다. 로그 저장은 접속 복구를 지연시키지 않으며 새 자원 삭제 전에만
  완료한다.
- container가 만들어지기 전의 build 실패도 Docker build 오류 출력으로 설명한다.
- 배포 상세 화면에서 실패 event와 연결된 진단 로그를 조회한다.
- 기존 deployment event, service-log snapshot/SSE와 recovery 안전장치는 가능한 한 그대로 유지한다.

## 단순화한 동작

### 일반 실패

```text
Docker build 실패
-> build stdout/stderr 저장
-> 만들어진 새 자원 즉시 정리
-> retry 또는 FAILED

container start/health 실패
-> 실패 command 출력과 service별 최근 로그 저장
-> 새 container·network·image 즉시 정리
-> retry 또는 FAILED

NGINX 전환 실패 + 기존 Preview 복구 확인
-> NGINX를 이전 배포로 즉시 복구
-> 기존 Preview 응답 확인
-> 실패 command 출력과 아직 남아 있는 새 service log 저장
-> 새 자원 즉시 정리
-> retry 또는 FAILED
```

사용자 접속 복구와 새 자원 cleanup을 분리한다. NGINX 설정·network 복구는 가용성을 위해 먼저
실행하고, 실패한 새 service container는 로그 수집이 끝날 때까지 유지한 뒤 정리한다.

### 드문 안전 예외

```text
NGINX 전환 직후 Worker 종료
-> DB와 실제 Preview 중 무엇이 맞는지 기존 recovery가 확인

새 버전이 실제 active
-> 삭제하지 않고 SUCCEEDED로 수렴

기존 버전이 active이고 새 버전이 미사용
-> 진단 로그 저장 후 새 버전 정리

판단 불가
-> 기존 RECOVERY_STATE_UNCERTAIN 보호 로직으로 보존
```

이번 변경에서는 새 `RECOVERING` 상태, activation intent, commit/config/image ID 교차 검증, 새로운 자동
재확인 scheduler를 추가하지 않는다. recovery 정책 자체의 재설계는 실제 운영 문제가 확인될 때 별도
Plan으로 다룬다.

## 확정된 저장 정책

- 기존 `deployment_events`에는 진단 저장 상태와 요약만 저장한다.
- 실제 로그는 별도 `deployment_diagnostic_artifacts` table에 command/service별 JSONB row로 저장한다.
- command artifact에는 safe operation 이름, return code와 bounded stdout/stderr를 저장한다.
- service artifact에는 service 이름, container 상태·exit code, timestamp가 있는 stdout/stderr를 저장한다.
- raw command argv, environment와 filesystem path 원문은 저장하지 않는다.
- command/service artifact당 최대 256KiB로 제한한다.
- service log는 최근 200줄, line당 최대 16KiB로 제한한다.
- 현재 최대 16 service 기준 실패 event 하나는 최악 약 4.25MiB로 제한한다.
- artifact는 기본 30일 보존하며 환경 설정으로 조정한다.
- 만료 artifact는 API에서 즉시 제외하고 Worker가 작은 batch로 삭제한다.
- deployment event 이력은 artifact가 만료돼도 유지한다.

## Secret과 실패 경계

- Worker는 known project secret과 managed database password를 준비한 뒤 raw log를 exact redaction한다.
- 안전한 redaction이 불가능하면 원문을 저장하지 않고 `REDACTION_UNAVAILABLE` metadata만 저장한다.
- container 없음, Docker logs 실패나 timeout도 service별 stable failure metadata로 저장한다.
- 로그 payload를 저장하지 못하면 가능한 경우 수집 실패 metadata를 남기되, 진단 저장 실패가 일반
  failure cleanup을 막지는 않는다.
- diagnostic DB transaction 자체가 실패해도 구조화 deployment failure event와 기존 cleanup을
  계속한다. 진단 보존은 best-effort이며 runtime 자원 회수가 우선이다.
- Docker command와 logs를 실행하는 동안 DB transaction을 유지하지 않는다.
- 저장 직전 현재 claim token을 다시 확인하고 diagnostic event와 artifact를 짧은 transaction으로 함께
  commit한다.

## 데이터 계약

`deployment_diagnostic_artifacts`의 제안 필드:

- artifact ID
- deployment ID와 diagnostic event ID
- kind: `COMMAND_OUTPUT` 또는 `SERVICE_LOG`
- safe operation과 optional service name
- 원인이 된 failure stage/code
- capture status와 stable capture failure code
- command return code 또는 container status/exit code
- captured/expired 시각
- line/byte count와 truncated 여부
- 성공적으로 redaction된 bounded JSONB line payload

같은 diagnostic event에서 kind·operation·service 조합을 idempotency key로 사용한다. deployment/event가
삭제되면 artifact도 cascade 삭제한다. payload에는 검색 index를 만들지 않고 deployment ID, event ID와
expiry metadata만 index한다.

## 공개 API와 화면

```text
GET /api/deployments/{deploymentId}/diagnostics
GET /api/deployments/{deploymentId}/diagnostics/{artifactId}
```

- 목록 API는 payload 없이 종류, service, 상태, 크기, exit code, 수집·만료 시각만 반환한다.
- 단일 artifact API만 선택한 bounded payload를 반환한다.
- 두 응답은 `Cache-Control: no-store`를 사용한다.
- API process에는 Docker socket과 SecretStore 접근을 추가하지 않는다.
- 배포 상세의 `서비스 로그` 영역은 실패 배포에서 보존 모드로 전환하고 command 또는 service를 선택해
  event와 연결된 내용을 조회한다.
- 로그가 만료됐거나 수집되지 않았으면 빈 화면 대신 그 이유를 표시한다.
- 진행 중이거나 성공한 배포에서는 같은 service-log panel의 live snapshot/SSE 동작을 유지한다.

## 범위

- additive diagnostic artifact migration과 deployment-owned repository
- failed Docker command의 bounded result 전달
- exact-label service log collector와 known-secret redaction
- Deployment Worker의 capture-before-cleanup 순서
- 기존 reconciliation이 미사용 candidate를 cleanup할 때의 capture-before-cleanup 순서
- metadata/content API와 배포 상세 서비스 로그 통합 UI
- 30일 expiry filtering과 Worker bounded purge
- `RESOLVED` reconciliation에서 재실행·force 버튼을 숨기는 작은 UI 교정
- 관련 unit, PostgreSQL integration, Frontend, E2E와 opt-in Docker smoke

## 비범위

- 배포/recovery 상태 machine 재설계
- 새 `RECOVERING` deployment 상태와 새 scheduler
- durable activation intent와 activation phase schema
- commit/config fingerprint와 실제 image ID의 추가 교차 검증
- 기존 72시간 uncertain reconciliation 정책 변경
- Git, Heimdall Worker와 성공한 Docker command의 일반 로그 저장
- 전체 stdout/stderr 무제한 저장, 검색, 다운로드와 외부 log backend
- 일반 DLP, multi-host aggregation과 다중 사용자 RBAC

## 작은 수직 단계와 검증

### 1. 저장 계약

- additive table, model, repository와 30일 expiry를 구현한다.
- diagnostic event와 command/service artifact를 claim-fenced transaction으로 저장한다.
- PostgreSQL integration에서 원자성, idempotency, expiry, cascade와 claim loss를 검증한다.

안전한 중단: 새 table만 존재하며 Worker와 사용자 동작은 바뀌지 않는다.

### 2. Diagnostic 수집

- command runner가 실패한 command의 bounded result를 안전하게 전달한다.
- Docker operation을 allowlist code로 분류하고 raw argv는 버린다.
- 기존 exact-label과 redaction 규칙을 재사용해 service별 최근 200줄을 수집한다.
- build failure, stopped/missing container, stdout/stderr, exit code, truncation과 secret canary를 검증한다.

안전한 중단: Worker 내부 read-only collector만 존재하며 cleanup 순서는 바뀌지 않는다.

### 3. 일반 실패의 저장 후 즉시 cleanup

- 일반 failure마다 command/service 진단을 수집한다.
- NGINX 전환 후 failure는 기존 rollback을 먼저 실행해 이전 Preview를 복구하고, 새 service container를
  유지한 상태에서 diagnostic을 수집한다.
- diagnostic 저장을 시도한 뒤 성공 여부와 무관하게 기존 candidate cleanup과 retry/FAILED 처리를
  실행한다.
- rollback은 diagnostic 저장 실패와 무관하게 먼저 완료되며, DB 저장 실패, claim loss, Worker crash와
  cleanup 실패 순서를 테스트한다.
- 진단 저장 시도보다 먼저 Docker candidate 삭제 명령이 호출되지 않음을 검증한다.

### 4. 조회 화면

- metadata 목록과 단일 content API를 추가한다.
- 실패한 배포의 서비스 로그 영역에서 command/service 보존 로그를 조회한다.
- loading, partial, unavailable, expired와 redaction failure를 구분한다.

### 5. 기존 recovery 연결과 마무리

- 기존 recovery가 미사용 candidate cleanup을 결정했을 때도 diagnostic 저장을 먼저 시도하되 저장
  실패가 verified cleanup을 막지 않게 한다.
- 실제 active와 uncertain 경로는 삭제하지 않는 현재 동작을 회귀 테스트한다.
- `RESOLVED` UI에서 위험 동작을 숨긴다.
- expiry purge, Backend/Frontend 집계, E2E와 opt-in PostgreSQL/Docker smoke를 실행한다.
- 완료 전 `$verify-change`를 사용하고 현재 문서를 갱신한다.

## 인수 조건

- build 실패는 bounded Docker 오류 출력과 return code를 보존한 뒤 새 자원을 정리한다.
- start/health 실패는 존재하는 service의 최근 로그를 최대한 보존한 뒤 새 자원을 정리한다.
- NGINX 전환 후 실패는 이전 Preview 복구·확인을 diagnostic 저장보다 먼저 수행한다.
- diagnostic 저장이 느리거나 실패해도 사용자 접속 복구를 지연시키지 않는다.
- known secret 원문은 DB, API, UI, event, exception과 test output에 나타나지 않는다.
- 수집에 실패한 service는 원문 대신 stable 실패 이유를 남긴다.
- diagnostic 저장 시도 전에 container·network·image cleanup이 실행되지 않는다.
- diagnostic payload·metadata 저장이 실패해도 일반 failure cleanup과 `FAILED` 수렴은 계속한다.
- 일반 실패는 진단 저장 뒤 기존 retry 또는 `FAILED`로 즉시 진행한다.
- recovery가 새 버전을 실제 active로 판단하면 삭제하지 않고 성공으로 수렴한다.
- recovery가 판단 불가이면 기존 보호 로직대로 삭제하지 않는다.
- 기존 event SSE와 live service-log API에 diagnostic payload가 섞이지 않는다.
- 만료 artifact는 조회되지 않고 bounded batch로 삭제되며 deployment event는 유지된다.

## Rollback과 안전한 중단

- migration은 additive table/index만 추가한다. 이전 binary는 이를 읽지 않는다.
- API/UI rollback은 이미 저장한 artifact와 기존 배포 처리를 손상하지 않는다.
- Worker capture wiring을 rollback하면 기존 cleanup-first 동작으로 돌아가므로 in-flight 배포가 없을 때
  수행한다.
- purge는 expired artifact만 ID/expiry 조건으로 작은 batch 삭제한다.
- Docker smoke는 test-owned exact-label resource만 생성·정리한다.

## 문서 영향

- `project-docs/architecture.md`: bounded diagnostic artifact와 capture-before-cleanup
- `project-docs/project-profile.md`: failure diagnostic에 한정된 30일 저장 예외
- `README.md`: 실패 로그 조회, 보존 기간과 수집 실패 표시
- `.env.example`: diagnostic retention 설정

## 완료된 결정

- redaction/log read 실패는 가능한 metadata로 남기고 실제 payload 없이 cleanup을 계속한다.
- diagnostic DB 저장 자체가 실패해도 개인 프로젝트 운영 단순성을 위해 rollback·cleanup을 막지 않는다.
- 관리자 force cleanup도 diagnostic 저장을 먼저 시도하지만 저장 성공을 필수 조건으로 삼지 않는다.

## 구현 및 검증 기록

- additive `deployment_diagnostic_artifacts` migration, claim-fenced 일반 실패 저장과 terminal
  reconciliation 저장, 30일 expiry filtering·bounded purge를 구현했다.
- 실패한 Docker command의 bounded stdout/stderr·return code·safe operation을 보존하고, exact label
  service log와 container status·exit code를 cleanup 전에 수집하도록 연결했다.
- NGINX rollback을 diagnostic 수집보다 먼저 수행하고, diagnostic 수집·DB 오류는 cleanup을 막지 않되
  claim을 잃은 Worker는 runtime을 삭제하지 않도록 fencing을 유지했다.
- metadata/detail `no-store` API를 추가하고 배포 상세의 단일 서비스 로그 영역이 실패 시 event-linked
  보존 로그로 전환되도록 통합했으며, `RESOLVED` reconciliation의 재확인·강제 정리 UI를 숨겼다.
- Backend Ruff format·lint와 전체 pytest `145 passed, 12 skipped`, Frontend `pnpm verify`
  (9 test files·24 tests, typecheck, production build), Chromium E2E `5 passed`를 확인했다.
- 실행 중 개발 DB와 격리된 PostgreSQL 18.4 임시 container에서 migration, diagnostic atomicity·expiry,
  기존 deployment job·reconciliation integration `4 passed`를 확인한 뒤 container를 삭제했다.
- opt-in 실제 Docker deployment failure smoke는 실행하지 않았다. command/service 수집은 bounded
  subprocess·exact label reader 단위 테스트와 기존 Docker adapter 회귀 테스트로 검증했다.
