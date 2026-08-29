# 프로젝트 안전 삭제

- 상태: `COMPLETED`
- 기준일: `2026-08-29`

## 승인된 결정

- 승인일: `2026-08-30`
- Managed DB resource가 있는 project도 삭제하며 application data purge를 포함한다.
- 전체 Project UUID와 별도로 application data 영구 삭제 문구를 정확히 확인한 요청만 허용한다.
- 기존 운영 project나 Managed DB는 구현·자동 검증 대상으로 사용하지 않는다. 실제 파괴 smoke는
  disposable project와 전용 database·role에 한해 실행 직전 별도 승인을 받는다.
- ownership marker나 Docker·filesystem identity가 불확실하면 외부 resource와 deletion metadata를
  보존한다.

## 배경과 현재 문제

현재 Heimdall에는 프로젝트 삭제 기능이 없다. `projects.name`과 `projects.repository_url`은 unique라
이미 등록한 저장소를 삭제하지 않고 같은 환경에서 다시 등록해 배포 흐름을 시험할 수 없다. 운영자가
Control PostgreSQL volume을 초기화하면 UI metadata는 사라지지만 Project Gateway, application
container, generation network, image, Edge route와 secret file은 DB 밖에 남을 수 있다. 이 상태에서는
Control DB와 실제 runtime의 소유 관계가 끊어져 이후 cleanup이 더 위험해진다.

현재 schema도 project row 단독 삭제를 허용하지 않는다.

- `deployments.project_id`는 cascade가 없어 project 삭제를 막는다.
- `project_database_resources.project_id`는 `ON DELETE RESTRICT`다.
- `project_runtimes`와 public route metadata가 cascade되더라도 실제 Docker/Edge resource는 삭제되지
  않는다.
- raw project secret은 Control DB가 아니라 runtime root의 파일에 있다.
- Managed PostgreSQL provisioner에는 create/ensure만 있고 database/role drop이 없다.
- `SecretStore`에는 create/read/resolve만 있고 안전한 project subtree 삭제가 없다.

따라서 API transaction에서 `DELETE FROM projects`만 실행하는 기능은 만들지 않는다.

## 목표

관리자가 프로젝트 전체 UUID를 확인 입력해 삭제를 요청하면 Heimdall이 durable deletion job으로
외부 효과를 순서대로 수행하고, public route와 runtime이 실제로 제거된 뒤에만 Control DB metadata를
삭제한다. 삭제가 완료되면 같은 project name과 repository URL을 다시 등록할 수 있어야 한다.

## 비범위

- 여러 프로젝트 일괄 삭제
- 이름이나 repository URL만으로 삭제
- DB row만 먼저 지우는 강제 삭제
- identity가 불확실한 Docker resource의 자동 삭제
- 실행 중인 deployment를 강제로 중단하는 기능
- 삭제 완료 후 deployment/event/diagnostic audit를 장기 보존하는 tombstone UI
- backup 자동 생성과 복구

Managed PostgreSQL server·volume lifecycle 삭제는 비범위다. 승인된 범위는 exact project resource
marker로 소유권을 증명한 database·role·application data 삭제까지다.

## 사용자 경험과 API 계약

프로젝트 설정 화면 하단에 `Danger zone`을 둔다.

- 삭제하면 public hostname, Preview, Gateway, 배포 runtime, 배포 이력과 project secret이 제거된다고
  설명한다.
- 전체 Project UUID를 입력해야 삭제 버튼을 활성화한다.
- Managed DB resource가 있으면 application data가 영구 삭제됨을 별도로 설명하고 추가 확인을 요구한다.
- 요청 후 project 상태를 `DELETING`으로 표시하고 삭제 단계를 polling한다.
- 완료되어 project 조회가 `404`가 되면 project 목록으로 이동하고 project/deployment/runtime/route
  query cache를 제거한다.
- 실패하면 안정적인 error code와 retry 가능 여부를 보여준다.

추천 API는 다음과 같다.

