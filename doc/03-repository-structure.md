# 3. 저장소와 코드 구조

## 최상위 폴더

```text
heimdall_final/
├── backend/        FastAPI, Worker, DB migration, Docker/Git/NGINX 제어
├── frontend/       React 관리자 화면
├── infra/          Docker Compose와 NGINX 실행 설정
├── project-docs/   개발자용 제품·아키텍처·계획 문서
├── doc/            운영자가 읽는 쉬운 설명서
├── .agent-harness/ 작업 절차와 검증 규칙
├── .agents/        프로젝트 전용 자동화 skill
├── .env.example    환경변수 예시
└── README.md       프로젝트 소개와 실행 진입점
```

## Backend 구조

Backend는 기능별 세로 모듈로 나뉜다.

```text
backend/src/heimdall/
├── auth/                 고정 관리자 로그인과 secret
├── projects/             프로젝트 등록과 배포 설정
├── deployments/          배포 요청, 상태, queue, event
├── project_database/     Managed DB 생성과 metadata
├── public_routes/        public hostname desired/applied 상태
├── runtime/              Docker, Gateway, Edge, log, 복구
├── git/                  public Git repository 접근
├── secrets/              프로젝트 secret file 저장
├── migrations/           Control DB schema
├── api.py                공용 health와 관리 router 조합
├── main.py               API application 시작
├── worker.py             Deployment Worker 조립과 실행
├── routing_worker.py     Routing Worker 조립과 실행
├── config.py             환경변수 읽기와 검증
└── database.py           PostgreSQL connection과 migration
```

### 기능 모듈의 공통 층

대부분의 기능은 다음 책임으로 나뉜다.

```text
HTTP 요청
-> router
-> service
-> repository 또는 external adapter
```

| 층         | 책임                                          | 하지 말아야 할 일             |
| ---------- | --------------------------------------------- | ----------------------------- |
| router     | HTTP 입력·출력 변환, dependency 연결          | Docker/Git/DB SQL 직접 실행   |
| service    | 업무 규칙, 상태 전이, 여러 동작 조정          | HTTP 세부사항에 의존          |
| repository | 자기 기능의 Control DB 저장 상태              | 다른 기능의 table을 임의 조작 |
| adapter    | Git, Docker, NGINX, filesystem 같은 외부 효과 | 제품 상태를 독자적으로 결정   |

### `projects/`

프로젝트 등록과 최신 편집 설정을 소유한다. Git URL, 서비스, route, environment, DB 접근 여부를
검증한다. 프로젝트는 대략 `DRAFT` 또는 `READY`로 이해하면 된다.

### `deployments/`

배포 요청과 immutable snapshot, job queue, 상태, event, 실패 diagnostics를 소유한다. Worker가 작업을
안전하게 claim하고 retry/fail/succeed 처리하는 규칙도 여기에 있다.

### `runtime/`

실제 Docker와 NGINX 외부 효과를 소유하는 핵심 패키지다.

| 파일                   | 쉬운 설명                                                  |
| ---------------------- | ---------------------------------------------------------- |
| `docker.py`            | image, generation network, service container, health probe |
| `gateway.py`           | 프로젝트별 NGINX Gateway 생성·전환·복구                    |
| `gateway_config.py`    | Project Gateway NGINX 설정 생성                            |
| `edge.py`              | Shared Edge 설정 test·replace·reload·복구                  |
| `edge_network.py`      | Project Gateway를 Edge network에 연결                      |
| `repository.py`        | 현재 active runtime metadata 저장                          |
| `service.py`           | checkout 이후 candidate와 activation 흐름 조정             |
| `reconciliation_*`     | 중단된 배포와 실제 Docker 상태 재확인                      |
| `docker_logs.py`       | 정확한 container label 확인 후 로그 조회                   |
| `log_broker.py`        | API와 Worker 사이 snapshot log 전달                        |
| `log_stream_broker.py` | live log stream 전달                                       |

### `public_routes/`

