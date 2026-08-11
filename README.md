# Heimdall Python

Public GitHub 저장소의 `main` commit을 단일 호스트 Docker preview로 배포하는 관리 도구다.

현재 구현 범위는 다음과 같다.

- Public HTTPS 저장소 등록과 `main` 검증
- `DRAFT -> READY` 프로젝트 설정
- multi-service, route, health check 설정
- service별 plain·secret 환경변수와 PostgreSQL 접근 선언
- 별도 Managed PostgreSQL의 project database·role 생성
- owner-only secret file과 non-secret DB 연결정보
- 최근 `main` commit 조회
- 최신 또는 특정 commit 배포 요청과 immutable 설정 snapshot
- PostgreSQL claim token·lease 기반 Worker와 재시작 회수
- exact SHA checkout과 multi-service Docker candidate
- plain 환경변수, user secret file, Managed DB password file 주입
- service health check와 project별 NGINX atomic activation
- generation 전환 시 동일 Preview 포트의 candidate network 기준 NGINX gateway 재생성·재검증
- 실패 시 last-known-good preview 보존과 candidate label cleanup
- 명시적으로 정지된 managed NGINX gateway의 다음 배포 시 stable preview port 복구
- durable cursor 기반 배포 event SSE, 실패 단계와 안정 preview link
- Worker 매개 service별 최근 container stdout·stderr 200줄 snapshot·SSE와 secret 마스킹
- 모든 프로젝트의 최근 배포 100건을 조회·필터링하는 전역 배포 활동 화면
- 밝은 화이트톤 관리 UI

preview port는 초기 범위에서 host의 `127.0.0.1`에만 공개한다. public domain, TLS와 multi-host routing은 아직 포함하지 않는다.

## 구조

```text
backend/       FastAPI backend
frontend/      React control UI
project-docs/  제품·아키텍처·구현 기준
infra/         로컬 외부 상태
```

## Local Docker Compose

기본 로컬 실행은 Docker Desktop의 통합 Compose를 사용한다. Control/Managed PostgreSQL, FastAPI API,
deployment Worker와 production frontend를 함께 시작하며 API·Worker·frontend는 종료 시 자동
재시작된다.

```bash
cp .env.example .env
# .env의 password와 HEIMDALL_RUNTIME_ROOT, HEIMDALL_GIT_WORKSPACE_ROOT를 설정한다.
docker compose --env-file .env -f infra/dev/compose.yaml up -d --build --wait
docker compose --env-file .env -f infra/dev/compose.yaml ps
```

- UI: `http://127.0.0.1:5173`
- API health: `http://127.0.0.1:8000/api/health`
- API·Worker·frontend와 DB 로그:
  `docker compose --env-file .env -f infra/dev/compose.yaml logs --follow`

Compose project 이름은 `heimdall-python-local`로 고정하며 기존 Control/Managed PostgreSQL named
volume을 재사용한다. `HEIMDALL_RUNTIME_ROOT`와 `HEIMDALL_GIT_WORKSPACE_ROOT`는 host와 Worker
container에서 같은 절대 경로로 bind해 Worker가 host Docker daemon에 넘기는 build·secret·NGINX
mount source를 유지한다. service-log Unix socket은 별도 `broker-sockets` volume에서 API와 Worker만
공유한다.

Docker socket은 Worker에만 mount한다. API와 frontend에는 전달하지 않는다. Worker는 Docker
Desktop의 `host.docker.internal`로 loopback publish된 candidate health와 stable preview를 검증한다.
Managed PostgreSQL container가 재생성돼 active generation network 연결이 사라지면 Worker 시작 시
Control DB의 exact active deployment/network label을 확인한 뒤 DB 사용 network의
`managed-postgres` alias만 복원한다.

개발 데이터를 유지하려면 `docker compose down -v`를 실행하지 않는다. Compose 실행 중에는 아래
host API·Worker·Vite 명령을 같은 포트에서 동시에 실행하지 않는다.

