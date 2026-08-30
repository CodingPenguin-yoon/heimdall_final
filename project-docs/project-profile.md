# 프로젝트 프로필

- 상태: `APPROVED`
- 기준일: `2026-08-24`
- 프로젝트명: `Heimdall Python`
- 목적: Public GitHub 저장소의 애플리케이션을 단일 호스트 Docker preview로 수동
  배포하고 project별 HTTP hostname, 상태·로그·이력을 관리한다.
- 사용자: 신뢰된 저장소를 관리하는 단일 관리자
- 형태: FastAPI backend와 React frontend를 함께 관리하는 monorepo
- 하네스 버전: `1.0.0`

## 기술 방향

- Backend: Python, FastAPI, Pydantic, psycopg
- Authentication: one fixed `admin`, Argon2id verification, an eight-hour signed cookie that is
  Secure by default and in production, an explicit loopback-only HTTP development mode, and
  session-bound CSRF enforced by the Backend
- Authentication secrets: a canonical non-symlink owner-only host directory outside the repository,
  mounted read-only into the API only; API-only host-source metadata rejects direct lexical overlap
  with runtime, Git, and Edge roots; no password, hash, or signing-key value appears in environment,
  Docker inspect environment, DB, logs, or Git
- Control DB와 deployment·public routing durable job queue: Control PostgreSQL
- Project application data: 별도 Managed PostgreSQL cluster의 project별 database·role
- Secret: repository 밖 runtime root의 versioned owner-only file, Control DB에는 reference·version·fingerprint만 저장
- Source: Git CLI, Public HTTPS, `main` 고정
- Runtime: Docker CLI, generation candidate, 프로젝트별 NGINX gateway, 고정 Edge network
- Public Edge: Control과 별도 lifecycle의 NGINX, HTTP hostname data path와 default `404`
- Local control plane: Docker Desktop base Compose와 Ubuntu Worker host-network override,
  API·deployment Worker·Routing Worker·NGINX frontend와 Control PostgreSQL volume
- Managed DB: 별도 Compose/VM lifecycle, private TCP endpoint와 독립 PostgreSQL volume
- Frontend: React, TypeScript, React Router, TanStack Query, CSS Modules
- 검증: pytest, Ruff, Vitest, Testing Library, Playwright

정확한 dependency version은 lockfile과 `pyproject.toml`/`package.json`에서 관리한다.

## 승인된 제품 규칙

- Heimdall authenticates exactly one fixed account named `admin`. Signup, multiple users, user
  management, RBAC, password recovery, and database-backed user/session state are not part of the
  product.
- `POST /api/auth/login`, `GET /api/auth/session`, and `POST /api/auth/logout` own the authentication
  contract. `/api/health` and auth bootstrap remain public entry points; all other management APIs
  and SSE handshakes require a valid admin session.
- By default and in production, the signed `__Host-heimdall-session` cookie has an eight-hour
  absolute lifetime and is `Secure`, `HttpOnly`, host-only, `SameSite=Strict`, and scoped to `/`.
  Explicit `HEIMDALL_AUTH_COOKIE_SECURE=false` uses `heimdall-local-session` without `Secure` only
  for same-host loopback HTTP development and is rejected unless the management hostname ends in
  `.localhost`. Its purpose-derived signing key prevents cross-mode cookie replay; every other
  cookie and CSRF property remains unchanged. Unsafe authenticated requests require the exact
  session-bound `X-CSRF-Token`.
- The frontend resolves the session before rendering protected routes, returns successful login to
  the original internal deep link, keeps CSRF state in memory, exposes desktop/mobile logout, and
  clears protected query state on `401`, including a `401` found by deduplicated session revalidation
  after an SSE connection error. The signed cookie is browser-managed; password, returned session
  payload, and CSRF token are not stored in `localStorage` or `sessionStorage`.
- `heimdall-admin-init` creates a new `0700` directory containing `0600` Argon2id hash and signing-key
  files outside the repository, rejecting Git-worktree targets, overwrite, and symlink path
  components. Control Compose mounts it read-only at `/run/secrets/heimdall/auth` in the API only and
  derives non-secret `HEIMDALL_AUTH_SECRET_SOURCE_ROOT` metadata from the same host path. API startup
  rejects direct overlap with runtime, Git workspace, and Edge config roots; both Workers, frontend,
  Edge, Managed DB, and application containers receive neither auth key nor the mount.
