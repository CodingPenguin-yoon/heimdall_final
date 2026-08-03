# Managed Database와 환경변수 Plan

- 상태: `APPROVED`
- 날짜: `2026-08-03`
- 승인 근거: 기존 Heimdall/Zeus 흐름을 유지하고 Control PostgreSQL과 Managed Project PostgreSQL을 분리하며 사용자 환경변수와 Heimdall 관리 값을 병합하기로 사용자와 합의함

## 현재 동작과 문제

현재 Python 구현은 Control PostgreSQL 하나에 project, deployment, durable job만 저장한다. service 설정은 build, port, HTTP health만 가지며 환경변수와 project database 접근 선언이 없다. 초기 문서에서 managed project PostgreSQL을 비범위로 둔 것은 기존 제품 흐름과 사용자 의도에 맞지 않는다.

## 목표

- Control PostgreSQL과 Managed Project PostgreSQL을 별도 cluster·volume으로 운용한다.
- shared managed cluster 안에 project별 database와 최소 권한 login role을 생성한다.
- service별로 일반 환경변수, secret 환경변수, project database 접근 여부를 설정한다.
- 사용자 환경변수와 Heimdall 관리 `DATABASE_*`, `HEIMDALL_*` 계약을 충돌 없이 합성한다.
- raw secret을 Control DB, API response, deployment snapshot, log에 저장하지 않는다.
- UI에서 DB 생성, 상태, non-secret 연결정보와 연결 service를 확인한다.

## 범위

- service 설정의 `environment`와 `projectDatabaseAccess`
- project environment secret의 versioned owner-only file과 logical reference
- `project_database_resources` lifecycle metadata
- `POST/GET /api/projects/{id}/database`
- project database·role·`app` schema provisioning과 login probe
- DB 접근 project의 deployment request ACTIVE gate
- 두 PostgreSQL을 실행하는 개발 Compose
- React 설정 화면의 환경변수·DB 접근 입력과 상세 화면의 DB panel

## 비범위

- Docker candidate에 environment와 secret을 실제 mount하는 runtime
- password rotation, short-lived 사람용 credential, backup·restore
- project 삭제 retention·purge 실행
- PostgreSQL 이외 application database
- database 또는 volume data rollback

## 공개 계약

- 일반 환경변수는 config snapshot에 값을 저장한다.
- secret 환경변수는 write 시에만 raw 값을 받고 response와 snapshot에는 logical reference와 version만 남긴다. 값 생략은 기존 secret 보존을 뜻한다.
- `DATABASE_HOST`, `DATABASE_PORT`, `DATABASE_NAME`, `DATABASE_USER`, `DATABASE_SCHEMA`, `DATABASE_PASSWORD_FILE`과 `HEIMDALL_PROJECT_ID`, `HEIMDALL_DEPLOYMENT_ID`는 예약 이름이며 사용자 설정을 거부한다.
- DB 접근 service가 하나 이상이면 project database가 필요하다.
- 배포 요청은 project database resource가 `ACTIVE`가 아닐 때 `409 PROJECT_DATABASE_NOT_ACTIVE`로 거부한다.
- API는 raw password와 host filesystem 경로를 반환하지 않는다.

## 데이터와 소유권

- `projects.deployment_config`: plain 환경변수와 secret logical reference/version, service별 DB 접근 선언의 aggregate
- `project_environment_secrets`: project/service/name별 현재 logical reference, version, fingerprint
- `project_database_resources`: project별 desired/status/phase, deterministic database·role 이름, database credential reference/version/fingerprint
- Managed PostgreSQL: application data와 PostgreSQL catalog resource
- runtime root: raw versioned credential file

Control DB와 Managed PostgreSQL 사이에 분산 transaction을 만들지 않는다. filesystem 또는 managed DB 외부 단계를 실행한 뒤 짧은 Control DB 상태 전이를 기록하며 재시도는 현재 file/catalog를 관찰해 수렴한다.

## 보안과 실패 영향

- password는 `secrets.token_urlsafe(32)`로 생성하고 owner-only directory와 `0400` versioned file에 원자 생성한다.
- database·role 이름은 resource UUID로 결정하고 SQL identifier는 psycopg `Identifier`로 조립한다.
- provisioning 실패 시 기존 database나 role을 삭제하지 않고 `FAILED` 상태와 stable stage/code만 기록한다.
- secret 준비 후 Control DB 저장 실패로 생긴 미참조 version file은 노출보다 보존을 선택하며 후속 retention이 정리한다.
- project database와 raw secret은 source redeploy 실패나 candidate cleanup의 삭제 대상이 아니다.

## 수직 단계와 검증

1. schema와 model: migration, validation, reserved environment contract, snapshot secret redaction 단위 테스트
2. secret과 provisioning: owner-only store, Control DB state, managed PostgreSQL adapter 단위·실제 PostgreSQL smoke
3. API와 deployment gate: create/status, ACTIVE 요구, raw secret 응답 부재 계약 테스트
4. React: service environment·DB access 설정, database panel, TypeScript·component test
5. 집계 gate: backend Ruff/pytest, frontend verify, Compose config, secret canary scan

## 인수 조건

- 두 project를 provisioning하면 서로 다른 database와 role을 얻고 자기 database에만 login할 수 있다.
- DB 접근을 선언하지 않은 service에는 database 관리 값이나 credential reference가 없다.
- deployment config와 deployment snapshot에 raw secret 또는 password 포함 URL이 없다.
- 사용자가 예약 환경변수를 저장할 수 없다.
- Managed PostgreSQL이 중단되면 Control DB metadata와 기존 resource는 보존되고 명시적인 실패 상태를 반환한다.
- Docker runtime 없이도 API와 UI에서 DB 준비 상태를 안전하게 확인할 수 있다.

## 안전한 중단 지점

이 Plan은 Control DB·Managed PostgreSQL·API·UI 계약까지 완료하고 Docker mutation 전에 종료할 수 있다. 다음 runtime Plan은 ACTIVE database metadata와 secret reference만 소비하며 이 Plan의 raw credential 경계를 변경하지 않는다.

## 문서 영향

- `project-profile.md`: 두 PostgreSQL과 runtime root 기술 방향
- `product-scope.md`: managed PostgreSQL 포함 및 lifecycle 비범위 조정
- `architecture.md`: database·secret module과 데이터 소유권
- `README.md`, `.env.example`: 두 PostgreSQL 개발 실행과 application 환경변수 계약

## 구현 결과

- [x] service plain·secret 환경변수와 예약 prefix validation
- [x] owner-only versioned secret file과 Control DB metadata
- [x] project database resource migration과 단계별 state-version 전이
- [x] non-superuser provisioner의 role·database·schema·권한·SCRAM login probe
- [x] database API, deployment ACTIVE gate와 immutable database metadata snapshot
- [x] React 환경변수·DB 접근 설정과 database control panel
- [x] 두 PostgreSQL 개발 Compose와 실제 project 격리 smoke

실제 PostgreSQL 18.4 smoke에서 두 project의 database·role 분리, 자기 database login, 상대 project와 관리 database 접속 거부를 확인했다. Docker candidate environment 합성과 secret mount는 안전한 중단 지점에 따라 후속 runtime Plan에 남긴다.