### 운영 중지와 재시작

잠시 중지할 때는 container와 network를 삭제하는 `down` 대신 `stop`을 사용한다.

```bash
# 배포된 project와 Managed PostgreSQL은 유지하고 Control Plane만 중지
docker compose --env-file .env -f infra/dev/compose.yaml \
  stop frontend api worker control-postgres

# 전체 local stack 중지. Managed PostgreSQL도 멈추므로 DB 사용 project는 그동안 실패한다.
docker compose --env-file .env -f infra/dev/compose.yaml stop

# 기존 container를 다시 시작하고 health를 기다린다.
docker compose --env-file .env -f infra/dev/compose.yaml up -d --wait
```

`docker compose ... down`은 Compose container와 기본 network를 삭제하지만 `-v`가 없으면 PostgreSQL
named volume은 보존한다. 다음 `up`에서 Managed PostgreSQL container가 재생성되면 Worker가 DB를
사용하는 exact active generation network 연결을 복원한다. `down -v`는 Control/Managed PostgreSQL
volume까지 삭제하므로 개발 데이터를 의도적으로 초기화할 때만 사용한다.

### 장애 영향

| 중단 대상 | 기존 배포 project | 관리 기능 |
| --- | --- | --- |
| frontend | Preview에는 영향 없음 | 관리 UI만 사용할 수 없음 |
| API | Preview에는 영향 없음 | API와 관리 UI를 사용할 수 없음 |
| Worker | 실행 중 service와 gateway는 유지 | 새 배포, reconciliation과 service log broker가 멈춤 |
| Control PostgreSQL | 실행 중 project는 Managed DB가 살아 있으면 유지 | API와 Worker가 상태를 읽거나 갱신할 수 없음 |
| Managed PostgreSQL | DB 미사용 project는 유지 | DB 사용 project의 application 요청이 실패 |
| Docker daemon | project service, gateway와 local Compose container가 함께 영향받음 | Docker 복구 뒤 기존 container의 restart policy에 따라 재시작 |

성공한 project service와 project별 NGINX gateway는 Control Plane과 별도 container이며
`unless-stopped` restart policy를 가진다. 따라서 API나 Worker process만 종료돼도 기존 Preview는
계속 동작한다. 현재 Managed PostgreSQL은 같은 local Compose가 소유하므로 Control Plane 전체와
Managed DB의 장애 영역은 완전히 분리돼 있지 않다.

## Host Backend 개발 (선택)

```bash
cd backend
set -a
source ../.env
set +a
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/ruff format --check .
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/uvicorn heimdall.main:app --reload
```

API와 별도 terminal에서 Worker를 실행한다.

```bash
cd backend
.venv/bin/heimdall-worker
# 또는 .venv/bin/python -m heimdall.worker
```

Worker만 Docker socket을 사용한다. API process와 배포 project container에는 Docker socket을 전달하지 않는다.
서비스 로그 조회도 API가 Docker를 직접 호출하지 않고 같은 `HEIMDALL_RUNTIME_ROOT`의 owner-only
`logs.sock`과 `log-stream.sock`을 통해 Worker에 요청한다. API와 Worker를 함께 실행해야 하며
Worker가 없으면 로그 조회만 stable `503`으로 실패하고 배포 처리 상태는 바뀌지 않는다. snapshot과
live stream은 각각 최대 4개 처리 슬롯을 사용해 장시간 SSE 연결이 수동 조회를 막지 않는다.
구조화 deployment event SSE도 최대 4개의 PostgreSQL LISTEN 연결만 사용해 API pool 8개 중 일반
요청용 연결을 남긴다.

## Host Frontend 개발 (선택)

```bash
cd frontend
pnpm install
pnpm verify
pnpm exec playwright install chromium
pnpm e2e
pnpm dev
```

