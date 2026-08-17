# Runtime 배포 Plan

- 상태: `APPROVED`
- 구현 결과: `COMPLETE`
- 날짜: `2026-08-04`
- 승인 근거: 사용자가 단일 서비스 수직 배포를 먼저 완성하고 멀티 서비스·Secret·Managed DB·NGINX를 단계적으로 연결하는 추천 방향으로 진행하기로 함

## 현재 동작과 문제

현재 API는 READY project의 최근 `main` commit을 exact SHA로 결정하고, 설정과 Managed PostgreSQL의 non-secret metadata를 immutable deployment snapshot으로 저장한다. 같은 transaction에서 durable deployment job도 생성하며 project별 terminal 이전 deployment는 하나로 제한한다.

하지만 job을 claim하는 Worker, lease fencing, exact source checkout, Docker candidate, health check, NGINX activation, runtime resource 원본과 배포 로그가 없다. 따라서 배포 요청은 `QUEUED`에서 실제 서비스 실행으로 이어지지 않는다.

## 목표

- PostgreSQL job을 lease와 fencing token으로 claim하고 Worker 재시작 후 안전하게 회수한다.
- 요청 시 고정된 exact commit과 config snapshot만 사용해 generation candidate를 만든다.
- 단일 서비스 실제 배포를 먼저 관통시킨 뒤 같은 orchestration에 multi-service를 연결한다.
- plain 환경변수, 사용자 secret file, Managed PostgreSQL 연결정보와 password file을 서비스별로 합성한다.
- 모든 candidate health가 통과한 경우에만 project별 NGINX gateway를 atomic 전환한다.
- 실패 시 last-known-good preview와 Managed PostgreSQL data, secret version file을 보존하고 candidate만 정리한다.
- API와 UI에서 단계별 상태, 안전한 운영 이벤트, 안정 preview 주소를 확인한다.

## 범위

- `deployment_jobs` claim·renew·complete·retry와 claim token fencing
- API와 별도 실행되는 Python Worker command
- exact SHA source workspace와 안전한 repository-relative build path
- Docker CLI image build, generation network, service container, loopback health probe
- deterministic label·resource name과 deployment 단위 candidate cleanup
- 사용자 plain 환경변수와 Heimdall 관리 환경변수 합성
- 사용자 secret과 project database password의 read-only bind mount
- project별 NGINX gateway, stable loopback preview port, config test·reload·route probe
- active runtime metadata와 배포별 안전한 event log
- 배포 상태 polling과 preview link를 제공하는 React UI
- Worker·Docker·NGINX adapter 단위 테스트와 승인된 실제 smoke

## 비범위

- Private Git, arbitrary branch/tag/SHA, webhook 자동 배포
- Docker Compose file 실행과 image registry
- container application stdout/stderr의 무제한 수집과 실시간 SSE
- application log의 secret 추정 redaction
- CPU·memory quota UI와 multi-host scheduler
- public domain, TLS, 인증·다중 사용자
- image rollback, Managed PostgreSQL data rollback
- project 삭제·database purge·secret retention 자동화

## 사용자 흐름과 공개 계약

```text
배포 요청
-> QUEUED
-> Worker claim
-> exact SHA checkout
-> service image build
-> candidate network와 container 시작
-> service health check
-> NGINX candidate config 검증과 전환
-> route probe
-> SUCCEEDED와 stable preview URL
```

- API가 수락한 deployment의 `resolvedCommitSha`와 `configSnapshot`은 Worker가 다시 해석하거나 최신 설정으로 대체하지 않는다.
- deployment list/detail은 현재 상태, 실패 단계·코드, 최근 안전한 runtime event를 제공한다.
- project runtime은 stable loopback preview port와 active deployment를 가진다. public host/domain 합성은 초기 범위가 아니므로 API는 port를 반환하고 UI는 현재 browser host와 조합한다.
- 초기 UI 갱신은 TanStack Query polling을 사용한다. SSE는 실제 요구가 생긴 뒤 추가한다.
- plain 환경변수는 snapshot 값을 그대로 전달한다.
- secret kind 변수 `NAME`에는 raw 값 대신 `/run/secrets/heimdall/environment/name` 경로를 전달하고 해당 경로에 owner-only version file을 read-only mount한다. 사용자 코드는 secret kind 변수를 파일 경로로 읽는다.
- DB 접근 service만 `DATABASE_HOST`, `DATABASE_PORT`, `DATABASE_NAME`, `DATABASE_USER`, `DATABASE_SCHEMA`, `DATABASE_PASSWORD_FILE`을 받으며 password는 read-only file로 mount한다.
- 모든 service는 `HEIMDALL_PROJECT_ID`, `HEIMDALL_DEPLOYMENT_ID`를 받는다.

## 상태와 데이터 소유권

### Deployment와 job

