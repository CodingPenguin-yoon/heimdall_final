# Runtime 복구·Secret·설정 편집 Hardening Plan

- 상태: `DRAFT`
- 날짜: `2026-08-17`
- 기준 commit: `b4bda102d216df4f02c5f2a39c4a617015f5bf7c`
- 승인 상태: 조사와 기록만 완료했으며 구현 방향은 아직 사용자 승인을 받지 않음

## 현재 동작과 확인된 문제

기존 제품 Plan의 구현 체크리스트는 완료됐지만 현재 코드와 승인된 운영 계약을 다시 대조한 결과,
runtime 복구·cleanup, Secret 동시성, 프로젝트 설정 편집 흐름에 실제 수정이 필요한 문제가 확인됐다.

### P0: active runtime을 DB metadata만으로 확정

`NginxGatewayActivator.recover()`는 Control DB의 `active_deployment_id`가 회수 대상 deployment와
같으면 NGINX response marker, Docker exact label, service health와 route를 다시 관찰하지 않고
즉시 `ACTIVE`를 반환한다. Worker는 이 결과를 받으면 deployment를 `SUCCEEDED`로 종료한다.

이는 회수 Worker가 DB·NGINX marker·Docker label을 대조하고 실제 target이 정상일 때만 성공을
확정한다는 현재 제품 계약과 다르다. metadata commit 직후 Worker가 중단되거나 그 뒤 service 또는
Gateway 상태가 달라진 경우 실제 Preview가 정상인지 확인하지 않고 성공 처리할 수 있다.

### P0: rollback 확인 없이 candidate cleanup 가능

activation 실패 복구 과정은 previous Gateway 재생성이나 reload 실패를 억제할 수 있고,
`rollback_candidate()`도 reload·network disconnect의 결과와 복구된 Preview marker를 확인하지 않는다.
Deployment Worker는 rollback method가 예외 없이 반환하면 진단 수집 뒤 candidate container, network와
image cleanup으로 진행한다.

따라서 Gateway가 candidate를 계속 가리키거나 previous Preview가 복구되지 않은 불확실한 상태에서도
candidate를 삭제할 수 있다. 특히 active metadata commit 뒤 filesystem 오류가 발생하면 DB는 target을
active로 가리키는데 failure cleanup이 같은 target 자원을 삭제할 수 있는 경로도 함께 차단해야 한다.

### P1: concurrent Secret 생성의 파일·metadata 불일치

`FileSecretStore.create()`는 target이 없다고 관찰한 두 요청이 같은 version을 동시에 만들 때
hard link 경쟁의 loser가 `FileExistsError`를 무시하고 자신의 payload fingerprint를 반환할 수 있다.
그 loser가 설정 version CAS 또는 project database phase CAS를 이기면 Control DB fingerprint와 실제
owner-only file의 내용이 달라지고 이후 secret resolution과 deployment가 실패한다.

### P1: 기존 Secret 설정 round-trip 실패

Project 응답의 Secret 환경변수는 raw value 대신 `configured: true`를 포함한다. Frontend는 read model과
write model을 분리하지 않고 이 객체를 설정 PUT payload로 다시 보내지만 Backend 입력 model은 extra
field를 금지한다. 따라서 기존 Secret을 유지하거나 새 value를 입력해도 `422 INVALID_REQUEST`가 날 수
있다.

### P1/P2: 설정 편집기의 안정성과 aggregate 불변식

- Route path와 환경변수 이름이 React key의 일부라 값을 입력할 때 row가 재마운트되고 focus를 잃는다.
- service 이름을 변경해도 연결된 route의 `service`는 갱신되지 않는다.
- service 이름은 기존 Secret metadata identity에도 포함되므로 이름만 바꾸면 기존 Secret 유지가
  불가능하다.
- Route 추가는 고정 `/api`를 사용해 중복 path를 쉽게 만들 수 있다.
- root route를 소유한 service를 삭제하면 `/` route가 사라질 수 있다.
- 조회 실패 화면은 404와 API·network 장애를 구분하지 않아 실패를 빈 상태로 오인할 수 있다.

Backend가 service·route·environment aggregate 불변식을 최종 검증하는 경계는 유지하되, Frontend도
제출 전에 같은 사용자 의미의 오류를 표시하고 정상 편집 중에는 유효한 관계를 보존해야 한다.

### 검증 공백

- 최신 실패 진단 기능은 단위·PostgreSQL integration과 기존 Docker adapter 회귀 검증은 통과했지만,
  실제 Docker deployment failure에서 `rollback → redaction/capture → DB 저장 → cleanup` 순서를
  검증하는 opt-in smoke는 실행되지 않았다.