- 저장소 등록과 배포 설정을 분리한다.
- 프로젝트는 `DRAFT`, `READY` 또는 삭제 mutation을 차단하는 `DELETING` 상태다.
- 설정은 Heimdall DB가 원본이며 저장소에 전용 YAML을 요구하지 않는다.
- 배포는 `main` 최신 commit 또는 최근 목록에서 선택한 commit만 허용한다.
- 배포마다 source를 다시 build하며 image registry와 즉시 image rollback은 초기 비범위다.
- multi-service는 generation network의 고유 DNS alias로 통신한다.
- 프로젝트별 NGINX 하나가 안정 preview port를 소유한다.
- 모든 candidate가 정상일 때만 gateway를 전환하고 실패 시 기존 preview를 유지한다.
- 모든 generation 전환은 candidate route를 먼저 검증하고 project gateway를 candidate network의
  동일 preview port에 다시 생성·검증한 후에만 active metadata를 전환한다. exact managed label의
  정지 gateway는 그 전에 기존 active network의 last-known-good 상태로 1차 복원한다.
- deployment Worker는 PostgreSQL claim token과 lease로 fencing하며 만료된 작업은 새 Worker가 회수한다.
- 회수 deployment Worker는 DB·NGINX response marker·Docker deployment label을 비교해 실제 active
  generation을 확정하고, 불확실한 candidate는 삭제하지 않으며 attempt 상한을 적용한다.
- Docker resource는 project·deployment label과 deterministic generation name을 사용한다.
- cleanup은 label과 deployment ID가 일치하는 candidate와 이전 generation만 대상으로 한다.
- Project deletion API는 intent와 durable job만 기록한다. Docker socket과 runtime root를 가진 Worker가
  Edge hostname 제거 확인, Project Gateway, exact-label container·network·image, workspace·gateway
  config, project secret, 확인된 Managed PostgreSQL database·role 순으로 제거한 뒤 마지막 Control DB
  transaction에서 child metadata와 project row를 삭제한다.
- deletion claim token·lease와 phase가 stale Worker의 mutation/finalize를 fence한다. 이름만 일치하거나
  marker·label·filesystem identity가 불확실한 resource는 자동 삭제하지 않고 project와 job metadata를
  보존한다. Shared Edge container·network와 다른 project resource는 deletion 대상이 아니다.
- Managed DB 삭제가 활성화된 Worker는 시작 시 provisioner의 `CREATEDB`, `CREATEROLE`과
  `pg_signal_backend`의 `SET` privilege를 확인한다. session 종료 구간에서만 predefined role을
  활성화하고 반드시 reset한다. 권한이 부족하면 삭제 Worker를 포함한 process가 fail-fast하며 database
  session 종료나 drop을 시도하지 않는다.
- 별도 Managed DB Compose의 one-shot bootstrap은 신규·기존 volume 모두에서 provisioner role과 위
  privilege를 idempotent하게 reconcile한다. Main Worker는 Control DB 연결 전에 설정된 runtime/Git
  root와 고정 lock/gateway root만 비재귀적으로 현재 UID/GID의 `0700` directory로 준비한다. 실제 lock
  contention만 삭제 대기로 취급하고 unsafe filesystem 상태는 failed deletion으로 보존한다.
- 불확실한 failed candidate는 설정된 기간 동안 보존하고 durable reconciliation Worker가
  재확인한다. 자동 경로는 안전 판정이 없으면 삭제하지 않으며 관리자 force cleanup은 전체
  deployment ID 확인과 DB active guard를 요구한다.
- project runtime은 active deployment와 host loopback stable preview port를 Control DB에 저장한다.
- 관리용 exact hostname과 배포용 wildcard base domain은 서로 다른 domain이어야 한다.
  사용자는 subdomain label만 제출하고 Backend가 project당 하나의 hostname을 만든다.
- public hostname은 desired/applied revision을 분리하며 `applied_hostname`이 실제 Edge에 로드된
  마지막 snapshot을 보존한다. desired·applied hostname 둘 다 다른 project와 충돌하지 않아야 한다.
- Routing Worker는 PostgreSQL claim token·lease·desired revision으로 fencing하고 full applied
  snapshot의 config test·reload·probe·startup reconciliation을 담당한다. 실패하면 기존
  Edge config와 applied hostname을 보존하며 복구를 확정할 수 없을 때만 `UNCERTAIN`으로 남긴다.
- DB finalize 결과가 모호하면 candidate와 journal을 보존하고 canonical reconciliation이 성공하기 전
  새 routing claim을 처리하지 않는다. 확실한 stale claim 거절만 즉시 previous config로 복원한다.