```http
DELETE /api/projects/{project_id}
Content-Type: application/json

{
  "confirmation": "<전체 project UUID>",
  "deleteManagedDatabase": true,
  "managedDatabaseConfirmation": "DELETE <전체 project UUID> APPLICATION DATA"
}
```

```text
202 Accepted: 삭제 intent와 durable job 기록
404 PROJECT_NOT_FOUND: project 없음
409 PROJECT_DELETION_FAILED: 실패 job은 explicit retry endpoint로 재시도해야 함
422 PROJECT_DATABASE_DELETE_CONFIRMATION_REQUIRED: Managed DB 영구 삭제 확인 누락 또는 불일치
422 PROJECT_DELETE_CONFIRMATION_MISMATCH: 전체 UUID 불일치
```

삭제 요청은 idempotent하게 만든다. 같은 confirmation과 Managed DB 파괴 확인으로 `PENDING` 또는
`CLAIMED` job을 다시 요청하면 기존 상태를 반환한다. 실패 job은
`POST /api/projects/{id}/deletion/retry`로만 재시도하며 같은 확인을 다시 요구한다. DELETE는 새 intent와
진행 중 intent의 idempotent 반환만 담당한다.

진행 조회는 다음을 추천한다.

```http
GET /api/projects/{project_id}/deletion
```

project와 job이 존재하는 동안 phase/state/error를 반환하고, 최종 metadata 삭제 뒤에는 project와 함께
`404`가 된다.

## 상태와 durable job

`projects.status`에 `DELETING`을 추가한다. `DELETING` project에서는 다음을 `409`로 차단한다.

- 설정 수정
- 새 deployment 요청
- public hostname enable/rename
- Managed DB provision/retry
- 새 reconciliation 요청

`project_deletion_jobs`는 최소 다음을 가진다.

- `project_id`
- `state`: `PENDING | CLAIMED | FAILED`
- `phase`
- `attempts`, `available_at`
- `lease_owner`, `lease_expires_at`, `claim_token`
- `last_error_code`
- `last_error_retryable`
- `delete_managed_database`
- `created_at`, `updated_at`

추천 phase는 다음과 같다.

```text
REQUESTED
-> WAITING_FOR_OPERATIONS
-> ROUTE_DISABLING
-> ROUTE_REMOVED
-> RUNTIME_CLEANUP
-> DATABASE_QUIESCING
-> DATABASE_DROP_DATABASE
-> DATABASE_DROP_ROLE
-> SECRET_CLEANUP
-> METADATA_DELETE
```

마지막 metadata transaction이 project와 deletion job을 함께 삭제하므로 완료 row를 별도로 남기지 않는다.
실패 row는 project와 함께 유지해 retry와 진단이 가능해야 한다.

기존 job과 동일하게 claim token과 lease로 fencing한다. `CLAIMED`에서만 owner·expiry·token이
존재하도록 schema check를 두며, phase는 현재 phase·token·live lease가 모두 일치할 때만 단조롭게
전진한다. stale Worker의 phase 완료와 최종 metadata transaction은 거부한다. 긴 외부 작업 중 DB
transaction을 유지하지 않고 각 phase 완료를 짧은 transaction으로 기록한다. bounded backoff와 attempt
상한 뒤 `FAILED`로 남기며 explicit retry만 `PENDING`으로 되돌린다.

## concurrency와 삭제 fence

삭제 요청 transaction은 project row를 잠근 뒤 상태를 `DELETING`으로 바꾸고 deletion job을 만든다.
deployment 생성 transaction도 project row를 잠그고 `READY` 상태를 다시 확인하도록 보강한다. API
service의 사전 `ready()` 확인만으로는 삭제 요청과의 race를 막을 수 없다.

이미 claim된 deployment를 중간에 강제 종료하지 않는다. deletion Worker는 다음이 terminal/safe 상태가
될 때까지 재예약한다.