- 기존 crash smoke는 active metadata commit 이전 강제 종료를 중심으로 검증하며 commit 직후
  process 중단과 runtime 불일치를 다루지 않는다.
- Project settings form, environment editor, 기존 Secret round-trip과 service rename 흐름의 직접
  Frontend test가 없다.
- Compose의 `restart: unless-stopped` 선언과 적용 상태는 확인했지만 실제 process crash 뒤 자동
  재시작은 입증되지 않았다.

## 목표

- 회수 Worker는 DB, NGINX response marker와 exact Docker 상태를 재관찰해 실제 active generation만
  성공으로 확정한다.
- previous Preview 복구가 확인되지 않으면 candidate를 삭제하지 않고 durable uncertain 상태로
  보존한다.
- active metadata commit 이후에는 target generation을 일반 candidate cleanup 대상으로 취급하지 않는다.
- concurrent Secret 생성에서도 Control DB metadata와 실제 file이 항상 같은 payload를 가리킨다.
- 기존 Secret을 노출하지 않으면서 프로젝트 설정을 읽고 그대로 저장하거나 명시적으로 교체할 수 있다.
- 설정 편집 중 focus와 service·route 관계를 안정적으로 유지하고 사용자가 수정 가능한 validation
  오류를 field 수준에서 설명한다.
- 실제 Docker failure와 crash window를 release-gated smoke로 검증한다.

## 범위

1. deployment 회수의 active 판정과 post-activation convergence
2. Gateway rollback 결과 관찰과 cleanup 보존 결정
3. Secret version file 생성의 동시성·fingerprint 정합성
4. Project settings read/write DTO 분리와 Secret 유지 payload
5. service·route·environment 편집기의 stable row identity, rename, root·unique route validation
6. 404와 일시적 API 장애의 UI 상태 분리
7. 관련 unit, PostgreSQL integration, Frontend interaction test와 opt-in Docker smoke
8. 현재 제품 문서와 검증 기록 동기화

## 비범위와 별도 후속 후보

- 최근 100건을 넘는 전역·project 배포 이력 cursor pagination
- 배포 요청 없이 stopped Gateway를 상시 감지·복구하는 background watchdog
- 인증·RBAC, public domain·TLS, multi-host와 Linux production 배포
- Docker socket proxy 또는 rootless Docker 전환
- password rotation, project database backup·restore·purge
- Backend dependency lock/constraints와 원격 CI 재도입

CI는 `2026-08-05-runtime-crash-and-browser-verification.md`에서 사용자 결정으로 취소된 범위이므로
별도 요청 없이 이번 Plan에 다시 포함하지 않는다. Backend dependency 재현성은 lock 방식과 update
workflow를 결정하는 별도 일반 작업으로 분리한다.

## 데이터·보안·외부 효과와 실패 영향

- Control PostgreSQL schema와 공개 Deployment 상태 enum은 변경하지 않는 방향을 우선한다.
- recovery observation은 Docker inspect, NGINX loopback probe와 service health를 수행하지만 관찰이
  불완전하면 mutation하지 않는다.
- Gateway rollback은 config file, container/network 연결과 stable Preview 응답을 다루므로 모든
  mutation 뒤 exact managed label과 deployment marker를 다시 확인한다.
- claim token을 잃은 Worker는 diagnostic 저장과 Docker cleanup을 수행하지 않는다.
- active 또는 uncertain generation은 자동 cleanup 대상이 아니다. 보존된 target은 기존 durable
  reconciliation이 다시 판정한다.
- raw Secret은 API, Control DB, log, deployment snapshot과 Docker environment에 저장하지 않는다.
- Secret file 경쟁의 winner를 읽어 fingerprint를 계산하며, 명시적 사용자 값이 이미 존재하는 다른
  값과 충돌하면 성공처럼 반환하지 않는다.
- Frontend의 local row identity는 API payload와 deployment snapshot에 포함하지 않는다.

## 추천 방향과 감수한 단점

### Runtime recovery

- DB metadata가 target을 가리켜도 fast-path 성공을 제거하고 Gateway marker, exact candidate resource와
  route/service health를 검증한다.
- target이 실제 active면 terminal write와 남은 promotion만 완료한다.
- previous generation이 실제 응답하면 target을 safe retry 대상으로 삼되 cleanup은 previous 응답과
  Gateway detach를 확인한 뒤에만 수행한다.
