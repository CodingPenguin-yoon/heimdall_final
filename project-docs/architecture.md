# 아키텍처

## 시스템 구조

```text
Browser -> React UI -> FastAPI API -> Control PostgreSQL
                              |            |
                              |            +-> durable deployment job
                              |
                              +-> Managed PostgreSQL
                                  project별 database·role

Python Worker -> PostgreSQL claim/lease
              -> Git exact checkout
              -> Docker generation candidate
              -> project NGINX activation
```

API와 Worker는 같은 Python package를 사용하지만 별도 command로 실행한다. Docker socket은 Worker만 사용한다.

API와 Worker가 동시에 시작될 수 있으므로 schema migration은 PostgreSQL advisory transaction lock으로 직렬화한다.

## Backend 모듈

```text
heimdall/
├── main.py
├── config.py
├── api.py
├── common/
├── auth/
├── projects/
├── deployments/
├── project_database/
├── secrets/
├── git/
└── runtime/
```

각 feature는 필요한 `router`, `schemas`, `service`, `repository`, `models`만 가진다. 빈 계층과 일대일 전달 wrapper는 만들지 않는다.

`database.py`는 Control PostgreSQL 연결과 migration만 소유한다. `project_database`는 managed cluster의 database·role lifecycle을 소유하고 `secrets`는 repository 밖 credential file adapter를 제공한다.

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

`deployment_events`는 Worker가 생성한 bounded message와 stable code만 저장한다. child process stderr와 application stdout, environment 원문은 저장하지 않는다.

## Runtime generation

- 배포마다 전용 Docker network를 만든다.
- service alias는 `{service}-g-{generation}`처럼 generation별로 고유하다.
- project NGINX는 기존·candidate network에 잠시 함께 연결될 수 있다.
- 새 설정은 `nginx -t`, atomic replace, reload, route probe를 통과해야 effective 상태가 된다.
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