- 진행 중 deployment job 없음
- 진행 중 public routing claim 없음 또는 삭제가 만든 더 최신 disable revision으로 fence됨
- 진행 중 runtime reconciliation 없음
- project secret filesystem operation lock을 획득할 수 있음
- Managed DB resource가 있다면 resource advisory lock을 획득할 수 있음

project가 `DELETING`이 된 뒤 새 settings/deployment/route enable이 들어오지 못해야 한다.

settings 저장은 삭제 대상 subtree 밖의 `<runtime-root>/.locks/projects/<uuid>.lock` owner-only lock을
먼저 잡고 project 상태를 다시 읽은 뒤 secret을 기록하고 status 조건부 DB finalize를 수행한다. lock
root와 file은 canonical containment, owner, private mode, symlink 부재를 검증한다. 삭제 Worker는 같은
lock을 non-blocking으로
획득·해제해 이미 시작된 settings 작업이 끝났음을 확인하기 전에는 route 제거로 진행하지 않으며,
secret subtree 삭제 동안에도 lock을 유지한다. process crash는 OS lock을 해제하고 이후 subtree 전체
관찰·삭제가 중간 secret write를 수렴시킨다.

Managed DB provision과 deletion purge는 Managed PostgreSQL에서 resource UUID로 파생한 같은 advisory
lock을 사용한다. provision의 resource UUID intent 생성 transaction부터 project row를 `FOR UPDATE`로
잠그고 `READY`를 확인해 deletion intent와 serialize한다. provision이 먼저면 DELETE가 resource를 보고
Managed DB 파괴 확인을 요구하고, deletion이 먼저면 늦은 resource intent INSERT를 거부한다. provision은
그 뒤 advisory lock을 획득하고, lock을 유지한 채 Control DB project row를 `FOR UPDATE`로 다시 읽어
`READY`를 확인하고 전체
secret→role→database→privileges→LOGIN reconcile을 수행한다. 따라서 삭제 intent보다 먼저 시작했지만
lock을 기다리던 provision도 `DELETING`을 관찰하면 DDL 전에 중단한다. 삭제 Worker는
`WAITING_FOR_OPERATIONS`에서 lock을 non-blocking 획득·해제해 실행 중 provision을 drain하고, purge
단계에서는 같은 lock을 전체 단계 동안 유지한 채 marker·owner를 재검증하고 NOLOGIN, session
termination, database·role drop을 수행한다.

settings, deployment 생성, public route user mutation, Managed DB provision/retry, runtime reconciliation
요청의 최종 Control DB transaction은 모두 project row를 `FOR UPDATE`로 잠그고 `DELETING`이 아님을
확인한다. deletion Worker 전용 route disable은 user mutation 경로와 분리해 `DELETING`에서만 허용한다.

## 외부 효과의 필수 순서

```text
1. project DELETING + deletion job 기록
2. 진행 중 operation이 끝날 때까지 대기
3. public route disable intent 기록
4. Routing Worker가 Edge snapshot에서 hostname 제거
5. route INACTIVE + applied_hostname 없음 확인
6. Project Gateway 제거
7. active/inactive/candidate container·network·image 제거
8. workspace와 project gateway config 제거
9. Managed DB resource가 있으면 marker 재검증 후 role NOLOGIN, session 종료, database·role 제거
10. project secret subtree 제거
11. Control DB child metadata와 project row 삭제
```

Edge route를 먼저 제거해야 한다. Gateway를 먼저 지우면 public hostname은 남아 있는데 upstream만
사라져 사용자에게 `502`가 노출된다. 기존 public route disable과 revision fencing을 재사용한다.
disabled route는 running Gateway 없이도 Edge snapshot에서 제거할 수 있어야 한다.

## Worker와 feature 책임

API는 Docker socket을 받지 않으며 삭제 intent와 job만 기록한다. Docker cleanup을 API router나
ProjectService에서 직접 수행하지 않는다.

Deployment Worker는 이미 Docker socket, runtime root, Git workspace와 secret store를 소유하므로 같은
process loop에 `ProjectDeletionWorker`를 조립하는 것이 최소 변경이다. feature 책임은 다음처럼 나눈다.