- `deployments`는 제품 상태와 terminal 결과의 최종 원본이다.
- `deployment_jobs`는 전달 상태, attempt, lease owner·expiry, 매 claim마다 새로 생성되는 fencing token을 소유한다.
- claim은 `FOR UPDATE SKIP LOCKED`로 하나를 선택하고 expired `CLAIMED` job도 회수한다.
- 상태 변경, lease renew, retry, complete는 현재 fencing token과 함께 수행한다. token이 맞지 않으면 이전 Worker는 더 이상 Control DB 상태를 변경할 수 없다.
- 긴 Git·Docker·HTTP 작업 중 DB transaction을 유지하지 않는다.

### Runtime

- Control DB의 `project_runtimes`가 active deployment, gateway identity, stable preview port를 소유한다.
- Docker resource는 deployment·project label과 deterministic name을 가지며 Docker daemon의 관찰 결과와 Control DB를 함께 사용해 재시도 시 수렴한다.
- source workspace, generated NGINX config와 raw secret은 Control DB 밖 runtime root에 둔다.
- `deployment_events`에는 Worker가 생성한 bounded 구조 event만 저장한다. child process 원문, application log, raw environment와 secret은 저장하지 않는다.

## 외부 효과 순서

1. claim token을 발급하고 deployment를 `PREPARING`으로 전이한다.
2. 격리된 workspace에 `main`을 fetch하고 SHA가 해당 ref의 commit인지 확인한 뒤 detached checkout한다.
3. deployment·service별 deterministic image를 build한다.
4. candidate network와 모든 service container를 먼저 생성하고 전체 start 후 조기 종료 service를 한 번 더 idempotent start한다.
5. loopback random port로 각 service health endpoint를 probe한다.
6. gateway를 candidate network에 연결하고 candidate NGINX config를 별도 파일로 생성한다.
7. `nginx -t`, atomic replace, reload, route probe를 순서대로 통과시킨다.
8. active runtime metadata를 짧은 Control DB transaction으로 교체한다.
9. 이전 generation container·network는 성공 확정 뒤 정리한다.
10. job을 `DONE`, deployment를 `SUCCEEDED`로 terminal 전이한다.

## 실패·재시도·복구

- 모든 실패는 stable `failureStage`와 `failureCode`로 `FAILED` 처리하고 raw stderr는 API나 DB에 저장하지 않는다.
- claim을 잃은 Worker는 Control DB 전이를 중단한다. 새 Worker는 deterministic labels로 기존 candidate를 관찰하고 중복 생성 없이 계속하거나 candidate를 정리한다.
- build·start·health 실패는 candidate만 정리한다. active gateway와 이전 generation은 건드리지 않는다.
- NGINX config test 실패는 effective config를 변경하지 않는다.
- reload 또는 route probe 실패는 last-known-good config를 atomic 복구하고 reload한 뒤 candidate만 정리한다.
- 성공 metadata commit 전에 Worker가 중단되면 다음 claim이 gateway effective config와 Docker labels를 관찰해 성공을 확정하거나 last-known-good로 복구한다.
- retry는 명시적으로 retryable인 infrastructure failure에만 bounded backoff로 수행한다. source/build/config/health failure는 즉시 terminal 처리한다.
- 광범위한 `docker system prune`, project 전체 volume 삭제, Managed PostgreSQL·secret 삭제는 금지한다.

## 보안 경계

- Git과 Docker는 shell 문자열 없이 검증된 argument list로 실행한다.
- checkout root와 build context·Dockerfile·NGINX config 경로는 resolve 후 허용 root 내부인지 다시 확인한다.
- project container에는 Docker socket, Control DB URL, Managed PostgreSQL admin credential, host runtime root를 전달하지 않는다.
- bind mount는 snapshot이 가리키는 개별 secret file만 read-only로 허용한다.
- raw secret은 command output, structured event, exception message, API response에 포함하지 않는다.
- gateway와 service health port는 `127.0.0.1`에만 publish한다.
- Docker mutation과 cleanup은 Heimdall managed label이 일치하는 정확한 resource만 대상으로 한다.

## 선택한 방향과 감수한 단점

- 별도 queue 제품 대신 기존 PostgreSQL lease queue를 사용한다. 운영 구성은 단순하지만 높은 처리량보다 단일 호스트 정합성에 최적화된다.
- Docker SDK 대신 Docker CLI adapter를 사용한다. subprocess 경계 테스트와 stable error mapping이 필요하지만 설치 의존성과 기존 기술 방향을 유지한다.
- health probe는 loopback random port를 사용한다. 일시적으로 host port를 점유하지만 image 내부에 curl 같은 도구를 요구하지 않는다.
- 첫 UI는 polling을 사용한다. 실시간성은 낮지만 Redis·SSE lifecycle을 초기 Runtime에 추가하지 않는다.
- 사용자 secret 환경변수는 raw 값이 아니라 file path 계약이다. 사용자 코드가 파일을 읽어야 하지만 Docker inspect와 process environment 노출을 피한다.

## 수직 단계와 검증

### 1. Job Worker 기반

- migration, claim token, lease/recovery repository와 Worker orchestration
- fake stage runner로 상태 전이·fencing·retry·terminal 동작 검증
- PostgreSQL integration에서 동시 claim과 expired lease 회수 검증

