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

## Runtime generation

- 배포마다 전용 Docker network를 만든다.
- service alias는 `{service}-g-{generation}`처럼 generation별로 고유하다.
- project NGINX는 기존·candidate network에 잠시 함께 연결될 수 있다.
- 새 설정은 `nginx -t`, atomic replace, reload, route probe를 통과해야 effective 상태가 된다.
- 실패하면 last-known-good config를 복구하고 candidate만 정리한다.
