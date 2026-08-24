# Heimdall Python

Public GitHub 저장소의 `main` commit을 단일 호스트 Docker preview로 배포하고 project별 HTTP
hostname으로 공개하는 관리 도구다.

현재 구현 범위는 다음과 같다.

- One fixed `admin` account with Argon2id password verification, signed browser sessions, and
  session-bound CSRF protection for the management UI and API
- Public HTTPS 저장소 등록과 `main` 검증
- `DRAFT -> READY` 프로젝트 설정
- multi-service, route, health check 설정
- service별 plain·secret 환경변수와 PostgreSQL 접근 선언
- 별도 Managed PostgreSQL의 project database·role 생성
- owner-only secret file과 non-secret DB 연결정보
- 최근 `main` commit 조회
- 최신 또는 특정 commit 배포 요청과 immutable 설정 snapshot
- PostgreSQL claim token·lease 기반 deployment Worker와 재시작 회수
- exact SHA checkout과 multi-service Docker candidate
- plain 환경변수, user secret file, Managed DB password file 주입
- service health check와 project별 NGINX atomic activation
- generation 전환 시 동일 Preview 포트의 candidate network 기준 NGINX gateway 재생성·재검증
- 실패 시 last-known-good preview 보존과 candidate label cleanup
- 명시적으로 정지된 managed NGINX gateway의 다음 배포 시 stable preview port 복구
- project gateway의 생성·generation 전환·복구 시 고정 `heimdall-edge` network와 deterministic
  alias 유지
- 별도 lifecycle의 공용 Edge NGINX, exact 관리 hostname과 unknown hostname 기본 `404`
- project당 하나의 server-derived public hostname, 전체 hostname uniqueness와 desired/applied revision
- durable Routing Worker의 Edge config test·atomic replace·reload·hostname probe·startup reconciliation
- durable cursor 기반 배포 event SSE, 실패 단계와 안정 preview link
- Worker 매개 service별 최근 container stdout·stderr 200줄 snapshot·SSE와 secret 마스킹
- 실패 배포의 bounded command·service 진단 artifact와 기본 30일 보존·조회
- 모든 프로젝트의 최근 배포 100건을 조회·필터링하는 전역 배포 활동 화면
- 밝은 화이트톤 관리 UI

기존 stable preview port는 계속 host의 `127.0.0.1`에만 공개한다. public hostname은 Edge의 HTTP
listener를 통해 인증 없이 접근하며 Edge는 application container가 아니라 project gateway만
가리킨다. HTTPS/TLS와 wildcard certificate 발급·갱신 방식, custom domain은 아직 포함하지 않는다.

## 구조

```text
backend/       FastAPI backend
frontend/      React control UI
project-docs/  제품·아키텍처·구현 기준
infra/         로컬 외부 상태
```

## Single administrator authentication

Heimdall has exactly one fixed administrator named `admin`. It does not create user or session
tables and does not implement signup, user management, RBAC, or password recovery. The Backend is
the security boundary; the frontend route guard prevents protected management screens from
rendering before the current session is known.

The authentication API is:

```text
POST /api/auth/login
GET  /api/auth/session
POST /api/auth/logout
```

`/api/health` and the authentication entry points remain outside the management-router dependency.
Login verifies the stored Argon2id hash and creates an eight-hour signed
`__Host-heimdall-session` cookie. The cookie is `Secure`, `HttpOnly`, host-only,
`SameSite=Strict`, and scoped to `/`; it contains only the admin identity, absolute expiry, a CSRF
token, and a credential revision. Logout requires the exact session-bound `X-CSRF-Token`, clears
the cookie, and returns JSON. Every other management API, including SSE handshakes, requires a
valid session. `POST`, `PUT`, `PATCH`, and `DELETE` management requests also require the exact CSRF
token.