- `projects`: deletion intent, project fence, deletion job과 최종 metadata transaction
- `public_routes`: idempotent disable intent와 applied-inactive 확인
- `runtime`: exact-label project teardown adapter
- `project_database`: Managed DB operation lock, marker 검증과 explicit database·role purge
- `secrets`: canonical project subtree delete
- frontend project settings feature: Danger zone과 상태 표시

Project deletion Worker가 배포 orchestration을 소유하되 Docker/NGINX 구현은 `runtime` adapter를 통해
사용한다.

## Docker runtime cleanup 계약

기존 inactive candidate cleanup과 previous generation retirement 일부는 재사용할 수 있지만 project
teardown 전용 adapter가 필요하다.

삭제 대상은 Control DB의 project/deployment/runtime snapshot에서 계산한다. 각 mutation 전에 다음을
검증한다.

- `heimdall.managed=true`
- exact `heimdall.project-id`
- resource type에 따라 exact `heimdall.deployment-id`
- exact `heimdall.kind`: `gateway | service | network | image`
- deterministic name과 DB snapshot이 일치

Gateway, application container, generation network와 image를 삭제한 뒤 실제 부재를 다시 확인한다.
이름 충돌, label 충돌, Docker 관찰 실패, DB에 없는 추가 project-labeled resource가 있으면 자동 삭제를
중단하고 `PROJECT_RESOURCES_UNCERTAIN`으로 남긴다. 초기 버전에서 광범위 label filter 결과를 곧바로
삭제 명령으로 연결하지 않는다.

active generation은 일반 reconciliation cleanup에서 삭제가 거부되므로 project deletion 전용으로
"route 제거와 project delete intent가 확인된 active runtime"만 허용하는 별도 계약을 둔다.

현재 application container·network·image에는 `heimdall.kind`가 없으므로 새 deployment부터 이 label을
기록한다. 기존 resource가 exact label을 충족하지 않으면 이름이나 Docker resource type만으로 소유권을
추론해 지우지 않고 `PROJECT_RESOURCES_UNCERTAIN`으로 보존한다. Shared Edge container/network와 공용
Gateway base image는 삭제 대상이 아니다. 모든 deployment snapshot에서 deterministic expected set과
workspace를 계산하며 label filter는 예상 밖 project-labeled resource 검출에만 사용한다.

## Workspace와 Gateway config cleanup 계약

workspace는 모든 project deployment ID에서 `<git-workspace-root>/<deployment-id-hex>` exact child를
계산하고, Gateway config는 `<runtime-root>/gateways/<project-id-hex>` exact child만 대상으로 한다. 두 root와
대상은 canonical containment, expected basename, owner, private mode, 모든 component와 descendant의
symlink·special-file 부재를 file-descriptor/no-follow 방식으로 확인한다. identity가 불명확하면 unlink나
`shutil.rmtree`를 호출하지 않고 `PROJECT_FILESYSTEM_RESOURCES_UNCERTAIN`으로 보존한다. 삭제 뒤 exact
target 부재를 재확인하며 다른 project workspace, runtime root, Edge config root는 변경하지 않는다.

## Secret cleanup 계약

`SecretStore`에 project subtree 삭제를 추가한다.

```text
projects/<exact-project-uuid>/
```

canonical relative path, runtime root containment, 모든 component와 descendant의 symlink·special-file
부재, owner-only mode와 owner를 다시 검증한다. unsafe permission을 자동 교정해 삭제하지 않는다.
project 전체 UUID가 일치하는 정확한 subtree만 제거한다. Docker container와 Gateway가 모두 제거되기
전에 secret file을 지우지 않는다. 삭제 후 subtree 부재를 확인한다.

## Control DB 최종 삭제

외부 resource 부재가 검증된 뒤 하나의 짧은 transaction에서 child row를 명시적 순서로 삭제한다.