- 어떤 관찰도 확정할 수 없거나 rollback 자체가 실패하면 `RECOVERY_STATE_UNCERTAIN`으로 보존한다.

관찰 명령과 probe가 늘어 회수 시간이 길어질 수 있지만 잘못된 성공 확정이나 Preview를 제공하는
candidate 삭제보다 안전성을 우선한다.

### Secret file

- atomic link 결과와 무관하게 최종 target을 owner-only 검증으로 다시 읽고 그 실제 bytes의 fingerprint를
  반환한다.
- explicit value가 최종 target과 다르면 stable conflict로 실패한다.
- 자동 생성 credential은 winner file을 읽어 같은 credential로 수렴한 뒤 CAS를 진행한다.

경쟁에서 진 요청이 자신이 생성한 임의 값을 사용하지 못할 수 있지만 하나의 version은 하나의 실제
payload만 소유한다는 계약을 지킨다.

### Settings editor

- API read type과 write type을 분리하고 submit 직렬화가 `configured`와 UI row ID를 제거한다.
- value가 없는 configured Secret은 기존 값 유지로 보내고 새 value가 있을 때만 교체한다.
- 편집 row에 client-only stable ID를 부여한다.
- service rename은 route reference를 같은 편집 transaction에서 cascade한다.
- Route 추가는 아직 사용하지 않은 canonical path 초안을 만들고 root route 삭제는 차단하거나 즉시
  다른 service로 재할당하게 한다.
- Backend problem detail의 violation path를 field 오류로 연결하고 transport/5xx는 재시도 가능한 page
  오류로 분리한다.

client state가 조금 복잡해지지만 public API와 저장 snapshot에 UI 전용 identity를 추가하지 않는다.

## 수직 단계와 단계별 검증

### 1. Recovery active 판정

- DB만 target을 가리키고 Gateway가 previous·unreachable·다른 marker인 실패 테스트를 먼저 추가한다.
- DB, Gateway marker, exact Docker labels와 service/route health가 모두 target일 때만 `ACTIVE`를
  반환한다.
- metadata commit 직후 Worker 중단을 재현하고 회수 Worker가 실제 상태에 맞게 성공 또는 uncertain으로
  수렴하는 PostgreSQL·Docker smoke를 추가한다.
- 검증: gateway recovery unit, deployment Worker claim/fencing test, opt-in crash integration.

안전한 중단 지점: 관찰 기반 disposition만 강화하고 rollback·cleanup mutation은 기존 코드에 둔다.

### 2. Rollback과 cleanup 보존

- reload, previous Gateway 재생성, route probe와 network detach 각각의 실패 테스트를 먼저 추가한다.
- rollback 결과를 명시적으로 성공·불확실로 전달하고 successful previous Preview 관찰 전에는 cleanup을
  호출하지 않는다.
- active metadata가 target이면 일반 failure cleanup이 target container/network/image를 삭제하지 못하게
  guard한다.
- 검증: command order뿐 아니라 cleanup 미호출, active resource 보존과 stable marker를 단언한다.

안전한 중단 지점: 불확실 상태에서 자원을 더 오래 보존할 수 있지만 기존 Preview와 data를 삭제하지
않는다.

### 3. Secret 동시성 정합성

- 동일 version에 서로 다른 explicit value와 자동 생성 value가 경합하는 filesystem test를 추가한다.
- 최종 target bytes와 반환 fingerprint가 항상 일치하도록 atomic create 결과를 재검증한다.
- project setting version CAS와 project database secret phase concurrency를 PostgreSQL integration에서
  검증한다.
- 검증: owner·mode·symlink/path escape 기존 보안 test도 함께 통과한다.

안전한 중단 지점: filesystem primitive 수정까지만 적용해도 새 mismatch 생성은 막으며 기존 mismatch
자동 복구는 하지 않는다.

### 4. Settings Secret round-trip

- configured Secret이 포함된 응답을 그대로 편집해 value 없이 저장하는 Frontend test를 먼저 추가한다.
- read/write type과 submit serializer를 분리하고 `configured`를 payload에서 제거한다.
- 새 value 입력 시에만 Secret version이 증가하고 빈 입력은 기존 reference를 유지하는 Backend 계약을
  회귀 검증한다.
- 검증: ProjectSettingsForm interaction, API payload, Backend schema/service test.

### 5. Settings aggregate 편집과 오류 UX