`pnpm e2e`는 local Vite server와 mock API를 사용해 관리자 runtime 복구 화면을 실제
Chromium에서 검증한다.

## PostgreSQL data와 release smoke

통합 Compose는 Control PostgreSQL과 Managed Project PostgreSQL을 별도 volume으로 실행한다.
`.env`의 provisioner password와 `HEIMDALL_PROJECT_DB_ADMIN_URL` password는 같은 값이어야 한다.

실제 PostgreSQL·Docker·NGINX release smoke는 명시적으로 opt-in한다. 아래 URL의 password는
로컬 `.env`에 설정한 test 전용 값과 맞춰야 한다.

```bash
cd backend
export HEIMDALL_TEST_CONTROL_DB_URL='postgresql://heimdall:<control-password>@127.0.0.1:55432/heimdall_control'
export HEIMDALL_TEST_MANAGED_DB_ADMIN_URL='postgresql://heimdall_provisioner:<provisioner-password>@127.0.0.1:55433/postgres'
export HEIMDALL_TEST_MANAGED_DB_CONTAINER='heimdall-managed-postgres'
export HEIMDALL_TEST_PUBLIC_REPOSITORY_URL='https://github.com/CodingPenguin-yoon/heimdall-test'
export HEIMDALL_RUN_DOCKER_SMOKE='true'
.venv/bin/pytest tests/integration
```

Mac 로컬 테스트의 checkout, generated NGINX config와 secret file은 저장소의
`.heimdall-local/git`, `.heimdall-local/runtime` 아래에 모은다. 이 디렉터리는 전체가
Git에서 제외되며 PostgreSQL data는 계속 Compose named volume이 소유한다.

## Application database contract

프로젝트 코드는 DB 접근 service에서 다음 값을 읽어야 한다.

```text
DATABASE_HOST
DATABASE_PORT
DATABASE_NAME
DATABASE_USER
DATABASE_SCHEMA
DATABASE_PASSWORD_FILE
```

비밀번호는 `DATABASE_PASSWORD_FILE`이 가리키는 read-only file에서 읽는다. application schema와 table migration은 Alembic, Django migration 등 프로젝트 코드가 소유한다.

사용자가 `SECRET` kind로 `JWT_SECRET`을 설정하면 raw 값 대신 다음 file path가 환경변수에 전달된다.

```text
JWT_SECRET=/run/secrets/heimdall/environment/jwt_secret
```

프로젝트 코드는 해당 path의 read-only file을 읽는다. raw secret은 Control DB, deployment snapshot, API, event와 Docker environment에 저장하지 않는다.

## Runtime flow

```text
QUEUED
-> PREPARING: PostgreSQL job claim과 exact SHA checkout
-> BUILDING: service image build
-> STARTING: generation network와 candidate container
-> HEALTH_CHECKING: loopback service probe
-> ACTIVATING: nginx -t, atomic config replace, reload, route probe
-> SUCCEEDED
```

build, start, health 또는 activation이 실패하면 candidate resource만 정리하고 기존 active generation과 Managed PostgreSQL data는 유지한다. cleanup은 Heimdall label과 deployment ID가 모두 일치하는 정확한 resource만 대상으로 한다.

`GET /api/deployments/{deploymentId}/events`는 저장된 구조화 event snapshot을 반환한다.
active deployment의 UI는 마지막 event ID를
`GET /api/deployments/{deploymentId}/events/stream?after={eventId}`에 넘겨 이후 event를 SSE로
이어 받는다. insert transaction은 deployment UUID와 event ID만 PostgreSQL `NOTIFY`로 보내며 실제
event는 항상 Control DB에서 cursor 조회한다. 브라우저 재연결의 `Last-Event-ID`도 함께 반영하므로
notification이 유실되거나 연결이 잠시 끊겨도 저장된 event부터 복구한다. 배포가 terminal이면 남은
event를 보낸 뒤 stream을 닫는다.

