# 외부 Managed Database 전환 Plan

- 상태: `APPROVED`
- 날짜: `2026-08-13`
- 승인 근거: 사용자가 단일 Docker runtime을 유지하고 학생 application data를 별도 Managed DB VM으로
  먼저 분리하기로 결정했으며, 기존 Managed PostgreSQL data 초기화를 허용함
- 대상 구조: Control/Runtime VM 1대 + Managed DB VM 1대

## 결정 요약

```text
Control/Runtime VM
├─ NGINX frontend
├─ FastAPI
├─ Control PostgreSQL
├─ Heimdall Worker
├─ Docker daemon
├─ project gateway/application container
└─ Secret Store

Managed DB VM
└─ 학생 application 전용 Managed PostgreSQL
```

Control PostgreSQL은 Control/Runtime VM에 유지한다. 분리 대상은 학생 application data를 소유하는
Managed PostgreSQL뿐이다.

로컬과 운영에 별도 database mode를 만들지 않는다. 모든 환경에서 Managed PostgreSQL은 Docker
runtime 밖의 TCP endpoint로 취급한다.

```text
로컬   host.docker.internal:55433
운영   managed-db.internal:5432
```

Docker image, checkout, container, project network와 generated NGINX config는 재생성 가능한 runtime
artifact다. application data는 외부 Managed PostgreSQL에만 저장하며 임의 persistent project volume은
제공하지 않는다.

## 현재 동작과 문제

현재 FastAPI, Worker와 Managed PostgreSQL은 같은 Docker host에서 실행된다.

FastAPI provisioner는 Compose DNS로 연결한다.

```text
postgresql://heimdall_provisioner:...@managed-postgres:5432/postgres
```

Worker는 DB 접근 deployment마다 local Managed PostgreSQL container를 generation network에 직접
연결하고 `managed-postgres` alias를 만든다.

```text
Worker
-> docker network connect --alias managed-postgres
-> generation network
-> heimdall-managed-postgres container
```

Worker 시작 시 active generation network의 DB 연결을 복구하고, generation cleanup 시 DB container를
network에서 분리한다. 이 Docker daemon 결합은 DB가 다른 VM에 있으면 성립하지 않는다.

- Docker service DNS와 network alias는 VM 경계를 넘지 않는다.
- Worker는 외부 DB VM의 container lifecycle을 관리하면 안 된다.
- DB VM은 project generation cleanup과 독립적으로 살아야 한다.
- 기존 deployment snapshot과 running container의 `managed-postgres` 주소는 자동 변경되지 않는다.

## 목표 연결 구조

```text
Control/Runtime VM                          Managed DB VM
┌─────────────────────────────┐             ┌─────────────────────────┐
│ FastAPI provisioner         │ private TCP │ Managed PostgreSQL      │
│       └─────────────────────┼────────────▶│ provisioner role        │
│                             │             │ project database·role   │
│ Project application        │ private TCP │                         │
│       └─────────────────────┼────────────▶│ application role        │
└─────────────────────────────┘             └─────────────────────────┘
```

환경별로 다음 endpoint 설정만 바꾼다.

```text
HEIMDALL_PROJECT_DB_ADMIN_URL
HEIMDALL_PROJECT_DB_RUNTIME_HOST
HEIMDALL_PROJECT_DB_RUNTIME_PORT
```

Worker는 Managed PostgreSQL container 이름을 알지 못한다. project container는 Docker outbound NAT를
통해 외부 PostgreSQL TCP endpoint에 연결한다.

## 목표

- Managed PostgreSQL을 모든 환경에서 외부 TCP dependency로 통일한다.
- Worker의 Managed PostgreSQL Docker inspect/connect/disconnect와 startup restore를 제거한다.
- project database provisioning, project별 role과 password file 계약은 유지한다.
- `heimdall-python`의 sibling에 독립 `heimdall-managed-db` Compose project를 만든다.
- 두 Compose가 network를 공유하지 않는 상태에서 실제 provisioning과 application 배포를 검증한다.
- 재배포와 API/Worker 재시작 뒤 application data가 유지됨을 확인한다.
- 기존 local Managed PostgreSQL data와 Control DB의 managed database metadata는 초기화 가능한 것으로
  취급한다.