1. `project_runtimes`
2. `project_public_routes`와 `public_route_jobs`
3. `runtime_reconciliations`, diagnostic, event, deployment job은 deployment cascade 사용
4. `deployments`
5. `project_environment_secrets`
6. `project_database_resources`
7. `project_deletion_jobs`
8. `projects`

FK를 무조건 cascade로 넓히기보다 외부 cleanup 성공 후에만 최종 repository method가 실행되도록 한다.
Managed database·role 부재와 credential cleanup이 확인된 뒤에만 `project_database_resources`를 지운다.

## Managed PostgreSQL 삭제 계약

Managed DB resource가 있으면 `deleteManagedDatabase=true`와 정확한 별도 영구 삭제 확인을 필수로 한다.
metadata만 지우고 실제 database/role을 orphan으로 남기는 동작은 제공하지 않는다. 전체 Project UUID
확인만으로 application data 삭제까지 암묵적으로 허용하지 않는다.

구현 순서는 다음과 같다.

```text
runtime과 Gateway 제거
-> resource marker와 owner 재검증
-> project role NOLOGIN
-> 기존 DB session 종료
-> DROP DATABASE
-> DROP ROLE
-> credential secret 제거
-> project_database_resources metadata 제거
```

각 단계는 crash 후 재실행 가능한 idempotent phase여야 한다. marker나 owner가 다르면
`PROJECT_DATABASE_OWNERSHIP_CONFLICT`로 중단하고 외부 DB를 보존한다. database와 role drop 중간 실패도
phase에 기록해 다음 claim이 이어서 처리한다.

phase별 재관찰 계약은 다음과 같다.

- `DATABASE_QUIESCING`: database와 role이 존재하면 둘 다 exact resource marker와 expected admin owner를
  확인하고 role을 `NOLOGIN`으로 만든다. project role의 session을 종료하고 대상 database session이 0인지
  확인한다. 재실행 시 이미 `NOLOGIN`이거나 session이 없는 상태는 성공이다.
- `DATABASE_DROP_DATABASE`: advisory lock 아래 marker를 다시 확인한다. database가 없으면 이전 drop 성공으로
  수렴한다. 있으면 autocommit `DROP DATABASE` 후 새 관찰 connection에서 부재를 확인한다.
- `DATABASE_DROP_ROLE`: role이 없으면 이전 drop 성공으로 수렴한다. 있으면 marker를 다시 확인해 `DROP ROLE`
  후 부재를 확인한다.
- command acknowledgement가 모호하면 존재와 marker를 다시 관찰할 수 있을 때만 phase를 전진한다.
  존재하면서 marker를 증명하지 못하거나 관찰 자체가 실패하면 외부 resource를 보존한다.

Worker startup preflight는 Managed DB admin이 expected database owner이며 `ALTER ROLE`, database session
종료, `DROP DATABASE`, `DROP ROLE`에 필요한 권한을 가졌는지 확인한다. 모든 관찰과 종료에는 bounded
connect/statement/lock timeout을 적용한다. session 종료 timeout은 drop으로 진행하지 않고 retryable
`PROJECT_DATABASE_SESSIONS_ACTIVE`로 남긴다.

Managed DB provisioner는 resource UUID marker가 database와 role의 expected ownership과 일치하는지
확인한다. marker가 없거나 다르고, 이름만 일치하거나, 관찰 결과가 모호하면
`PROJECT_DATABASE_OWNERSHIP_CONFLICT`로 실패하고 database·role·credential과 Control metadata를 보존한다.

## 작은 구현 단계

### 단계 1: 삭제 fence와 API

- migration으로 `DELETING`과 deletion job/lease/fencing 추가
- confirmation schema, DELETE/GET API 추가
- settings/deployment/route/database/reconciliation 차단
- deployment create transaction의 project-row lock 보강
- Managed DB 별도 파괴 confirmation

검증:

- UUID confirmation mismatch
- 없는 project와 중복 요청
- Managed DB 별도 confirmation 누락·불일치
- deletion request와 deployment create race
- deletion request와 Managed DB resource intent create race
- `DELETING` 상태 mutation 차단
- 인증과 CSRF