`GET /api/deployments/{deploymentId}/service-logs?service={serviceName}`은 immutable snapshot의
service만 선택하고 deterministic container 이름과 managed·project·deployment label을 모두 확인한
뒤 최근 200줄을 조회한다. stdout과 stderr는 Docker timestamp 순으로 반환하고, Heimdall이 관리하는
project secret과 database password는 Worker에서 `[REDACTED]`로 바뀐 뒤에만 socket을 통과한다.
redaction 값을 준비하지 못하면 원문을 반환하지 않으며, 응답은 메모리에서만 처리하고 저장하지 않는다.
line 단위로 안전하게 치환할 수 없는 multiline·oversized secret도
`503 SERVICE_LOG_REDACTION_UNAVAILABLE`로 fail closed 한다.

`GET /api/deployments/{deploymentId}/service-logs/stream?service={serviceName}`은 같은 검증·redaction
경계를 사용해 최근 200줄부터 `docker logs --follow`의 새 출력을 SSE로 전달한다. 브라우저가
끊어지면 자동 재연결하며 새 session의 tail 200으로 화면 buffer를 교체한다. service 전환, HTTP
disconnect, Worker 종료와 container log 종료 시 해당 Docker follow process를 정리한다. line은
16KiB, 화면 buffer는 200줄로 제한하고 raw·redacted log 모두 저장하지 않는다. 기존 `새로고침`은
snapshot fallback으로 유지한다. 자동 스크롤 일시정지는 SSE 수집을 끊지 않으며 새 line 수를
표시하고, `최신 로그` 버튼으로 마지막 line 이동과 자동 추적을 함께 재개한다.

다음 배포에서 Worker는 managed·project·gateway label과 실제 running 상태를 함께 확인한다. 실행
중 gateway는 candidate route를 먼저 검증하고 candidate network를 주 네트워크로 동일 Preview
포트에 다시 생성해 host route를 재검증한다. exact managed gateway가 정지 상태면 그 전에 기존
active network의 last-known-good 상태로 먼저 복원한다. 이 확인이 끝난 뒤에만 DB active 전환과
이전 generation 회수를 수행하며, 실행 중 gateway와 label이 다른 동명 container는 자동 교체하지
않는다.

Worker가 activation 도중 종료돼 lease가 만료되면 새 Worker는 DB 기록만 믿고 candidate를
삭제하지 않는다. Control DB의 active deployment, NGINX가 응답하는 deployment ID와 Docker
label을 비교한다. 실제 target이 정상 서비스 중이면 남은 성공 기록만 완료하고, 이전
generation이 서비스 중임을 확인한 뒤에만 candidate를 다시 만든다. 상태를 확정할 수 없으면
candidate를 보존하며, 반복 crash는 `HEIMDALL_WORKER_MAX_ATTEMPTS` 상한 뒤 안정적인 recovery
failure로 종료한다.

## Preserved runtime reconciliation

`RECOVERY_STATE_UNCERTAIN`으로 끝난 deployment의 Docker resource는 즉시 삭제하지 않는다.
기본 72시간(`HEIMDALL_RUNTIME_RETENTION_HOURS`) 동안 보존한 뒤 Worker가 DB, NGINX marker와
exact Docker label을 다시 확인한다. target이 실제 active면 deployment를 성공으로 수렴시키고,
이전 generation이 안전하게 응답하면 target candidate만 정리한다. 여전히 불확실하면
`BLOCKED`로 남기며 자동 삭제하지 않는다.

관리 UI에서 보존 기간 전에도 안전 재확인을 요청할 수 있다. 강제 정리는 전체 Deployment ID를
확인값으로 입력해야 하며, Control DB가 active로 기록한 generation과 label이 일치하지 않는
resource는 삭제하지 않는다. API는 요청을 DB에만 기록하고 실제 Docker 작업은 lease를 획득한
Worker가 수행한다.