- 현재 단일 Docker runtime을 유지하면서 향후 Kubernetes 재배포에 필요한 storage 경계를 확보한다.

## 범위

- Managed PostgreSQL local container coupling 제거
- 외부 admin URL과 application runtime host/port 설정 검증
- 독립 Managed DB Compose, bootstrap과 named volume
- Control Compose에서 Managed PostgreSQL service와 dependency 제거
- Docker Desktop `host.docker.internal` 기반 외부 DB 재현
- project database provisioning과 application read/write persistence smoke
- Managed DB 장애 시 안정적인 provisioning error와 deployment failure 확인
- local/production 실행 문서와 설정 예제 갱신

## 비범위

- 기존 Managed PostgreSQL database·role·data migration
- Control PostgreSQL 별도 VM 이전 또는 HA
- Managed PostgreSQL replication, failover와 무중단 upgrade
- custom multi-Worker scheduler와 Worker placement
- Kubernetes runtime adapter
- 회원가입·권한, public domain과 TLS
- Secret Store 외부화와 password rotation
- application persistent volume과 Object Storage

## 데이터와 Secret 소유권

```text
Control PostgreSQL
├─ project와 deployment config
├─ Managed DB resource lifecycle metadata
└─ credential reference·version·fingerprint

Managed PostgreSQL
└─ project application data

Secret Store
└─ project DB password raw value
```

세 저장소 사이에 분산 transaction을 추가하지 않는다. 현재 provisioning의 intent/phase/CAS 기반
수렴 방식을 유지한다.

raw password는 다음 경계를 유지한다.

- Control PostgreSQL과 deployment snapshot에 저장하지 않는다.
- application environment에 직접 넣지 않는다.
- 개별 owner-only secret file을 read-only mount한다.
- provisioner admin credential을 project container에 전달하지 않는다.

기존 Managed DB를 폐기하면 Control DB의 `project_database_resources`와 연결된 credential metadata도
새 DB 실체와 맞지 않는다. local 전환 smoke는 새 Control DB/Secret Store에서 시작하거나 정확히 제한된
개발 metadata 초기화 절차를 사용한다. 코드가 시작 시 이를 자동 삭제하지는 않는다.

## Docker runtime 변경

다음 local DB 전용 계약을 제거한다.

- `HEIMDALL_MANAGED_DB_CONTAINER`
- candidate 시작의 `docker network connect`
- Worker startup의 active DB network restore
- cleanup/reconciliation의 DB container `disconnect`
- Managed PostgreSQL container Docker inspect

다음 계약은 유지한다.

- deployment별 service Docker network와 DNS alias
- `DATABASE_HOST`, `PORT`, `NAME`, `USER`, `SCHEMA`, `PASSWORD_FILE`
- project별 password file read-only mount
- gateway candidate/activation/last-known-good recovery
- exact Docker label과 cleanup fencing

## 독립 Managed DB 폴더

저장소 최상위에서 `heimdall-python`과 나란히 다음 구성을 둔다.

```text
heimdall_total/
├─ heimdall-python/
├─ heimdall-managed-db/
│  ├─ compose.yaml
│  ├─ init-managed-postgres.sh
│  ├─ .env.example
│  └─ README.md
└─ heimdall-test/
```

PostgreSQL data directory를 Git workspace에 직접 bind하지 않는다. local에서는 독립 Compose named
volume이 데이터를 소유한다.

```text
heimdall-managed-db-postgres
```

실제 DB VM에서는 전용 disk/volume을 PostgreSQL data path에 mount한 뒤 같은 Compose 계약을 사용할
수 있다. VM의 실제 mount path는 repository에 고정하지 않는다.

## 로컬 외부 VM 재현

```text
Compose A: heimdall-python-local
├─ control-postgres
├─ api
├─ worker
└─ frontend

Compose B: heimdall-managed-db
└─ managed-postgres
```

두 Compose는 network를 공유하지 않는다. Managed PostgreSQL은 local host port로만 노출한다.

```text
127.0.0.1:55433 -> managed-postgres:5432
```

Heimdall API, Worker와 project container는 다음 TCP endpoint를 사용한다.