### 단계 2: route 제거와 Worker phase

- ProjectDeletionWorker claim/retry/lease 구현
- public route disable을 idempotent하게 요청
- applied route가 실제 inactive가 될 때까지 requeue
- 기존 routing revision/claim fencing과 crash recovery 검증

검증:

- route 없는 project
- active/pending/applying/failed route
- Edge 적용 완료 전 runtime cleanup 호출 금지
- Routing Worker crash와 stale claim

### 단계 3: runtime teardown

- exact-label project runtime observer/deleter 추가
- Gateway, active generation, inactive/candidate resource 제거와 부재 검증
- workspace/gateway config 제거

검증:

- 이름과 label conflict 보존
- active/candidate/uncertain resource 조합
- Docker 관찰 실패 retry
- phase별 Worker crash 후 재개
- 다른 project와 Shared Edge resource 미변경

### 단계 4: Managed DB purge와 secret teardown

- Managed DB destructive confirmation과 operation advisory lock
- marker-verified role NOLOGIN, session drain, database·role teardown
- phase별 재관찰과 crash recovery
- safe project secret subtree 삭제

검증:

- confirmation 누락·불일치
- provision 대기/실행과 deletion race
- marker·owner conflict 보존
- active session 종료 timeout retry
- database/role drop acknowledgement 상실 뒤 재관찰
- runtime cleanup 전 DB·secret cleanup 호출 금지

### 단계 5: metadata finalization과 frontend

- 외부 부재 확인 후 child/project 최종 삭제 transaction
- Danger zone, UUID confirmation, deletion polling, 실패/retry UX
- 완료 후 목록 이동와 query cache 정리
- 같은 name/repository URL 재등록

검증:

- deployment/event/diagnostic/runtime/route/project metadata 부재
- 실패 중 project와 job 보존
- 성공 후 project API 404
- frontend confirmation과 navigation

## 통합과 실제 Docker smoke

실제 Docker·PostgreSQL smoke는 명시적으로 만든 disposable test project에서 수행한다.

```text
project 등록과 설정
-> deployment 성공
-> Preview 응답
-> public route ACTIVE
-> 삭제 요청
-> public hostname이 먼저 404
-> Gateway와 service/container/network/image 부재
-> project API 404
-> 같은 repository URL 재등록 성공
-> 다시 deployment 성공
```

별도 disposable database/role을 생성하고 marker, active connection, drop, crash resume과 재등록을
검증한다. 운영 데이터가 있는 기존 project를 smoke 대상으로 사용하지 않는다. 실제 파괴 smoke는 실행
직전에 사용자 승인을 다시 받는다.

프로젝트 삭제 job 완료와 외부 resource 부재를 확인하기 전에 Control DB volume을 초기화하지 않는다.
job이 DB와 함께 사라지면 cleanup을 재개할 근거도 사라진다.

## 실패 영향과 안전한 중단 지점

- route 제거 실패: project는 `DELETING`, 기존 Gateway/runtime은 보존
- runtime 관찰/cleanup 실패: Edge route는 제거됐지만 resource와 metadata는 보존하고 retry 가능
- secret cleanup 실패: project metadata를 보존하고 retry
- final DB transaction 실패: 외부 resource는 없지만 project/job metadata가 남아 idempotent 재시도 가능
- Worker claim 상실: stale Worker는 phase finalize를 할 수 없음
- identity 불확실: 자동 삭제하지 않고 관리자 확인 대기

각 단계는 다음 외부 mutation 전에 현재 phase와 실제 resource 상태를 다시 관찰한다. 세 번 이상의 수정
시도가 서로 다른 orphan 문제를 만들면 구현을 멈추고 teardown architecture를 재검토한다.

## Rollback