The public `/login` page checks the session before rendering management data, returns a successful
login to the original internal deep link, and distinguishes an unauthenticated response from an API
availability failure. The shared API client keeps the CSRF token in memory, sends same-origin
credentials, adds the CSRF header to unsafe methods, and clears both authentication state and the
query cache on `401`. The App shell exposes the current `admin` identity and logout on desktop and
mobile. The signed cookie is browser-managed; the frontend does not store the password, returned
session payload, or CSRF token in `localStorage` or `sessionStorage`.

Management login is supported only at `https://<management-hostname>/login`. The existing
operator-managed front Edge on the OCI VM must terminate HTTPS; certificate issuance, installation,
renewal, and the operator's Edge TLS configuration are outside this repository. The Secure cookie
intentionally makes the direct checked-in HTTP listener unsuitable for management login. Project
public hostnames and loopback Preview URLs remain unauthenticated and do not receive the host-only
management cookie.

### Initialize and rotate administrator secrets

Install the Backend package in the host virtual environment and initialize authentication before
starting Compose. The command reads the password twice with `getpass`; it never accepts the password
through an environment variable or command-line argument.

```bash
python3 -m venv backend/.venv
backend/.venv/bin/pip install -e backend
backend/.venv/bin/heimdall-admin-init /absolute/private/path/heimdall-auth
```

The target must be a new canonical, non-symlink absolute path outside the repository and disjoint
from the runtime, Git workspace, and Edge config roots. Initialization atomically creates a `0700`
directory with `0600` `admin-password.hash` and `session-signing.key` files and refuses symlink path
components, a target inside a Git worktree, or an existing target. Set
`HEIMDALL_AUTH_SECRET_ROOT` in `.env` to that exact unchanged host path. Control Compose mounts it
read-only at `/run/secrets/heimdall/auth` in the API service only. Compose derives the non-secret
API-only `HEIMDALL_AUTH_SECRET_SOURCE_ROOT` metadata from the same host setting; do not add a second
variable to `.env`. At startup the API rejects direct lexically equal, descendant, or ancestor
overlap with the runtime root, Git workspace root, or Edge config root. Docker resolves host
bind-source symlinks before the container starts, so the operator must not replace the initialized
directory with a symlink or configure an alias to it. The password, hash, and signing key are not
placed in Compose environment values, Docker inspect environment, Control PostgreSQL, logs, Git,
Workers, frontend, Edge, Managed DB, or application containers; rendered Compose and Docker mount
metadata expose only paths.

To rotate the administrator credential or signing key, initialize a different new directory, change
`HEIMDALL_AUTH_SECRET_ROOT` to the new path, and recreate the API so it loads the new read-only bind:

```bash
backend/.venv/bin/heimdall-admin-init /absolute/private/path/heimdall-auth-v2
# Update HEIMDALL_AUTH_SECRET_ROOT in .env, then recreate only the API service.
docker compose --env-file .env -f infra/dev/compose.yaml \
  up -d --no-deps --force-recreate api
```

Changing either credential invalidates existing sessions; the operator signs in again through the
HTTPS management hostname.

## Local Docker Compose

기본 로컬 실행은 Docker Desktop을 사용하되 Edge Gateway, Managed PostgreSQL과 Control Plane의
Compose lifecycle을 분리한다. Control frontend가 external `heimdall-edge` network에 연결되므로 Edge를
먼저 시작하고, Managed PostgreSQL과 Control PostgreSQL, FastAPI API, deployment Worker, Routing
Worker와 production frontend를 이어서 시작한다.

```bash
cd ../heimdall-python
cp .env.example .env
# Set passwords, runtime/Git/Edge absolute paths, management/deployment hostnames, and
# HEIMDALL_AUTH_SECRET_ROOT. Initialize that new auth directory before starting Compose.
# HEIMDALL_EDGE_CONFIG_ROOT로 지정한 owner-only host directory를 먼저 만든다.
backend/.venv/bin/heimdall-admin-init /absolute/private/path/heimdall-auth
docker compose --env-file .env -f infra/edge/compose.yaml up -d --wait

cd ../heimdall-managed-db
cp .env.example .env
# .env의 admin/provisioner password를 설정한다.
docker compose --env-file .env up -d --wait

cd ../heimdall-python
docker compose --env-file .env -f infra/dev/compose.yaml up -d --build --wait
docker compose --env-file .env -f infra/dev/compose.yaml ps
```