- Route와 environment name 연속 입력 중 focus 유지 test를 추가한다.
- service rename 시 route cascade와 configured Secret 처리 정책을 적용한다.
- root·duplicate route, unknown service와 duplicate environment를 제출 전에 표시한다.
- project/deployment/settings 조회에서 404와 retryable error 화면을 분리한다.
- 검증: Testing Library와 project 설정 Chromium E2E.

### 6. Release gate와 문서

- build, start/health와 activation 실패 fixture에서 previous Preview 복구, bounded redaction artifact,
  capture-before-cleanup과 exact cleanup을 확인한다.
- recovery rollback 실패 fixture는 candidate 보존과 durable uncertain 상태를 확인한다.
- Backend Ruff format/check/pytest, Frontend verify와 Chromium E2E를 실행한다.
- opt-in Docker·PostgreSQL smoke는 test-owned project/deployment ID와 exact label만 정리한다.
- 검증 결과와 남은 운영 위험을 이 Plan에 누적하고 README, architecture와 project profile을 갱신한다.

## 인수 조건

- DB metadata만 target인 상태는 `SUCCEEDED`로 확정되지 않는다.
- 실제 Gateway marker, exact Docker candidate와 모든 health·route가 target일 때만 active로 수렴한다.
- rollback reload·recreate·probe가 실패하면 candidate cleanup이 실행되지 않는다.
- active metadata가 target을 가리키는 generation은 일반 failure cleanup으로 삭제되지 않는다.
- claim을 잃은 Worker는 diagnostic write와 cleanup을 수행하지 않는다.
- concurrent Secret create의 반환 fingerprint는 최종 target file bytes와 항상 일치한다.
- 기존 configured Secret 설정은 raw value 노출 없이 변경 없이 저장할 수 있고 새 value 입력 시에만
  version이 증가한다.
- Route path와 environment name을 연속 입력해도 focus를 유지한다.
- service rename, root route와 unique route 불변식이 저장 전에 일관되게 처리된다.
- 404와 API 일시 장애가 다른 UI 상태와 재시도 동작을 제공한다.
- 실제 Docker failure smoke가 rollback, artifact redaction·저장, cleanup 순서와 기존 Preview 보존을
  검증한다.
- Backend와 Frontend 집계 gate가 통과하고 현재 문서가 실제 계약과 일치한다.

## Rollback과 안전한 중단

- Recovery와 cleanup 단계는 mutation 전 관찰을 강화하는 방향이므로 문제가 있으면 자동 cleanup을
  중단하고 candidate를 보존한 채 `BLOCKED/UNCERTAIN`으로 남긴다.
- Secret 변경은 additive schema 없이 filesystem create primitive에 한정한다. 문제가 생기면 새 설정과
  provisioning을 중단하고 기존 version file과 metadata를 변경하지 않는다.
- Frontend serializer와 editor 변경은 Backend 공개 계약을 유지하므로 기존 UI bundle로 되돌릴 수 있다.
- 실제 Docker smoke는 test-owned exact label 자원만 만들고 `finally` cleanup 대상을 명시한다.
- 이미 존재할 수 있는 file·metadata fingerprint 불일치는 자동 수정하거나 secret을 덮어쓰지 않는다.
  별도 검사와 관리자 복구 절차가 필요하면 추가 Plan을 작성한다.

## 문서 영향

- `project-docs/project-profile.md`: recovery 성공·보존 조건과 기준일
- `project-docs/architecture.md`: active 판정, rollback outcome과 cleanup guard
- `README.md`: 운영 실패·복구 동작과 release smoke
- 이 Plan: 단계별 구현·검증 결과와 남은 위험

## 남은 결정

1. service rename 시 configured Secret을 어떻게 처리할지 사용자 승인이 필요하다.
   - 추천: route는 자동 cascade하되 Secret identity migration은 하지 않고 해당 service의 Secret 재입력을
     명시적으로 요구한다.
   - 대안: 기존 service name에서 새 name으로 secret metadata/reference를 원자적으로 이전한다. raw value
     노출 없이 가능하지만 repository transaction과 filesystem ownership 규칙을 새로 설계해야 한다.
2. settings field validation을 Backend violation path의 완전한 client mapping으로 만들지, 핵심 aggregate
   규칙만 Frontend에서 선검증할지 결정해야 한다.
   - 추천: 핵심 관계는 Frontend에서 즉시 검증하고 Backend violation은 알 수 없는 필드를 포함한 최종
     방어선으로 표시한다.
3. 실제 Docker failure smoke와 crash smoke는 release gate에서 Docker mutation을 수행한다. 구현 승인과
   함께 로컬 test-owned 자원 생성·정리 승인을 확인해야 한다.