- Edge candidate 교체 전 owner-only transaction journal을 fsync하고 DB finalize 뒤 commit phase를
  기록한다. Routing Worker 재시작은 Control DB 연결 전에 journal/current 일관성만 검증해 현재 valid
  config를 유지하고, DB 연결 뒤 canonical applied snapshot으로 finalize 전 crash는 previous, finalize 뒤
  crash는 candidate에 수렴시킨다. journal과 무관한 current config는 자동 덮어쓰지 않고 startup
  reconciliation 성공 전 새 job도 처리하지 않는다.
- gateway가 없어 대기한 route는 첫 성공 deployment 또는 active runtime reconciliation이 exact
  project의 현재 job만 깨우며, callback 실패 시 bounded timer retry를 유지한다.
- `heimdall-python-edge` Compose project와 고정 `heimdall-edge` network는 Control Compose와 별도
  lifecycle이다. Edge는 Control frontend와 project gateway만 proxy하고 application container를 직접
  참조하지 않는다.
- project gateway는 최초 생성, generation rebase, stopped gateway 복구와 rollback 모든 경로에서
  Edge network의 deterministic alias와 기존 loopback stable Preview port를 함께 유지한다.
- 로컬 Control Plane은 `heimdall-python-local` Compose project가 소유한다. Docker socket은
  deployment Worker와 Routing Worker에만 전달하고 API, frontend와 application container에는
  전달하지 않는다. API·deployment Worker log broker socket은 전용 named volume에서 공유한다.
  Managed PostgreSQL은 별도 `heimdall-managed-db` Compose project/VM이 소유하며 외부 TCP
  endpoint로만 연결한다.
- Edge, 성공한 project service와 project별 NGINX gateway는 Control Plane과 별도 container로
  실행한다. API·두 Worker·frontend 또는 Control PostgreSQL만 중단되면 기존 HTTP public URL과
  loopback Preview는 유지하지만 새 route·배포·관리 변경은 중단된다. Managed PostgreSQL
  lifecycle은 Control Plane과 분리되어 DB 사용 project의 application data path는 유지된다.
- Public project hostnames and loopback Preview remain unauthenticated. Default and production
  management login requires HTTPS at the existing operator-managed front Edge; certificate
  installation, issuance, renewal, and the operator's TLS configuration remain outside this
  repository. The explicit insecure-cookie mode is limited to loopback HTTP development and requires
  one consistent browser hostname.
- 전역 배포 활동은 기존 Deployment 공개 필드로 최근 100건을 최신순 조회하고, project 이름은 기존
  project 목록과 UI에서 결합한다. 진행 중 배포가 있을 때만 목록을 자동 갱신한다.
- 구조화 deployment event는 raw application output 없이 Control DB에 저장하고, 초기 snapshot 뒤
  PostgreSQL LISTEN/NOTIFY wake-up과 durable event ID cursor 기반 SSE로 전달한다. 별도 서비스 로그
  snapshot과 SSE live follow는 deployment Worker가 exact container label을 확인하고 알려진 secret을 fail-closed
  마스킹한 뒤 service당 최근 200줄 buffer로 제공한다. 일반 snapshot/live stream은 저장하지 않는다.
  배포 실패에 한해서 cleanup 전에 command 출력과 service별 최근 로그를 256KiB artifact로 제한해
  별도 Control DB table에 기본 30일 저장한다. 마스킹을 준비하지 못하면 원문 대신 stable 수집 실패
  metadata만 남긴다. snapshot과 stream capacity를 분리하고 disconnect 시 Docker follow를 정리한다.
  배포 상세의 단일 서비스 로그 영역은 일반 배포에서는 live stream을, 실패 배포에서는 저장된
  command/service artifact 또는 수집 실패 이유를 보여준다. live 로그 일시정지는 stream을 끊지 않고
  자동 스크롤만 멈추며 새 line 수를 표시한다.
- service별 사용자 환경변수와 project database 접근 여부를 설정한다.
- 사용자 환경변수와 Heimdall 예약 `DATABASE_*`, `HEIMDALL_*` 값을 배포 시 합성한다.
- project database password와 사용자 secret은 Heimdall이 관리하며 API·Control DB·deployment snapshot에 raw 값을 남기지 않는다.
- user secret kind 환경변수에는 raw 값이 아니라 `/run/secrets/heimdall/environment/<name>` file path를 전달한다.
