# 아키텍처

## 시스템 구조

```text
Browser -> NGINX React UI -> FastAPI API -> Control PostgreSQL
                              |            |
                              |            +-> durable deployment job
                              |
                              +-> Managed PostgreSQL
                                  project별 database·role

Python Worker -> PostgreSQL claim/lease
              -> Git exact checkout
              -> Docker generation candidate
              -> project NGINX activation

FastAPI API -> owner-only Unix socket -> Worker snapshot log broker -> exact-label Docker logs
                                    -> Worker live log broker     -> exact-label Docker logs --follow
```

API와 Worker는 같은 Python package를 사용하지만 별도 command로 실행한다. Docker socket은 Worker만 사용한다.

API와 Worker가 동시에 시작될 수 있으므로 schema migration은 PostgreSQL advisory transaction lock으로 직렬화한다.

## 로컬 Compose 경계

Docker Desktop의 `infra/dev/compose.yaml`은 Control PostgreSQL, Managed PostgreSQL, FastAPI API,
deployment Worker와 NGINX frontend를 `heimdall-python-local` project로 함께 관리한다. PostgreSQL은
기존 named volume을 재사용하고 API·Worker·frontend와 두 DB에는 `restart: unless-stopped`를
적용한다.

API와 Worker는 같은 Backend image를 사용하지만 Docker socket은 Worker service에만 mount한다.
runtime과 Git workspace는 host Docker daemon도 source path를 해석할 수 있도록 `.env`의 host 절대
경로를 container 안의 같은 경로에 bind한다. API와 Worker의 owner-only service-log Unix socket은
runtime bind와 분리된 `broker-sockets` named volume을 사용한다. frontend는 정적 React build를
제공하고 `/api`를 API service로 proxy하며 SSE buffering을 끈다. 모든 host port는 계속
`127.0.0.1`에만 bind한다.

Compose Worker는 candidate health와 stable preview port를 `host.docker.internal`에서 probe한다.
host 직접 실행은 기본 `127.0.0.1` probe를 유지한다. Managed PostgreSQL container 재생성으로 active
generation network 연결이 사라질 수 있으므로 Worker 시작 시 Control DB active metadata,
deployment snapshot과 exact Docker network label을 대조한다. DB 접근 service가 있는 실제 active
network에만 `managed-postgres` alias를 복원하며 absent·conflict network는 변경하지 않는다.

### 장애와 재기동 경계

- API, Worker와 frontend는 Control Plane process다. 이들만 중단돼도 성공한 project service와
  project별 NGINX gateway container는 Docker에서 계속 실행된다.
- Worker가 중단되면 새 deployment, reconciliation과 service log broker가 멈춘다. 이미 활성화된
  Preview의 request path에는 Worker가 포함되지 않는다.
- Control PostgreSQL은 관리 metadata와 durable job의 원본이다. 중단 중에는 API와 Worker가 관리
  상태를 읽거나 변경할 수 없지만, 기존 project의 request path에는 포함되지 않는다.
- Managed PostgreSQL은 application data path에 포함된다. 현재 같은 local Compose가 소유하므로 전체
  stack을 멈추면 DB 접근 project는 service container가 살아 있어도 DB 요청에 실패한다.
- `compose stop`은 container와 network를 보존한다. `compose down`은 Compose container와 기본
  network를 삭제하지만 named volume은 보존하며, 다음 Worker 시작 시 active DB network 연결을
  복원한다. `down -v`는 두 PostgreSQL의 개발 데이터까지 삭제한다.

## 코드 책임 지도

### Backend entrypoint와 feature

```text
heimdall/
├── main.py
├── worker.py
├── config.py
├── database.py
├── api.py
├── common/
├── projects/
├── deployments/
├── project_database/
├── secrets/
├── git/
└── runtime/
```