DB migration 자체는 additive하게 유지한다. 기능 rollout 중 문제가 있으면 UI 삭제 버튼과 DELETE
endpoint를 비활성화하고 deletion Worker claim을 멈춘다. 이미 `DELETING`인 project와 job은 지우지
않고 phase와 외부 상태를 보존한다. route가 이미 제거됐지만 runtime이 남아 있다면 삭제를 되돌려
public route를 자동 재활성화하지 않는다. 관리자가 상태를 확인한 뒤 job retry 또는 별도 복구를
선택한다.

## 문서 영향

구현 시 다음을 갱신한다.

- `project-docs/product-scope.md`: project deletion 포함, Managed DB purge 선택에 따른 비범위 변경
- `project-docs/project-profile.md`: deletion ordering, identity guard, data disposition
- `project-docs/architecture.md`: deletion state/job, Edge-first teardown과 failure boundary
- `doc/02-execution-flow.md`: 삭제 실행 흐름
- `doc/04-data-and-lifecycle.md`: project delete와 DB disposition
- `doc/05-operations.md`: 안전한 삭제와 재등록 smoke
- `README.md`: 사용자 기능 목록과 운영 주의사항

## 남은 승인 경계

설계와 구현은 Managed DB purge를 포함한다. 실제 Docker·Managed PostgreSQL smoke는 사용자가 승인한
`hm-delete-smoke-20260830-a1` namespace의 disposable project와 전용 database·role에서만 수행했고,
완료 뒤 해당 Control·Edge·Managed DB container, network, volume과 temp root를 제거했다.

## 구현 중 검증 기록

- 삭제 API, `DELETING` mutation fence, durable phase/claim/lease/retry/finalize, Edge-first route
  invariant, exact-label·immutable-ID runtime teardown, owner-only filesystem/secret cleanup,
  Managed PostgreSQL marker·owner 검증과 frontend Danger zone을 구현했다.
- backend 전체 pytest는 sandbox의 Unix socket 제한을 피해 동일 명령을 권한 확장 환경에서 다시 실행해
  통과했다. Docker·Managed PostgreSQL 통합 test는 환경 변수가 없는 일반 suite에서 skip되었다.
- backend Ruff check와 format check, frontend `pnpm verify`의 format/lint/68 tests/typecheck/build,
  dev·Linux overlay·Edge Compose config가 통과했다.
- 사용자 승인 뒤 실제 destructive smoke를 수행했다. 첫 disposable project는 Preview와 public route,
  active Managed DB session을 가진 상태에서 삭제를 요청했고, mutation fence 4종이 모두 `409`를
  반환했다. route가 먼저 404가 된 뒤 exact-label runtime, workspace/config, secret, database·role·session,
  Control metadata가 제거되고 project API가 `404 PROJECT_NOT_FOUND`가 됐다. Shared Edge와 기존 project
  resource는 유지됐다.
- smoke가 owner-only가 아니던 gateway config root와 `NOINHERIT` provisioner의 session 종료 권한 활성화를
  실제로 차단했다. 두 문제 모두 metadata와 외부 resource를 보존한 FAILED job에서 발견했고, 회귀
  테스트를 먼저 실패시킨 뒤 gateway root `0700` 보장과 제한된 `SET ROLE pg_signal_backend`/`RESET ROLE`
  구간을 구현해 같은 durable job을 재시도·완료했다.
- 동일 name과 repository URL은 새 UUID로 재등록됐고 동일 정상 commit 배포가 `SUCCEEDED`했다. 새
  disposable database·role을 포함한 두 번째 삭제도 project API 404와 외부 resource 부재로 완료했다.
  모든 deletion과 외부 부재를 확인한 뒤에만 disposable Control DB volume을 제거했다.
- 최종 fresh gate는 backend 전체 pytest, Ruff check/format, frontend `pnpm verify`, dev·Linux overlay·Edge
  Compose config와 `git diff --check`다. 기존 Unix log broker round-trip test가 전체 suite 첫 실행에서
  한 차례 socket race로 실패했으나 단독 5회 재현되지 않았고, 전체 suite fresh rerun은 통과했다.