```text
HEIMDALL_PROJECT_DB_ADMIN_URL=
  postgresql://heimdall_provisioner:...@host.docker.internal:55433/postgres
HEIMDALL_PROJECT_DB_RUNTIME_HOST=host.docker.internal
HEIMDALL_PROJECT_DB_RUNTIME_PORT=55433
```

Docker Desktop에서는 `host.docker.internal`을 사용하고 Linux VM 운영에서는
`managed-db.internal:5432`를 사용한다. project container는 Docker daemon이 직접 생성하므로 host
gateway name을 resolve할 수 있는지 release smoke에서 확인한다.

## 실제 DB VM network

```text
허용
Control/Runtime VM private IP -> Managed DB VM:5432

차단
Internet -> Managed DB VM:5432
기타 subnet -> Managed DB VM:5432
```

- PostgreSQL은 private interface에서만 connection을 받는다.
- `pg_hba.conf`는 Control/Runtime VM private IP만 허용한다.
- SCRAM password authentication을 사용한다.
- provisioner role과 project application role을 분리한다.
- project role은 자기 database에만 연결할 수 있다.
- DB VM disk, connection 수와 backup 실패를 관찰한다.
- PostgreSQL을 systemd 또는 Docker 중 무엇으로 실행하는지는 Heimdall TCP 계약에 영향을 주지 않는다.

## 수직 단계와 검증

### 1. Docker DB 결합 제거

- external endpoint config가 그대로 application snapshot에 들어가는 테스트를 보존한다.
- Managed DB container connect/restore/disconnect를 기대하는 테스트를 외부 TCP 계약으로 변경한다.
- candidate, cleanup, recovery, reconciliation과 Worker startup 어디에서도 Managed PostgreSQL container
  Docker command가 발생하지 않음을 검증한다.
- `DATABASE_*`와 password file mount가 유지됨을 확인한다.

안전한 중단 지점: 코드만 외부 endpoint를 사용하며 독립 PostgreSQL은 기존 임시 port로 제공할 수 있다.

### 2. 독립 Managed DB Compose

- sibling `heimdall-managed-db`에 PostgreSQL, bootstrap, health와 named volume을 구성한다.
- Control Compose에서 Managed PostgreSQL service, volume과 dependency를 제거한다.
- API와 Worker가 `host.docker.internal:55433`으로 provisioner 연결을 할 수 있게 한다.
- 두 Compose config와 PostgreSQL health를 검증한다.

안전한 중단 지점: Control DB와 runtime은 그대로이며 Managed DB Compose만 독립 lifecycle을 가진다.

### 3. Persistence release smoke

```text
외부 PostgreSQL 시작
-> project role/database/schema provision
-> DB 접근 project 배포
-> application row 생성·조회
-> 같은 project 재배포
-> 기존 row 재조회
-> API/Worker 재시작
-> application query 재검증
```

- Managed PostgreSQL container가 project generation network에 연결되지 않았음을 inspect한다.
- snapshot, event, Docker inspect와 log에 raw credential이 없는지 확인한다.
- DB 중지 시 provisioning/deployment가 bounded failure로 끝나는지 확인한다.

### 4. 문서와 전체 gate

- backend pytest·Ruff와 frontend verify를 실행한다.
- 두 Compose config, health, restart policy와 volume ownership을 확인한다.
- README, architecture, project profile과 product scope를 현재 외부 DB 계약으로 갱신한다.
- 실제 DB VM 적용 시 private DNS/port만 교체하면 되는지 설정을 대조한다.

## 인수 조건

- Heimdall Control Compose에 Managed PostgreSQL service/volume이 없다.
- Managed PostgreSQL은 독립 Compose project와 named volume에서 healthy하게 실행된다.
- Worker가 Managed PostgreSQL Docker container를 inspect/connect/disconnect하지 않는다.
- FastAPI가 network를 공유하지 않는 PostgreSQL에 project database·role·schema를 생성한다.
- project application이 외부 host/port와 password file로 로그인한다.
- 같은 project 재배포와 API/Worker 재시작 뒤 application data를 조회할 수 있다.
- DB 접근 service에만 `DATABASE_*`가 전달된다.
- project A role이 project B database에 접속할 수 없다.
- raw project password와 provisioner credential이 Control DB, deployment snapshot, API, event와 Docker
  environment에 남지 않는다.