| 경로 | 책임 |
| --- | --- |
| `backend/src/heimdall/main.py` | FastAPI lifespan에서 DB 연결과 repository/service를 조립하고 `/api` router를 등록한다. Docker에는 접근하지 않는다. |
| `backend/src/heimdall/worker.py` | 배포 Worker의 composition root다. 시작 시 active Managed DB network를 복원하고 deployment/reconciliation loop와 service-log broker를 실행한다. |
| `backend/src/heimdall/config.py` | `HEIMDALL_*` 환경변수를 검증해 API와 Worker의 공통 `Settings`를 만든다. host/Compose probe host와 broker socket root도 여기서 분리한다. |
| `backend/src/heimdall/database.py` | Control PostgreSQL pool, transaction과 schema migration을 소유한다. |
| `backend/src/heimdall/api.py` | project, deployment, project database와 runtime router를 하나의 `/api` 아래에 결합하고 health endpoint를 제공한다. |
| `backend/src/heimdall/projects/` | project 등록, 설정 version, service/route/environment 설정, 최근 Git commit 조회를 담당한다. |
| `backend/src/heimdall/deployments/` | immutable deployment snapshot, durable job/lease, 상태 전이, event·service-log API와 SSE를 담당한다. `deployments/worker.py`는 claim token을 가진 단일 deployment 실행을 제어한다. |
| `backend/src/heimdall/project_database/` | project별 database·role lifecycle과 Control DB metadata를 관리한다. 실제 PostgreSQL DDL은 `provisioner.py`에 격리한다. |
| `backend/src/heimdall/git/client.py` | public Git repository 관찰과 exact SHA checkout을 수행한다. |
| `backend/src/heimdall/secrets/store.py` | runtime root의 owner-only versioned secret file을 생성·조회하며 경로 탈출과 덮어쓰기를 차단한다. |
| `backend/src/heimdall/common/` | 공통 API model과 예외 응답 변환만 제공한다. |

각 feature는 필요한 `router`, `schemas`, `service`, `repository`, `models`를 소유한다. Router는 HTTP
변환과 dependency 조회만 하고, DB·Git·Docker 접근은 service 뒤의 repository 또는 adapter가
담당한다. 빈 계층과 일대일 전달 wrapper는 만들지 않는다.

### Runtime package

| 경로 | 책임 |
| --- | --- |
| `runtime/service.py` | 하나의 deployment를 `Git checkout → candidate 생성 → Gateway 활성화 → metadata 확정 → 이전 generation 정리` 순서로 조정한다. |
| `runtime/docker.py` | Docker image, generation network와 service container를 생성·관찰·정리한다. Managed DB network 연결과 active service restart policy도 담당한다. |
| `runtime/gateway.py` | project별 NGINX gateway 생성, candidate route 검증, network rebase, 동일 Preview port 재생성과 실패 복원을 담당한다. |
| `runtime/gateway_config.py`, `gateway_probe.py` | NGINX 설정 렌더링과 host route 관찰을 분리한다. |
| `runtime/repository.py`, `status.py`, `api.py` | `project_runtimes`의 active deployment/network/Preview port를 저장하고 관리자 조회 API로 제공한다. |
| `runtime/reconciliation*.py` | 불확실한 runtime을 durable request/claim으로 재관찰하고 보존·정리·활성 확정을 수행한다. |
| `runtime/docker_logs.py`, `logs.py` | exact deployment/service label을 검증한 뒤 bounded Docker log snapshot과 live stream을 만든다. |
| `runtime/log_broker.py`, `log_stream_broker.py` | Docker socket이 없는 API와 Worker 사이에서 snapshot/live log를 owner-only Unix socket으로 전달한다. |
| `runtime/process.py`, `process_stream.py` | timeout, heartbeat, bounded capture와 process-group 종료가 적용된 외부 명령 실행 adapter다. |
| `runtime/models.py` | deployment snapshot을 검증된 service, route, environment와 database runtime model로 변환한다. |

### Frontend와 container image

| 경로 | 책임 |
| --- | --- |
| `frontend/src/app/` | React 진입점, route와 공통 App shell을 소유한다. |
| `frontend/src/pages/` | project 목록·생성·상세·설정, deployment 활동·상세 화면을 route 단위로 조합한다. |
| `frontend/src/features/` | project 등록·설정·배포, database provisioning과 runtime reconciliation 같은 사용자 동작을 구현한다. |
| `frontend/src/entities/` | project/deployment/database/runtime API, query key, cache hook, type과 표시 model을 소유한다. |
| `frontend/src/shared/` | 공통 HTTP client, formatting, UI primitive와 token·layout·page CSS를 제공한다. |
| `backend/Dockerfile` | API와 Worker가 공유하는 Python image에 Git과 host Docker daemon 호환 CLI를 포함한다. |
| `frontend/Dockerfile`, `frontend/nginx.conf` | React production build를 NGINX로 제공하고 `/api`를 API service로 proxy하며 SSE buffering을 끈다. |
| `infra/dev/compose.yaml` | 두 PostgreSQL, API, Worker와 frontend의 local lifecycle, health, port, volume과 Docker socket 경계를 선언한다. |