- UI: `http://127.0.0.1:5173`
- Management login: `https://<HEIMDALL_MANAGEMENT_HOSTNAME>/login` with username `admin`; use the
  logout action in the App shell to end the session
- API health: `http://127.0.0.1:8000/api/health`
- 기본 Edge HTTP listener: `http://127.0.0.1:8088`
- 기본 관리 route 확인:
  `curl --fail --header 'Host: heimdall.localhost' http://127.0.0.1:8088/`
- API·두 Worker·frontend와 Control DB 로그:
  `docker compose --env-file .env -f infra/dev/compose.yaml logs --follow`
- Edge 로그: `docker compose --env-file .env -f infra/edge/compose.yaml logs --follow`
- Managed DB 로그:
  `docker compose --env-file ../heimdall-managed-db/.env -f ../heimdall-managed-db/compose.yaml logs --follow`

Edge Compose project는 `heimdall-python-edge`이며 public request data path의 Edge container와 고정
network를 소유한다. Control Plane Compose project는 `heimdall-python-local`이고 Edge network를
external resource로만 사용한다. frontend는 `heimdall-control-frontend` alias로 Edge에 연결되고,
project gateway만 project별 deterministic alias로 추가 연결된다. application container는 generation
network에만 남는다. Routing Worker가 쓰는 `HEIMDALL_EDGE_CONFIG_ROOT`는 Edge container에 read-only로
mount된다.

Managed DB project 이름은 `heimdall-managed-db`다. Managed DB와 Control Plane은 Docker network를
공유하지 않고 TCP endpoint로만 통신한다. `HEIMDALL_RUNTIME_ROOT`와
`HEIMDALL_GIT_WORKSPACE_ROOT`는 host와 deployment Worker container에서 같은 절대 경로로 bind해 host
Docker daemon에 넘기는 build·secret·NGINX mount source를 유지한다. service-log Unix socket은 별도
`broker-sockets` volume에서 API와 deployment Worker만 공유한다.

`HEIMDALL_AUTH_SECRET_ROOT` is a canonical non-symlink host directory outside the repository and not
a child or ancestor of any Worker- or Edge-visible root. Control Compose passes only the fixed
container path to the API and mounts the directory there read-only. The API also receives the
derived host-source path as non-secret direct-overlap-check metadata. The deployment Worker, Routing
Worker, frontend, Edge, Managed DB, and application containers receive neither auth key nor this
mount.

Docker socket은 deployment Worker와 Routing Worker에만 mount한다. API, frontend와 application
container에는 전달하지 않는다. deployment Worker는 Docker Desktop의 `host.docker.internal`로
loopback publish된 candidate health와 stable preview를 검증하고, Routing Worker는 같은 host에서 Edge
HTTP listener를 probe한다. Routing Worker 환경은 Control DB와 Edge/routing 설정만 포함하며 사용하지
않는 Managed DB provisioner credential은 전달하지 않는다. API와 DB 접근 project container도 기본적으로
`host.docker.internal:55433`의 외부 Managed DB에 연결한다. 운영에서는 Managed DB VM의 private DNS와
`5432`로 바꾼다. 운영 Edge HTTP bind는 VM 방화벽 정책과 함께 명시적으로 설정한다.

개발 데이터를 유지하려면 `docker compose down -v`를 실행하지 않는다. Compose 실행 중에는 아래
host API·Worker·Vite 명령을 같은 포트에서 동시에 실행하지 않는다.