public hostname의 원하는 상태와 실제 적용 상태, routing job을 소유한다. Docker/NGINX 구현은 직접
갖지 않고 `runtime` adapter를 사용한다.

### `project_database/`

Managed PostgreSQL에 project별 role, database, schema를 만들고 Control DB에는 metadata만 저장한다.
애플리케이션 row를 읽거나 관리하지 않는다.

### `auth/`와 `secrets/`

두 종류의 secret 책임을 분리한다.

- `auth/`: Heimdall 관리자 password hash와 session signing key
- `secrets/`: 배포할 프로젝트가 사용하는 환경변수·DB credential file

raw secret을 Control DB, API response, Git 설정, 로그에 넣지 않는 것이 기본 원칙이다.

## Frontend 구조

```text
frontend/src/
├── app/       React 시작점, router, auth gate, 공통 shell
├── pages/     URL 단위 화면 조립
├── features/  사용자가 수행하는 기능 단위 UI
├── entities/  API client, query, type, 표시 model
└── shared/    공용 HTTP client, style, UI, formatting
```

### `app/`

전체 route tree와 로그인 상태를 관리한다. 관리 화면을 그리기 전에 현재 admin session을 확인한다.

### `pages/`

사용자가 접하는 페이지 단위다.

- 로그인
- 프로젝트 목록과 생성
- 프로젝트 상세와 설정
- 배포 활동과 배포 상세

page는 여러 feature와 entity를 조합한다.

### `features/`

사용자 행동 단위다.

- 프로젝트 등록
- 배포 설정
- 배포 실행
- DB 생성
- public route 설정
- runtime reconciliation

### `entities/`

Backend resource별 API와 TanStack Query 상태를 소유한다. project, deployment, runtime, database,
public route가 각각 분리되어 있다.

### `shared/`

credential과 CSRF를 포함하는 공용 API client, 공용 UI, style token과 formatting이 있다. 인증이
만료되어 `401`이 발생하면 보호된 query cache도 함께 비운다.

## Infra 구조

```text
infra/
├── dev/
│   ├── compose.yaml        Control Plane 기본 Compose
│   └── compose.linux.yaml Ubuntu Worker host-network override
└── edge/
    ├── compose.yaml        Shared Edge 독립 lifecycle
    ├── nginx.conf          Edge 기본 NGINX 설정
    └── management.conf.template
```

Control Compose는 Edge나 project runtime을 소유하지 않는다. 따라서 Control Compose를 stop/down해도
이미 실행 중인 Project Gateway와 application container를 자동으로 제거하지 않는다.

## 문서 구조

- `README.md`: 사용자와 개발자의 첫 진입점
- `doc/`: 쉬운 운영자 설명서
- `project-docs/product-scope.md`: 현재 포함·비범위
- `project-docs/project-profile.md`: 승인된 기술 방향과 제품 규칙
- `project-docs/architecture.md`: 정확한 아키텍처 계약
- `project-docs/plans/`: 고위험 변경의 결정과 검증 기록

## 무엇을 바꿀 때 어디를 보면 되는가

| 하고 싶은 일                    | 먼저 볼 위치                                        |
| ------------------------------- | --------------------------------------------------- |
| 프로젝트 설정 API 변경          | `projects/`와 frontend configure feature            |
| 배포 상태·retry 변경            | `deployments/worker.py`, repository, 관련 migration |
| Docker container 생성 방식 변경 | `runtime/docker.py`                                 |
| Preview/Gateway 전환 변경       | `runtime/gateway.py`, `gateway_config.py`           |
| public hostname 변경            | `public_routes/`, `runtime/edge.py`                 |
| 로그인 정책 변경                | `auth/`, `main.py`, frontend admin-auth             |
| Compose network 변경            | `infra/`, `config.py`, architecture 문서            |
| Control DB schema 변경          | `migrations/`와 해당 feature repository             |
| 화면 변경                       | `pages/`, `features/`, `entities/` 순서로 확인      |