### 주요 호출 흐름

1. 관리 API 요청은 `frontend shared client → feature/entity → FastAPI router → service → repository`로
   흐른다.
2. 배포 요청은 `deployments/router.py`가 snapshot과 durable job을 Control DB에 저장한다. Worker는
   job을 claim한 뒤 `DockerDeploymentProcessor → GitClient → DockerRuntime → NginxGatewayActivator`
   순서로 실행하고 성공 후 active runtime metadata를 확정한다.
3. Worker 재시작은 `project_runtimes`의 active row와 deployment snapshot을 읽고
   `DockerRuntime.restore_active_database_network`로 exact Managed DB network 연결만 복원한다.
4. 구조화 deployment event SSE는 Control PostgreSQL을 직접 구독한다. Service log 요청은 API의
   Unix broker client에서 Worker broker로 전달되고, Worker만 exact-label Docker logs를 실행한다.

## 설정 snapshot

`projects.deployment_config`는 service와 route 설정 전체를 JSONB aggregate로 저장한다. 배포 요청은 현재 config와 version을 `deployments.config_snapshot`에 복사한다. 진행 중 설정 변경은 이미 생성된 배포에 영향을 주지 않는다.

plain 환경변수는 snapshot에 값을 포함한다. secret 환경변수와 managed database credential은 logical reference·version·fingerprint만 포함하며 raw 값은 포함하지 않는다. DB 접근 deployment snapshot은 ACTIVE database의 identity와 non-secret connection metadata도 함께 고정한다.

## PostgreSQL 소유권

```text
Control PostgreSQL
├── projects와 deployment config
├── project environment secret metadata
├── deployment와 durable job
└── project database lifecycle metadata

Managed PostgreSQL
├── project A database + role
└── project B database + role

Runtime root
└── versioned owner-only raw secret files
```

두 PostgreSQL 사이에 분산 transaction을 만들지 않는다. Managed PostgreSQL DDL과 filesystem I/O는 Control DB transaction 밖에서 실행하고 각 단계 뒤 짧은 state-version CAS로 관찰 결과를 기록한다.

## 환경변수 합성 계약

- 사용자는 service별 plain·secret 환경변수를 설정한다.
- `DATABASE_*`, `HEIMDALL_*`는 예약 prefix라 사용자 override를 거부한다.
- `projectDatabaseAccess=true` service만 managed DB network와 `DATABASE_HOST`, `DATABASE_PORT`, `DATABASE_NAME`, `DATABASE_USER`, `DATABASE_SCHEMA`, `DATABASE_PASSWORD_FILE`을 받는다.
- raw password는 environment가 아니라 `/run/secrets/heimdall/project-database-password` read-only file로 전달한다.

## 배포 상태

```text
QUEUED -> PREPARING -> BUILDING -> STARTING -> HEALTH_CHECKING
                                                |
                                                +-> ACTIVATING -> SUCCEEDED
                                                +-> FAILED
```

project별 terminal 이전 deployment는 최대 하나다. PostgreSQL job row는 전달과 lease를 담당하고 deployment row가 제품 상태의 최종 원본이다.

매 claim은 새로운 UUID token을 발급한다. 상태 전이, lease renew, retry와 terminal write는 worker ID와 token이 모두 현재 row와 일치할 때만 허용된다. lease가 만료되면 `CLAIMED` job을 새 Worker가 회수하며 이전 Worker는 Control DB를 더 이상 갱신할 수 없다.