### 운영 중지와 재시작

잠시 중지할 때는 container와 network를 삭제하는 `down` 대신 `stop`을 사용한다.

```bash
# Control Plane만 중지한다. Edge, Managed DB와 배포된 project는 계속 실행된다.
docker compose --env-file .env -f infra/dev/compose.yaml stop

# Control Plane을 다시 시작하고 health를 기다린다.
docker compose --env-file .env -f infra/dev/compose.yaml up -d --wait

# Managed DB는 별도 lifecycle로 중지하거나 다시 시작한다.
docker compose --env-file ../heimdall-managed-db/.env \
  -f ../heimdall-managed-db/compose.yaml stop
docker compose --env-file ../heimdall-managed-db/.env \
  -f ../heimdall-managed-db/compose.yaml up -d --wait

# Edge 중지는 모든 public hostname을 중단하므로 의도한 점검 때만 별도로 수행한다.
docker compose --env-file .env -f infra/edge/compose.yaml stop
```

Control Compose의 `stop/down`은 Edge container·Edge network와 배포된 project runtime을 소유하지
않으므로 제거하지 않는다. Edge NGINX의 마지막 적용 config와 project gateway가 살아 있으면 기존
public URL과 loopback Preview는 Control 중단 중에도 data path를 유지한다. 각 DB Compose의 `down`은
자기 container와 network만 삭제하며 `-v`가 없으면 자기 PostgreSQL named volume을 보존한다. Control
Plane의 `down -v`는 Control DB만, Managed DB project의 `down -v`는 project application data만
삭제하므로 의도적인 데이터 초기화에만 사용한다.

### 장애 영향

| 중단 대상          | 기존 public route와 Preview                                        | 관리·변경 기능                                               |
| ------------------ | ------------------------------------------------------------------ | ------------------------------------------------------------ |
| frontend           | project public URL과 loopback Preview는 유지                       | 관리 hostname과 UI를 사용할 수 없음                          |
| API                | 마지막 적용 Edge config로 public URL 유지                          | API와 관리 UI를 사용할 수 없음                               |
| deployment Worker  | 실행 중 service·gateway와 public URL 유지                          | 새 배포, runtime reconciliation과 service log broker가 멈춤  |
| Routing Worker     | 마지막 적용 Edge config로 public URL 유지                          | hostname 변경이 `PENDING/APPLYING`에서 대기                  |
| Control PostgreSQL | Edge·project runtime·Managed DB가 살아 있으면 기존 요청 유지       | API와 두 Worker가 상태를 읽거나 갱신할 수 없음               |
| Edge Gateway       | loopback Preview는 유지되지만 모든 public hostname 중단            | Docker restart policy 복구 전 hostname 접근 불가             |
| Managed PostgreSQL | DB 미사용 project는 유지                                           | DB 사용 project의 application 요청이 실패                    |
| Docker daemon      | project service, gateway와 local Compose container가 함께 영향받음 | Docker 복구 뒤 기존 container의 restart policy에 따라 재시작 |

Edge Gateway, 성공한 project service와 project별 NGINX gateway는 Control Plane과 별도 container이며
`unless-stopped` restart policy를 가진다. 이미 적용된 hostname의 request path에는 API, 두 Worker와
Control PostgreSQL이 포함되지 않는다. Managed PostgreSQL도 별도 Compose/VM lifecycle이므로 Control
Plane을 중지하거나 재배포해도 application data path는 유지된다.

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
.venv/bin/uvicorn heimdall.main:create_app --factory --reload
```

For a host-run API, `source ../.env` must set `HEIMDALL_AUTH_SECRET_ROOT` to the absolute host
directory created by `heimdall-admin-init`. The Uvicorn factory loads the configured authentication
files and fails closed before serving if the directory or files are missing, linked, malformed, or do
not have the exact private modes. The two Worker processes do not read this authentication root.

API와 별도 terminal에서 deployment Worker와 Routing Worker를 실행한다.

```bash
cd backend
.venv/bin/heimdall-worker
# 또는 .venv/bin/python -m heimdall.worker