안전한 중단 지점: job은 Worker가 처리하지만 Docker mutation adapter는 fake만 사용한다.

### 2. 단일 서비스 Docker candidate

- exact checkout, build path containment, image/network/container adapter
- loopback health probe와 candidate cleanup
- 단일 fixture repository를 `QUEUED -> SUCCEEDED`로 실행

안전한 중단 지점: candidate는 임시 host port에서 검증되며 stable gateway는 아직 전환하지 않는다.

### 3. Snapshot 설정 합성

- multi-service network alias
- plain 환경변수, user secret file, Managed DB metadata/password file mount
- raw secret 부재와 DB 접근 service 격리를 adapter argument 단위에서 검증

안전한 중단 지점: 모든 service candidate는 정상이나 stable preview는 기존 상태를 유지한다.

### 4. NGINX activation

- project runtime metadata, gateway ensure, candidate config, config test·reload·probe
- last-known-good restore와 이전 generation 성공 후 cleanup
- 첫 배포와 두 번째 배포 전환 smoke

### 5. 관찰 UI와 release gate

- bounded deployment events API, polling, 상태·실패·preview UI
- backend pytest·Ruff, frontend verify, Compose config
- build 실패, health 실패, NGINX 실패, Worker 재시작, multi-service+DB+secret 실제 smoke
- snapshot·DB·API·log·Docker inspect에 raw secret canary가 없는지 검사

## 인수 조건

- Worker 두 개가 실행돼도 한 claim token만 deployment 상태를 변경한다.
- Worker 중단 후 lease expiry가 지나면 새 Worker가 같은 deployment를 안전하게 회수한다.
- 단일·multi-service fixture가 exact SHA로 build되고 모든 health가 통과해야만 stable preview가 전환된다.
- 사용자 plain 값과 관리 값은 지정 service에만 전달되고 예약 이름 override는 계속 거부된다.
- user secret과 database password가 environment, Control DB, API, event, Docker inspect에 raw 값으로 나타나지 않는다.
- build·start·health·NGINX 어느 단계가 실패해도 기존 SUCCEEDED preview는 계속 응답한다.
- 성공 후 candidate가 active generation이 되고 이전 generation의 container·network만 정리된다.
- UI에서 진행 상태, 실패 단계·코드, 안전한 event와 preview link를 확인한다.

## 문서 영향

- `README.md`: Worker 실행, Docker/NGINX 개발 요구사항, secret file 계약, preview 접근
- `project-profile.md`: Runtime metadata와 Worker 운영 계약
- `architecture.md`: lease fencing, runtime resource, activation·recovery 흐름
- `product-scope.md`: 실제 runtime과 polling event 포함 상태로 현재 범위 설명

## 구현 결과

- [x] Job Worker 기반
- [x] 단일 서비스 Docker candidate
- [x] multi-service·environment·Secret·Managed DB 합성
- [x] NGINX activation·rollback
- [x] deployment events·polling UI
- [x] 실제 release smoke와 집계 검증

PostgreSQL integration에서 동시 claim 차단, 100ms lease expiry 회수와 이전 token fencing을 확인했다. 사용자 `heimdall_final` public repository의 최신 `main` SHA를 실제 detached exact checkout했다.

Docker release smoke에서는 단일 서비스와 multi-service를 image build하고 generation network·container·loopback health probe·NGINX config test·reload·route probe를 거쳐 stable preview 응답을 확인했다. multi-service smoke는 user secret file과 Managed PostgreSQL password file, managed database network를 함께 사용했고 Docker inspect·snapshot·event에 raw secret canary가 없음을 확인했다.

두 번째 deployment의 route probe를 의도적으로 실패시킨 smoke에서 새 deployment는 `FAILED/ACTIVATION/GATEWAY_ROUTE_PROBE_FAILED`로 종료됐으며 첫 번째 active deployment ID, stable preview port와 응답이 그대로 유지됐다. 모든 smoke resource는 project·deployment label을 확인한 정확한 candidate 대상으로 정리했다.

로컬 API·Worker·Frontend를 함께 실행한 수동 UI 검증에서는 public `CodingPenguin-yoon/Heimdall` 저장소 등록, Web service 설정 저장, 최신 `main` 배포 요청, 단계별 event polling, `SUCCEEDED` 표시와 stable preview 링크의 실제 응답까지 확인했다. smoke 전용 Control DB·Managed DB volume과 runtime Docker resource는 검증 후 정확한 Compose project·Heimdall label 기준으로 삭제했다.

`CodingPenguin-yoon/heimdall-test`의 frontend nginx가 backend DNS를 시작 시점에 해석하는 실제 multi-service 배포에서는 service 설정 순서와 무관하도록 모든 container를 먼저 create하고, 전체 start 뒤 조기 종료 service를 한 번 더 start한 다음 health port를 조회했다. frontend가 backend보다 먼저 설정된 snapshot에서도 두 service health, stable preview `/`, `/api/status`와 Managed PostgreSQL 연결을 확인했다.