회수된 job은 candidate를 바로 다시 만들지 않는다. 먼저 Control DB의 active deployment,
project NGINX가 응답하는 `X-Heimdall-Deployment-Id`와 Docker resource의 exact deployment
label을 비교한다. 실제 target generation과 모든 service health·route가 정상이면 runtime
metadata와 terminal write만 마무리한다. 이전 generation이 응답할 때는 current config를
last-known-good로 맞춘 뒤에만 target candidate 재생성을 허용한다. gateway 또는 Docker
상태를 확정할 수 없으면 candidate를 삭제하지 않는다.

claim attempt는 process crash로 만료된 회수도 포함한다. 설정된 최대 attempt를 넘긴 job은
실제 generation을 마지막으로 재조정한 뒤 target이 active면 성공 처리하고, 안전한 이전
generation이면 candidate를 정리해 실패 처리하며, 불확실하면 resource를 보존한 recovery
failure로 종료한다.

`RECOVERY_STATE_UNCERTAIN` terminal deployment는 runtime reconciliation 대상이다. 기본 보존
기간 전에는 Docker mutation을 하지 않는다. 기간이 지나면 별도 `runtime_reconciliations` job이
생기며 deployment Worker가 idle일 때 claim token·lease를 가진 reconciliation Worker가 처리한다.
API는 job 요청만 저장하며 Docker socket은 계속 Worker만 소유한다.

safe reconciliation은 실제 target이 healthy·active면 runtime metadata와 deployment를
`SUCCEEDED`로 수렴시키고, 이전 generation이 실제 응답함을 확인하면 target candidate를
정리한다. 관찰이 불확실하면 `BLOCKED/UNCERTAIN`으로 보존한다. 관리자 force cleanup도 전체
deployment ID 확인, DB active guard, deterministic name과 managed·project·deployment exact label
검사를 통과해야 한다.

`deployment_events`는 Worker가 생성한 bounded message와 stable code만 저장한다. child process stderr와
application stdout, environment 원문을 event row에 저장하지 않는다. 일반 application stdout·stderr는
bounded snapshot과 SSE live follow 계약으로 제공하고 저장하지 않는다. API는 Docker socket 대신 runtime root의 `logs.sock`과
`log-stream.sock`에 연결하고, Worker가 immutable deployment snapshot의 service와 deterministic
container exact label을 다시 검증한 뒤 최근 200줄 또는 tail 200부터의 follow만 읽는다. 알려진
project secret과 managed database password는 Worker에서 fail-closed exact redaction한 뒤 전달하며
Docker timestamp가 삽입된 line 경계를 넘어 안전하게 exact 치환할 수 없는 multiline·oversized
secret은 로그 원문을 읽기 전에 redaction unavailable로 차단한다.

배포 실패 진단은 이 일반 로그 비저장 정책의 제한된 예외다. Worker는 NGINX rollback과 이전 Preview
복구를 먼저 수행하고, 실패한 새 container를 삭제하기 전에 command stdout/stderr와 service별 최근
200줄을 exact redaction한다. command/service artifact는 각각 최대 256KiB JSONB로
`deployment_diagnostic_artifacts`에 event·deployment·service와 연결해 기본 30일 저장한다. raw argv와
environment는 저장하지 않으며 redaction·Docker read 실패는 원문 없이 stable metadata로 남긴다.
diagnostic transaction 실패는 가용성 복구와 새 자원 cleanup을 막지 않는다. 목록 API는 payload를
제외하고 단일 artifact API만 bounded line을 반환하며 둘 다 `no-store`다.
배포 상세의 서비스 로그 영역은 진행 중이거나 성공한 배포에서는 live snapshot/SSE를 사용하고,
실패한 배포에서는 live 연결을 열지 않고 저장된 command/service artifact나 수집 실패 이유를 같은
자리에서 조회한다.

구조화 deployment event는 먼저 durable snapshot을 조회하고 active deployment 동안
`GET /api/deployments/{deploymentId}/events/stream` SSE로 이어 받는다. event insert transaction은
commit 시 deployment UUID와 event ID만 PostgreSQL `NOTIFY` payload로 보내고, API의 `LISTEN`
subscription은 이를 wake-up 신호로만 사용한다. 실제 전달 내용과 순서는 항상
`deployment_events.id > cursor` 조회가 결정하므로 notification 유실이나 EventSource 재연결에도
`Last-Event-ID` 뒤부터 복구한다. API DB pool 8개 중 일반 요청용 연결을 남기기 위해 LISTEN
subscription은 최대 4개이며, terminal 상태에서 남은 row를 모두 전달한 뒤 종료한다.