.venv/bin/heimdall-routing-worker
# 또는 .venv/bin/python -m heimdall.routing_worker
```

두 Worker만 Docker socket을 사용한다. API process와 frontend, 배포 project container에는 Docker
socket을 전달하지 않는다. deployment Worker는 project generation과 gateway를, Routing Worker는
exact-label Edge network·gateway와 generated route config를 관리한다.
서비스 로그 조회도 API가 Docker를 직접 호출하지 않고 같은 `HEIMDALL_RUNTIME_ROOT`의 owner-only
`logs.sock`과 `log-stream.sock`을 통해 deployment Worker에 요청한다. API와 deployment Worker를
함께 실행해야 하며 deployment Worker가 없으면 로그 조회만 stable `503`으로 실패하고 배포 처리
상태는 바뀌지 않는다. snapshot과
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

Control Plane과 Managed DB Compose는 각자의 PostgreSQL volume을 소유한다. 두 `.env`의 provisioner
password와 `HEIMDALL_PROJECT_DB_ADMIN_URL` password는 같은 값이어야 한다.

세 Compose의 정적 설정은 실제 `.env` 값으로 각각 확인한다.

```bash
docker compose --env-file .env -f infra/edge/compose.yaml config --quiet
docker compose --env-file .env -f infra/dev/compose.yaml config --quiet
docker compose --env-file ../heimdall-managed-db/.env \
  -f ../heimdall-managed-db/compose.yaml config --quiet
```

실제 PostgreSQL·Docker·NGINX release smoke는 명시적으로 opt-in한다. public hostname smoke는
`heimdall_routing_smoke_` 접두사의 새 전용 빈 DB만 허용하며 일반 `heimdall_control` DB를 거부한다.
Edge container와 network도 동일한 `heimdall.test-id` label을 가진 test-owned resource여야 한다.
아래 URL의 password는 로컬 test PostgreSQL 값과 맞춰야 한다.

```bash
cd backend
export HEIMDALL_TEST_ID='public-hostname-routing-<run-id>'
export HEIMDALL_TEST_CONTROL_DB_URL='postgresql://heimdall:<test-password>@127.0.0.1:<test-port>/heimdall_routing_smoke_<run_id>'
export HEIMDALL_TEST_MANAGED_DB_ADMIN_URL='postgresql://heimdall_provisioner:<provisioner-password>@127.0.0.1:55433/postgres'
export HEIMDALL_TEST_MANAGED_DB_RUNTIME_HOST='host.docker.internal'
export HEIMDALL_TEST_MANAGED_DB_RUNTIME_PORT='55433'
export HEIMDALL_TEST_PUBLIC_REPOSITORY_URL='https://github.com/CodingPenguin-yoon/heimdall-test'
export HEIMDALL_TEST_EDGE_NETWORK='heimdall-edge-smoke'
export HEIMDALL_TEST_EDGE_CONTAINER='heimdall-edge-gateway-smoke'
export HEIMDALL_TEST_EDGE_CONFIG_ROOT="/absolute/test-owned/path/$HEIMDALL_TEST_ID"
export HEIMDALL_TEST_EDGE_HOST='127.0.0.1'
export HEIMDALL_TEST_EDGE_PORT='18088'
export HEIMDALL_TEST_MANAGEMENT_HOSTNAME='control.routing-smoke.test'
export HEIMDALL_RUN_DOCKER_SMOKE='true'
install -d -m 700 "$HEIMDALL_TEST_EDGE_CONFIG_ROOT"
printf '%s\n' "$HEIMDALL_TEST_ID" \
  > "$HEIMDALL_TEST_EDGE_CONFIG_ROOT/.heimdall-routing-smoke-owner"
