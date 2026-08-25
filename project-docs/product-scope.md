# 초기 제품 범위

## 첫 사용자 흐름

```text
Initialize the fixed administrator secrets
-> Sign in as admin through the HTTPS management hostname, or explicit same-host loopback HTTP mode
-> Public GitHub 저장소 등록
-> main 검증
-> service와 route 설정
-> 환경변수와 PostgreSQL 접근 service 설정
-> managed project database 생성
-> 최근 commit 확인
-> public subdomain 예약
-> 최신 또는 특정 commit 배포 요청
-> 상태와 로그 관찰
-> 안정 preview 접근
-> project HTTP public hostname 접근
```

## 포함

- One fixed `admin` account with Argon2id password verification and no user/session database tables
- Login, session lookup, and logout through a signed eight-hour cookie that is Secure by default and
  in production, HttpOnly, host-only, and `SameSite=Strict`; explicit loopback HTTP development uses
  the separate `heimdall-local-session` name without Secure
- Session-bound CSRF for unsafe management requests and default-deny authentication for all
  management APIs and SSE handshakes
- A public `/login` route with session-first rendering, deep-link restoration, logout, and `401`
  query-cache cleanup
- A browser-managed signed cookie with no password, returned session payload, or CSRF token stored in
  `localStorage` or `sessionStorage`
- Owner-only authentication files mounted read-only into the API service only
- Operator-provided HTTPS at the existing front Edge as the default and production management-login
  prerequisite; explicit `HEIMDALL_AUTH_COOKIE_SECURE=false` requires a `.localhost` management
  hostname and is limited to one consistent loopback browser hostname, while TLS and certificate
  operations remain outside the repository
- Public HTTPS GitHub repository
- 고정 `main` branch
- multi-service Dockerfile build
- service 내부 Docker DNS 통신
- path 기반 project gateway route
- Control Compose와 별도 lifecycle의 HTTP Edge NGINX와 고정 `heimdall-edge` network
- exact 관리 hostname, unknown hostname default `404`와 배포용 wildcard base domain
- project당 하나의 server-derived public hostname 예약·조회·비활성화
- desired/applied revision·`applied_hostname` snapshot과 durable Routing Worker
- Edge config test·atomic reload·probe·restore, claim token·lease·revision fencing과 startup reconciliation
- project gateway만 Edge network에 deterministic alias로 연결하는 hostname data path
- service별 plain·secret 환경변수
- 외부 TCP Managed PostgreSQL cluster와 project별 database·role
- DB 접근 service 전용 `DATABASE_*` 계약과 password file
- 수동 배포
- exact commit source rebuild
- 배포 상태, 이력, 실패 단계, 로그
- 구조화 deployment event snapshot·SSE와 계속 유지되는 loopback stable Preview link
- 불확실 runtime 보존, 자동 안전 재확인과 관리자 confirmed cleanup

## 초기 비범위

- Private Git과 SSH key
- GitLab과 provider API
- webhook과 자동 배포
- Compose file 직접 실행
- arbitrary branch/tag/SHA
- image registry와 image rollback
- Managed DB password rotation과 사람용 단기 DB credential
- project database backup·restore·purge 자동화
- volume/data rollback
- Kubernetes runtime과 multi-node scheduling
- Signup, multiple users, user management, RBAC, password recovery, and database-backed
  user/session state
- An administrator password-reset or rotation UI; operational rotation uses a new secret directory
  and an API restart
- custom domain, project당 복수 hostname과 path로 여러 project를 합치는 global route
- Repository-managed HTTPS listeners, TLS certificate placement, issuance, or renewal automation
- OCI Load Balancer·WAF, multi-node routing·failover와 cross-node project placement
- Authentication for public project hostnames and private Preview access
- application stdout/stderr 무제한 수집·장기 저장·검색·다운로드