live broker는 snapshot broker와 별도 owner-only socket·최대 4개 capacity를 사용한다. stdout·stderr
reader는 bounded queue로 backpressure를 전달하고 5초 keepalive로 출력이 없는 disconnect도 감지한다.
API subscription close는 Worker socket close로, 다시 Docker follow process group terminate로 전파된다.
SSE 재연결은 durable cursor 대신 새 tail 200 session으로 UI buffer를 교체한다.
서비스 로그 화면의 일시정지는 stream 수집이 아니라 자동 스크롤만 멈춘다. 최근 200줄 buffer와
redaction은 계속 동작하고 새 line 수를 표시하며, 최신 로그 이동이나 service 전환 시 자동 추적을
다시 시작한다.

## Runtime generation

- 배포마다 전용 Docker network를 만든다.
- Worker 시작 시 DB 접근 active generation의 exact network에서 Managed PostgreSQL 연결이 빠졌으면
  `managed-postgres` alias를 복원한다.
- service alias는 `{service}-g-{generation}`처럼 generation별로 고유하다.
- project NGINX는 기존·candidate network에 잠시 함께 연결될 수 있다.
- 실행 중 project NGINX도 stored active network와 candidate network가 다르면 candidate route를
  1차 검증한 뒤 candidate network를 주 네트워크로 동일 Preview 포트에 재생성한다. 재생성 뒤 host
  route를 다시 검증한 다음에만 active metadata를 전환하고 이전 generation을 회수한다.
- 정지된 project NGINX는 managed·project·gateway exact label과 running 상태가 확인될 때만 다음
  배포에서 저장된 Preview 포트로 교체한다. 기존 active network에서 last-known-good를 1차
  복원한 뒤 candidate route를 검증하고, candidate network를 주 네트워크로 동일 포트에 다시
  생성해 재검증한 다음에만 이전 generation을 회수한다.
- 실행 중 gateway와 label이 다른 동명 container는 stopped gateway 복구 경로에서 제거하지 않는다.
- 새 설정은 `nginx -t`, atomic replace, reload, route probe와 network rebase 후 host route probe를
  통과해야 effective 상태가 된다.
- generated NGINX는 upstream의 같은 이름 header를 숨기고 실제 loaded generation의
  `X-Heimdall-Deployment-Id`를 응답해 process 재시작 후 관찰 기준을 제공한다.
- 실패하면 last-known-good config를 복구하고 candidate만 정리한다.
- `project_runtimes`는 project gateway, stable loopback preview port, active deployment·network·container·image 이름의 최종 원본이다.
- source workspace와 generated NGINX config는 Control DB 밖 runtime root에 둔다.
- health probe는 image 내부 도구를 요구하지 않도록 service port를 임시 loopback port에 publish해 수행한다.
- NGINX는 generation별 DNS alias를 사용하므로 old·candidate network에 동시에 연결돼도 upstream이 모호하지 않다.
- 성공 metadata commit 뒤 이전 generation을 정리하고, 실패 시 active metadata와 이전 generation을 보존한다.
- Docker cleanup 전에 managed label과 deployment ID를 다시 검사하며 이름만 일치하는 외부 resource는 변경하지 않는다.
- reconciliation cleanup은 삭제 전후 exact label resource를 관찰하며 Docker 명령 실패나 이름
  충돌을 정리 성공으로 기록하지 않는다.
- 일반 실패와 안전 판정이 끝난 reconciliation cleanup은 bounded diagnostic 저장을 먼저 시도한다.
  저장 실패도 cleanup을 막지 않지만 실제 active 또는 uncertain generation은 기존 guard대로 삭제하지
  않는다.
- service log broker socket parent는 owner-only이고 snapshot·live socket은 `0600`이다. frame 크기,
  동시 처리 수, Docker command timeout과 follow process lifecycle을 제한하며 socket이 안전하지 않으면
  해당 로그 broker만 비활성화하고 deployment Worker loop는 계속 실행한다.