- backend/frontend gate, Compose config와 실제 external DB smoke가 통과한다.

## Rollback과 안전 원칙

- 기존 Managed PostgreSQL data 폐기는 사용자가 승인했지만, destructive volume 삭제는 자동으로
  실행하지 않는다.
- 코드와 Compose에서 local DB 결합을 제거해도 기존 Docker volume은 운영자가 확인할 때까지 남긴다.
- Control PostgreSQL, runtime root와 Secret Store를 삭제하지 않는다.
- `docker compose down -v`, database reset과 broad Docker cleanup을 사용하지 않는다.
- 독립 DB 전환에 실패하면 기존 code revision과 기존 Managed DB volume을 사용해 수동 복구할 수 있다.
- smoke resource cleanup은 exact Compose project와 Heimdall labels로 제한한다.

## Kubernetes migration 여지

```text
exact commit
+ immutable deployment config
+ external Managed PostgreSQL
+ 복구 가능한 Secret
-> 새 runtime에서 재배포 가능
```

application data는 Managed PostgreSQL이 소유하고 Docker container filesystem은 영속 원본이 아니다.
향후 Kubernetes에서는 image registry, Deployment, Service, Ingress와 Secret adapter를 추가하되 동일한
Managed PostgreSQL endpoint를 연결할 수 있다.

## 후속 운영 로드맵

1. 회원가입·로그인, ADMIN/STUDENT 권한과 project ownership
2. student container CPU·memory·PID·disk·network 제한
3. NGINX public domain, TLS와 project hostname routing
4. Control PostgreSQL·Secret Store·Managed DB backup/restore runbook
5. 단일 Docker node 운영과 scale-up
6. 실제 확장 필요 시 image registry와 Kubernetes runtime 검토

커스텀 멀티 Worker scheduler, project placement와 Worker Agent는 현재 로드맵에서 제외한다.

## 문서 영향

- `README.md`: Control Compose와 독립 Managed DB 실행 순서·설정
- `project-docs/architecture.md`: 외부 PostgreSQL TCP data boundary
- `project-docs/project-profile.md`: Managed PostgreSQL의 별도 VM 소유권
- `project-docs/product-scope.md`: 외부 Managed DB 포함과 Kubernetes 비범위
- `heimdall-managed-db/README.md`: local 실행과 실제 VM volume/network 지침

## 구현 중 검증 기록

- 구현 전: 사용자 결정에 따라 local/external mode 분기를 제거하고 단일 외부 TCP 계약으로 Plan을
  갱신했다. 기존 Managed PostgreSQL data 초기화를 허용하되 destructive volume 삭제는 자동화하지
  않는 경계를 확정했다.
- 구현: Control Compose의 Managed PostgreSQL service·volume·dependency와 Worker의 DB container
  inspect/connect/disconnect/startup restore를 제거했다. sibling `heimdall-managed-db` Compose,
  bootstrap, health check와 독립 named volume을 추가했다.
- 정적·단위 검증: `git diff --check`, Backend Ruff format/check, 변경 핵심 pytest 36건과 두 Compose
  `config --quiet`가 통과했다.
- 전체 gate: Backend `143 passed, 12 skipped`, Frontend `9 files / 24 tests`, ESLint, TypeScript와 Vite
  production build가 통과했다.
- 실제 external DB smoke: Control Compose와 network를 공유하지 않는 healthy 독립 PostgreSQL을
  `127.0.0.1:55433`에서 실행했다. project 두 개의 database·role 격리와 cross-database login 거부,
  DB 접근 multi-service 배포, password file 계약, Docker environment/event/log secret 비노출,
  Managed DB container의 generation network 미연결을 확인했다. API/Worker 구성 객체를 새로 만든 뒤
  같은 project를 재배포하고 기존 row를 재조회했으며 `2 passed`였다.
- 보존: 기존 `heimdall-managed-postgres` container와 기존 volume, Control PostgreSQL, runtime root는
  삭제하거나 자동 전환하지 않았다. smoke용 독립 DB container는 테스트 전용 credential 노출 시간을
  줄이기 위해 검증 뒤 중지했으며 `heimdall-managed-db-postgres` volume은 삭제하지 않았다.