chmod 600 "$HEIMDALL_TEST_EDGE_CONFIG_ROOT/.heimdall-routing-smoke-owner"
export HEIMDALL_EDGE_TEST_ID="$HEIMDALL_TEST_ID"
export HEIMDALL_EDGE_CONFIG_ROOT="$HEIMDALL_TEST_EDGE_CONFIG_ROOT"
export HEIMDALL_EDGE_NETWORK_NAME="$HEIMDALL_TEST_EDGE_NETWORK"
export HEIMDALL_EDGE_CONTAINER_NAME="$HEIMDALL_TEST_EDGE_CONTAINER"
export HEIMDALL_EDGE_HTTP_PORT="$HEIMDALL_TEST_EDGE_PORT"
export HEIMDALL_MANAGEMENT_HOSTNAME="$HEIMDALL_TEST_MANAGEMENT_HOSTNAME"
docker compose --project-name "heimdall-routing-smoke-$HEIMDALL_TEST_ID" \
  -f ../infra/edge/compose.yaml up -d --wait
.venv/bin/pytest tests/integration
docker compose --project-name "heimdall-routing-smoke-$HEIMDALL_TEST_ID" \
  -f ../infra/edge/compose.yaml down
```

config root는 test id와 이름이 같은 새 전용 경로여야 하며 현재 사용자 소유 `0700` directory와 위의
exact owner marker만 있는 무라우트 상태로 시작한다. smoke는 실행 중 test Edge의
`/etc/nginx/routes`가 이 resolved 경로를 가리키는 read-only bind인지도 확인한다. 시작 전에 전용 DB가
비어 있는지 확인하고, 종료 시 생성한 project row와 exact-label gateway/generation network를 삭제한 뒤
Edge public route snapshot을 빈 상태로 되돌린다. 실패한 pytest 뒤에도 마지막 `docker compose down`을
실행해 test Edge lifecycle을 정리한다.

Mac 로컬 테스트의 checkout, project/Edge generated NGINX config와 runtime secret file은 저장소의
`.heimdall-local/git`, `.heimdall-local/runtime`, `.heimdall-local/edge` 아래에 모은다. 이
디렉터리는 전체가 Git에서 제외되며 PostgreSQL data는 각 Compose의 named volume이
소유한다. Administrator authentication files use the separate outside-repository path
described above.

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

build, start, health 또는 activation이 실패하면 기존 Preview 연결을 먼저 복구하고, 실패 command 출력과
존재하는 service의 최근 로그를 저장한 뒤 실패한 새 resource만 정리한다. 진단 저장 자체가 실패해도
cleanup과 `FAILED` 수렴은 계속한다. cleanup은 Heimdall label과 deployment ID가 모두 일치하는 정확한
resource만 대상으로 한다.

## Public hostname routing flow

프로젝트 상세 화면은 다음 단일 route API를 사용한다.

```text
GET    /api/projects/{projectId}/public-route
PUT    /api/projects/{projectId}/public-route   { "subdomain": "student-a" }
DELETE /api/projects/{projectId}/public-route
```

Backend는 lowercase subdomain label을 검증하고 `HEIMDALL_DEPLOYMENT_BASE_DOMAIN`과 결합해 hostname을
만든다. `admin`, `api`, `www`, 관리 hostname label과 운영자가 추가한 label은 예약되며 project당 한
route와 전체 hostname uniqueness를 DB constraint와 transaction guard로 함께 보장한다. public URL은
현재 `http://<server-derived-hostname>`이며 route가 `PENDING` 또는 `APPLYING`일 때 UI가 상태를
갱신한다.

같은 hostname PUT은 `PENDING/APPLYING/ACTIVE`에서 revision을 바꾸지 않는다.
`FAILED/UNCERTAIN`에서 같은 PUT으로 retry하면 새 revision 없이 현재 desired revision job만
다시 queue한다. gateway가 아직 없어서 대기한 job은 첫 성공 배포 또는 active runtime
reconciliation이 해당 project의 현재 `GATEWAY_START_FAILED` job만 즉시 깨운다.

```text
desired route와 revision 저장
-> Routing Worker claim token·lease
-> exact-label project gateway를 heimdall-edge network/alias에 연결
-> applied_hostname 기반 전체 route snapshot render
-> live Edge main·management config와 candidate route 전체 nginx -t
-> atomic config replace와 exact-label Edge reload
-> 관리 hostname과 변경 hostname probe
-> 최신 claim·revision일 때 ACTIVE/INACTIVE와 applied snapshot 확정
```

`applied_hostname`은 desired hostname 변경이나 disable 요청 중에도 Edge가 실제로 사용 중인 마지막
snapshot을 별도로 보존한다. 새 config test·reload·probe 또는 확실한 stale claim 거절은 이전 config를
복원하고 이전 applied hostname을 다시 확인한다. DB finalize 결과가 모호하면 candidate와 journal을
유지한 채 새 claim을 막고 canonical DB snapshot reconciliation을 먼저 수행한다. UI도 desired hostname과 실제
`applied_hostname`이 다르면 기존 적용 URL과 실패한 요청 URL을 구분해 표시한다. 오래된 claim은 lease
token과 desired revision fencing을 통과할 수 없으며, 실패한 desired 변경이 기존 valid hostname을
다른 project에 넘기지 못하도록 desired·applied hostname을 모두 충돌 검사한다. Routing Worker는 config
생성 전, 교체 전, probe 후 finalize 전에 최초 full applied snapshot을 다시 비교하며, 다른
claim이 바꾼 stale snapshot을 적용하지 않고 재시도한다.

candidate 교체 전에는 owner-only transaction journal을 file과 directory에 fsync하고 DB finalize 뒤에는
commit phase를 기록한다. reload 뒤 process가 강제 종료되면 다음 Routing Worker는 Control DB를 열기 전에
journal과 current config가 previous/candidate 중 하나인지 검증만 하며, DB가 unavailable인 동안에는 현재
valid config를 바꾸지 않는다. DB가 열리면 canonical applied snapshot을 적용하는 startup reconciliation이
finalize 전 crash는 previous로, finalize 뒤 crash는 candidate로 수렴시키고 journal을 정리한다. current가
journal의 previous/candidate 어느 쪽도 아니면 자동 덮어쓰지 않으며 reconciliation 성공 전 새 job도
처리하지 않는다.

Edge config는 hostname 순으로 결정적으로 생성되며 application generation을 직접 참조하지 않고
deterministic project gateway alias의 `8080`만 upstream으로 사용한다. unknown hostname과 deployment
base domain 밖 hostname은 default server의 `404`로 끝난다. project gateway가 새 generation으로
재생성되어도 Edge alias와 기존 loopback stable Preview port가 함께 유지된다.

현재 public hostname은 인증 없는 HTTP route다. Edge NGINX가 향후 TLS 종료 지점이지만 certificate
배치, wildcard certificate 발급·자동 갱신과 HTTPS listener는 구현하지 않았다.

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

`GET /api/deployments/{deploymentId}/diagnostics`는 실패 event와 연결된 command/service artifact
metadata를 반환하고, `GET /api/deployments/{deploymentId}/diagnostics/{artifactId}`는 선택한 bounded
line payload만 반환한다. artifact당 최대 256KiB, service당 최근 200줄이며 기본 30일
(`HEIMDALL_DIAGNOSTIC_RETENTION_DAYS`) 보존한다. 알려진 secret을 안전하게 가릴 수 없거나 container
로그를 읽지 못하면 원문 대신 수집 실패 이유만 저장한다. 배포 상세 화면은 실패한 배포의 `서비스
로그` 영역을 보존 모드로 전환하며, 이곳에서 event별 command/service artifact를 선택할 수 있다.

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
