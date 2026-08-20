# Public Hostname Routing Plan

- 상태: `APPROVED`
- 날짜: `2026-08-20`
- 승인 근거: 사용자가 현재 단일 OCI Runtime VM과 project별 NGINX gateway 구조를 유지하면서,
  관리용 도메인과 배포용 wildcard 도메인을 분리하고 공용 Edge Gateway와 별도 Routing Worker를
  추가하는 방향으로 문서화와 후속 구현을 승인함

## 결정 요약

```text
관리용 도메인
Browser -> OCI public IP -> Heimdall Edge Gateway -> Control frontend -> FastAPI API

배포용 wildcard 도메인
Browser -> *.deployment.example -> OCI public IP -> Heimdall Edge Gateway
                                             -> project NGINX gateway
                                             -> active generation service

Control Plane
FastAPI API -> Control PostgreSQL에 desired public route 저장
Routing Worker -> route job claim/lease -> Edge config test·atomic reload·probe
Deployment Worker -> project gateway와 generation 전환·Edge network 연결
```

OCI Load Balancer는 두지 않는다. 단일 Runtime VM에서 Load Balancer를 추가해도 VM 장애를 제거하지
못하고 운영 계층만 늘어나기 때문이다. 배포용 wildcard DNS는 OCI Runtime VM의 고정 public IP를
가리키고, 공용 Edge Gateway가 `Host`를 기준으로 관리 화면과 project route를 구분한다.

Edge Gateway는 실제 request data path이고 Routing Worker는 config를 관리하는 control-plane
process다. Routing Worker가 중단돼도 이미 적용된 Edge config와 project gateway는 계속 요청을
처리한다.

## 확인된 현재 동작

- Control Compose는 Control PostgreSQL, FastAPI API, deployment Worker와 production frontend를
  `heimdall-python-local` lifecycle로 관리한다.
- API는 Docker socket을 갖지 않으며 deployment Worker만 Docker·NGINX external effect를 실행한다.
- project별 NGINX gateway는 deterministic container name과 exact managed/project/kind label을 가진다.
- project gateway는 active generation network를 주 네트워크로 사용하고 `127.0.0.1`의 stable
  Preview port를 `8080/tcp`에 publish한다.
- generation 전환 시 candidate route를 검증한 뒤 gateway를 candidate network의 주 네트워크와
  동일 Preview port로 재생성하고 다시 probe한다.
- 성공한 project service와 gateway는 `unless-stopped`로 실행되므로 API·Worker·frontend가
  중단돼도 기존 loopback Preview는 유지된다.
- public domain, TLS와 multi-host routing은 현재 구현 범위에 없다.

## 문제

OCI DNS에서 배포용 wildcard domain을 Runtime VM으로 보내더라도 현재 host의 public `80/443`을
소유하고 `Host`별 project gateway를 선택하는 ingress가 없다. 사용자가 원하는 subdomain과 project
runtime의 관계를 저장하거나 충돌을 막는 Control DB 계약도 없고, public route 설정을 검증·반영·복구할
durable Worker도 없다.

기존 stable Preview port를 public으로 직접 열면 project 수만큼 방화벽과 port를 노출해야 하고,
hostname·TLS·default deny를 중앙에서 관리할 수 없다. Edge가 application container를 직접 선택하면
현재 project gateway가 소유하는 atomic generation 전환과 last-known-good 복구도 우회하게 된다.

## 목표

- 관리용 exact hostname과 배포용 wildcard hostname을 하나의 공용 Edge Gateway에서 수신한다.
- 사용자는 임의 full hostname이 아니라 배포용 base domain 아래의 subdomain label 하나를 요청한다.
- Control PostgreSQL이 project별 desired hostname, 적용 revision과 공개 상태의 최종 원본이 된다.
- 공용 Edge는 hostname을 deterministic project gateway로만 전달하고 application container나
  generation을 직접 선택하지 않는다.
- Routing Worker가 durable claim·lease와 revision fencing으로 config를 생성·검증·반영·probe한다.
- project gateway 재생성 시에도 Edge network 연결과 alias가 유지된다.
- API·Routing Worker·Deployment Worker 또는 Control PostgreSQL이 중단돼도 이미 적용된 public URL은
  계속 동작한다.
- route 적용 실패는 기존 Edge config와 이미 활성인 project runtime을 보존한다.

## 범위

- 별도 lifecycle의 Edge NGINX와 고정 `heimdall-edge` Docker network
- 관리용 exact hostname의 고정 frontend route
- 배포용 wildcard base domain 아래 project별 hostname route
- project당 public hostname 하나와 전체 hostname uniqueness
- public route 설정·조회·비활성화 API와 관리 UI
- desired route, applied revision, 상태와 bounded error metadata
- PostgreSQL claim token·lease 기반 Routing Worker
- 전체 public route snapshot의 deterministic NGINX config render, `nginx -t`, atomic replace와 reload
- exact managed project gateway의 Edge network 연결과 deterministic alias
- hostname route probe와 Worker startup reconciliation
- 기존 active gateway의 초기 Edge network 편입
- 로컬 hosts/DNS 대체 smoke와 OCI wildcard DNS 운영 문서

## 비범위

- OCI Load Balancer, WAF와 multi-node routing
- Runtime VM 자동 failover와 cross-node project placement
- custom domain과 사용자가 소유한 certificate
- project당 복수 hostname
- path 기반으로 서로 다른 project를 합치는 global route
- 회원가입, 로그인, ADMIN/STUDENT 역할과 project ownership
- 공개 Preview 접근 인증과 private Preview
- container CPU·memory·PID·disk·network quota
- application generation 선택 또는 deployment orchestration 변경
- 자동 DNS record 생성; wildcard record는 운영자가 한 번 구성한다.
- wildcard certificate 발급·갱신 자동화 방식 확정

회원·권한과 tenant runtime 제한은 별도 Plan에서 다룬다. 이 Plan의 service 경계는 후속 인증이 추가되면
public route 변경 권한을 project ownership 검사에 연결할 수 있어야 하지만, 현재 단일 관리자 계약을
미리 다중 사용자 schema로 확장하지 않는다.

## 선택한 구조

### 1. 별도 Edge lifecycle

Edge Gateway와 `heimdall-edge` network는 Control Compose와 별도 lifecycle로 시작한다. Control
Compose의 `stop/down`이 public request path를 제거하지 않도록 Edge container, config와 network를
Control Compose가 소유하지 않는다.

```text
Edge lifecycle
├─ heimdall-edge-gateway
├─ heimdall-edge network
└─ generated config root

Control lifecycle
├─ control-postgres
├─ api
├─ deployment-worker
├─ routing-worker
└─ frontend -- heimdall-edge network alias로 관리 hostname 제공

Project runtime lifecycle
└─ project gateway
   ├─ active generation network
   └─ heimdall-edge network의 deterministic alias
```

Edge Gateway는 `80/443`의 유일한 public listener이고 `unless-stopped` restart policy를 사용한다.
local에서는 충돌 없는 configurable loopback port로 같은 구조를 재현한다. Management frontend는
Edge network의 고정 alias로 연결되며 Control Plane 중단 시 관리 화면만 unavailable이 된다.

### 2. Edge network와 project isolation

`heimdall-edge`에는 Edge Gateway, Control frontend와 project별 NGINX gateway만 연결한다. application
service container는 기존 project generation network에만 남고 공용 Edge network에 연결하지 않는다.
Edge는 deterministic project gateway alias의 `8080`으로 proxy한다.

```text
Host: student-a.<deployment-base-domain>
-> edge gateway
-> hm-p<project-prefix>-gateway:8080
-> 현재 project NGINX config
-> service generation alias
```

project gateway의 기존 loopback stable Preview port는 health·recovery와 내부 관리 URL 호환성을 위해
유지한다. public route는 host port를 우회해 Docker Edge network를 사용하므로 host의 dynamic port를
외부에 공개하지 않는다.

Deployment Worker는 gateway를 처음 만들거나 candidate network로 재생성하거나 previous gateway를
복원할 때마다 exact `heimdall-edge` network와 stable project alias를 연결한다. Edge 연결이 필요한
활성 route의 gateway 전환이 실패하면 새 deployment를 성공으로 확정하지 않고 기존 gateway와
generation을 복원한다. route가 아직 없는 기존 active gateway는 Routing Worker의 exact-label
reconciliation으로 Edge network에 한 번 연결할 수 있다.

### 3. Public route feature와 runtime adapter

새 vertical feature는 public hostname의 desired/applied state와 HTTP 계약을 소유한다.

```text
api -> public_routes service -> public_routes repository
                         -> runtime Edge adapter
```

- Router는 request/response 변환과 dependency 조회만 담당한다.
- Service는 subdomain 검증, project 존재 확인, uniqueness, 상태 전이와 job 요청을 조정한다.
- Repository는 public route와 routing job만 접근한다.
- Docker network, filesystem과 NGINX 명령은 `runtime` adapter가 담당한다.
- public route feature는 deployments/runtime repository를 직접 읽지 않고 기존 feature service나
  명시적인 port를 사용한다.

Routing Worker는 API와 별도 command지만 같은 Backend image와 Python package를 사용한다. Docker
socket과 generated Edge config write access는 Routing Worker와 기존 Deployment Worker에만 허용한다.
API, frontend와 project container에는 전달하지 않는다.

### 4. Canonical Edge config

Control DB의 applied route snapshot을 hostname 순서로 정렬해 하나의 bounded generated route config로
렌더링한다. 개별 요청이 NGINX 파일을 직접 덧붙이거나 삭제하지 않는다.

1. Routing Worker가 claim revision의 전체 desired snapshot을 읽는다.
2. 임시 config를 owner-only 경로에 작성한다.
3. 고정 NGINX image와 exact Edge network에서 `nginx -t`를 실행한다.
4. 검증된 파일만 atomic replace한다.
5. exact managed Edge container를 확인한 뒤 reload한다.
6. 관리 hostname과 변경된 project hostname을 probe한다.
7. claim token과 desired revision이 여전히 일치할 때만 applied revision과 `ACTIVE`를 기록한다.

test·reload·probe가 실패하면 이전 generated config를 복원하고 기존 route를 다시 probe한다. 복원 여부를
확정할 수 없으면 job을 terminal 성공으로 쓰지 않고 `FAILED` 또는 `UNCERTAIN` 상태와 stable error
code만 저장한다. raw command environment와 certificate content는 DB·API·event에 저장하지 않는다.

## 데이터 계약

초기 제품은 project당 public route 하나를 소유한다.

```text
project_public_routes
├─ project_id                    PK/FK projects
├─ subdomain                     normalized label
├─ hostname                      UNIQUE, server-derived
├─ desired_state                 ENABLED | DISABLED
├─ status                        PENDING | APPLYING | ACTIVE | INACTIVE | FAILED | UNCERTAIN
├─ desired_revision              monotonic integer
├─ applied_revision              nullable integer
├─ last_error_code               nullable stable code
├─ created_at
└─ updated_at

public_route_jobs
├─ project_id                    PK/FK project_public_routes
├─ desired_revision
├─ state                         PENDING | CLAIMED | SUCCEEDED | FAILED
├─ attempts
├─ available_at
├─ lease_owner
├─ lease_expires_at
├─ claim_token
├─ last_error_code
├─ created_at
├─ updated_at
└─ completed_at
```

hostname은 API가 설정된 deployment base domain과 normalized subdomain으로 생성한다. client가 임의
hostname, scheme, port나 upstream을 저장하지 못하게 한다. lowercase ASCII label만 허용하고 길이,
leading/trailing hyphen과 연속 separator를 제한하며 관리 hostname, `www`, `api`, `admin`과 운영자가
설정한 reserved label을 거부한다.

동일 hostname의 동시 요청은 Control DB unique constraint가 최종 차단한다. update/disable은
`desired_revision`을 증가시키고 같은 transaction에서 해당 revision의 durable job을 upsert한다.
오래된 claim은 revision fencing 때문에 새 desired state를 덮어쓸 수 없다.

## 공개 API와 사용자 흐름

초기 API는 project resource 아래의 단일 public route로 제한한다.

```text
GET    /api/projects/{projectId}/public-route
PUT    /api/projects/{projectId}/public-route
DELETE /api/projects/{projectId}/public-route
```

`PUT`은 subdomain label만 받고 server-derived hostname과 `PENDING` 상태를 반환한다. 동일 desired
subdomain 재요청은 idempotent하다. 다른 project가 hostname을 선점했으면 stable `409`를 반환한다.
`DELETE`는 row를 즉시 지우지 않고 `DISABLED` desired state와 새 revision을 기록해 Edge config에서
제거된 사실을 확인한 뒤 비활성 상태로 수렴시킨다.

```text
사용자 subdomain 요청
-> validation·uniqueness
-> desired route와 job commit
-> Routing Worker claim
-> project gateway exact-label/network 확인
-> full config test·atomic reload
-> hostname probe
-> ACTIVE
```

아직 성공한 runtime이 없는 project도 hostname을 예약할 수 있지만 route는 `PENDING`을 유지한다.
Routing Worker는 active gateway가 관찰될 때까지 bounded backoff하고, 첫 성공 배포 또는 명시적 retry가
job을 다시 available하게 한다. public route 실패가 application generation이나 project database를
삭제하지 않는다.

## 관리 hostname과 배포 hostname

- 관리용 도메인은 exact hostname 설정 하나이며 Edge의 static config가 Control frontend alias로
  전달한다.
- 배포용 도메인은 wildcard DNS와 base domain 설정 하나이며 project route config가 subdomain을
  deterministic project gateway alias로 전달한다.
- 관리용 hostname과 배포용 base domain은 서로 다른 사용자 소유 도메인을 허용한다.
- 알 수 없는 hostname과 wildcard apex 요청은 default server에서 `404`로 종료한다.
- Edge는 inbound `Host`를 project gateway와 application에 보존하고 신뢰할 수 없는 inbound
  forwarding header는 덮어쓴다. project gateway는 기존 proxy chain에 Edge hop을 추가한다.

## TLS와 certificate 경계

Load Balancer가 없으므로 production HTTPS의 종료 지점은 Edge Gateway다. certificate와 private key는
repository, image, Control DB와 API에 저장하지 않고 owner-only host path에서 Edge container에
read-only mount한다. Routing Worker는 certificate 원문을 읽거나 응답에 포함하지 않는다.

wildcard certificate의 발급·자동 갱신 방식은 아직 선택하지 않았다. 초기 구현은 operator-provided
certificate path와 reload 계약까지만 만들 수 있으며, 실제 public release 전에 DNS provider와
DNS-01 자동화 여부를 별도 승인한다. certificate가 없거나 만료됐을 때 기존 HTTP/project runtime을
삭제하거나 route metadata를 자동 변경하지 않는다.

## 실패 영향과 복구

| 실패 대상 | 기존 public URL | 새 route 변경 |
| --- | --- | --- |
| API | 기존 Edge config로 계속 제공 | 요청 불가 |
| Routing Worker | 기존 Edge config로 계속 제공 | `PENDING/APPLYING`에서 대기 |
| Deployment Worker | 기존 active generation 계속 제공 | 새 배포·gateway 전환 불가 |
| Control PostgreSQL | 기존 Edge config로 계속 제공 | desired/applied 상태 변경 불가 |
| Control frontend | project public URL은 유지 | 관리 화면만 불가 |
| Edge Gateway | public URL 전체 중단 | Worker가 직접 traffic fallback하지 않음 |
| 특정 project gateway | 해당 project URL만 중단 | 다른 project는 유지 |
| application generation | 해당 project의 gateway 응답 실패 | 기존 deployment recovery 계약 사용 |

Edge Gateway가 중단되면 Docker restart policy가 재시작을 담당한다. Routing Worker startup은 exact
managed Edge container, config revision, route rows, gateway label/network와 probe 결과를 비교한다.
자동 복구는 DB desired state와 exact managed label이 모두 확인되는 resource로 제한하고 unmanaged
container, unknown network와 broad config directory를 삭제하지 않는다.

## 선택한 방향과 감수한 단점

- 기존 stable Preview port를 public listener로 확대하지 않고 고정 Edge network를 추가한다. public
  port 수와 방화벽 규칙은 작아지지만 project gateway가 generation network와 Edge network 두 개에
  연결된다.
- Edge Gateway를 Control Compose와 분리해 request path 생존을 보장한다. 대신 local/production 시작
  순서가 `Edge -> Control Plane`으로 하나 늘어난다.
- Edge는 application generation이 아니라 project gateway만 바라본다. proxy hop이 하나 늘지만 기존
  atomic activation과 last-known-good 복구를 그대로 재사용한다.
- Routing Worker를 deployment Worker와 별도 process로 둔다. process와 job 운영은 하나 늘지만 public
  hostname 변경 실패가 deployment queue와 service-log broker를 점유하지 않는다.
- project당 hostname 하나로 시작한다. custom domain과 복수 hostname 요구가 확인되면 route ID 중심
  aggregate로 확장한다.
- Control DB 전체 snapshot으로 generated config를 만든다. route 수가 매우 커지면 reload 비용이
  증가하지만 단일 VM 초기 규모에서는 partial file mutation보다 재현·복구가 단순하다.

## 수직 단계와 검증

### 1. Public route aggregate와 API

- migration에 desired/applied route와 durable job schema, FK·unique·CHECK constraint를 추가한다.
- normalized subdomain, reserved label, idempotent request, conflict, disable과 revision fencing을 단위·실제
  PostgreSQL 테스트로 검증한다.
- project 상세 화면에서 hostname 요청과 `PENDING/ACTIVE/FAILED` 상태를 확인한다.
- Docker·NGINX mutation은 아직 활성화하지 않는다.

안전한 중단 지점: desired route만 저장되며 기존 deployment와 Preview 동작은 바뀌지 않는다.

### 2. 독립 Edge lifecycle과 static management route

- Edge NGINX, external Edge network, generated config root와 restart policy를 별도 lifecycle로 구성한다.
- Control frontend를 Edge network의 고정 alias에 연결한다.
- exact 관리 hostname, unknown host `404`, Docker label, `80/443` bind와 Control Compose 독립 생존을
  검증한다.
- local에서는 test-owned port와 hostname으로 public listener 구조를 재현한다.

안전한 중단 지점: 관리 hostname만 Edge를 통과하고 project route는 기존 loopback Preview를 유지한다.

### 3. Project gateway Edge network 연결

- gateway start·candidate rebase·previous restore 모든 경로가 Edge network와 deterministic alias를
  보존하도록 변경한다.
- Edge network 연결 실패 시 DB active metadata를 전환하지 않고 previous gateway를 복구한다.
- 기존 active gateway의 exact label을 확인한 초기 연결 reconciliation을 추가한다.
- running/stopped gateway, first deployment, generation rebase와 rollback 회귀 테스트를 보존한다.

안전한 중단 지점: Edge가 project gateway를 resolve할 수 있지만 dynamic hostname config는 없다.

### 4. Routing Worker와 dynamic hostname activation

- claim token·lease·revision fencing, renew, retry와 attempt 상한을 구현한다.
- full config render, `nginx -t`, atomic replace, exact managed Edge reload와 host probe를 구현한다.
- config test, reload, probe와 Worker crash 각 시점에서 이전 route 보존과 회수를 검증한다.
- route disable 뒤 unknown hostname이 `404`이고 다른 project route는 유지되는지 확인한다.

안전한 중단 지점: HTTP hostname routing이 durable하게 동작하며 TLS 자동화와 회원 기능은 없다.

### 5. Release smoke와 문서 동기화

- 서로 다른 두 project hostname이 각자의 deployment marker와 application 응답을 반환하는지 확인한다.
- project A hostname이 project B gateway로 연결되지 않는지 확인한다.
- 같은 project 연속 배포 뒤 hostname은 그대로이고 새 generation marker를 반환하는지 확인한다.
- API·Routing Worker·Deployment Worker 재시작과 Control Compose stop 중 기존 public route가 유지되는지
  확인한다.
- malformed/unknown hostname, disabled route, Edge reload 실패와 stopped gateway를 검증한다.
- backend pytest·Ruff, frontend verify, Edge/Control Compose config와 실제 Docker smoke를 실행한다.
- README, architecture, project profile과 product scope를 구현된 public hostname 계약으로 갱신한다.

## 인수 조건

- OCI wildcard DNS record를 project마다 추가하지 않아도 새 hostname을 활성화할 수 있다.
- 관리 hostname은 Control frontend로, 두 project hostname은 각자의 project gateway로 전달된다.
- unknown, reserved, disabled 또는 다른 base domain hostname은 project에 전달되지 않는다.
- route hostname uniqueness와 project당 하나의 route가 DB constraint로도 보장된다.
- Edge는 application container나 generation alias를 직접 참조하지 않는다.
- project gateway의 generation 전환 전후 public hostname과 loopback stable Preview port가 유지된다.
- API·Control DB·Worker가 중단돼도 Edge와 project runtime이 살아 있으면 기존 public URL이 응답한다.
- Routing Worker crash나 stale claim이 최신 desired revision 또는 기존 valid config를 덮어쓰지 않는다.
- invalid config, reload 또는 probe 실패 시 이전 public route snapshot이 유지된다.
- Control Compose `stop/down`은 Edge container, Edge network와 배포된 project runtime을 제거하지 않는다.
- application container, API와 frontend는 Docker socket을 받지 않는다.
- backend/frontend gate, Compose config와 실제 hostname routing smoke가 통과한다.

## Rollback과 안전 원칙

- route feature rollout 전 독립 Edge network와 Edge Gateway를 먼저 healthy하게 만든다.
- DB migration은 기존 project/deployment/runtime row를 수정하거나 public route를 자동 생성하지 않는다.
- route 활성화 전까지 기존 loopback Preview URL을 rollback 경로로 유지한다.
- 새 Edge config는 test 성공 전 현재 config를 교체하지 않고, reload·probe 실패 시 last-known-good를
  복원한다.
- project gateway mutation은 exact managed/project/kind label과 deterministic name을 모두 확인한다.
- broad Docker network/container cleanup, runtime root 삭제와 wildcard filesystem cleanup을 금지한다.
- 기능 rollback 시 public route API와 Worker를 중지하고 last-known-good Edge config를 유지할 수 있다.
  DB row와 generated config는 즉시 삭제하지 않는다.
- 실제 smoke resource는 test-owned domain label, project ID와 exact Docker label로 제한한다.

## 설정 영향

정확한 이름은 구현 시 기존 `Settings` 규칙에 맞추되 다음 책임의 설정이 필요하다.

- 관리용 exact hostname
- 배포용 base domain
- reserved subdomain labels
- Edge config root와 exact managed container/network name
- Routing Worker poll, lease, heartbeat, retry와 attempt 상한
- Edge HTTP/HTTPS bind와 local probe endpoint
- operator-provided certificate/key path

환경변수에는 certificate 원문이나 project별 hostname mapping을 넣지 않는다. route mapping의 원본은
Control PostgreSQL이다.

## 문서 영향

Plan 승인 시점에는 이 문서만 추가한다. 현재 `README.md`, `project-docs/architecture.md`,
`project-docs/project-profile.md`와 `project-docs/product-scope.md`는 구현된 현재 상태만 설명하므로
아직 미구현인 public routing을 현재 기능처럼 기록하지 않는다.

구현 완료 시 다음을 함께 갱신한다.

- `README.md`: Edge -> Control 시작 순서, 관리/배포 hostname, public port와 장애 영향
- `project-docs/architecture.md`: Edge data path, Routing Worker, Edge network와 desired/applied route
- `project-docs/project-profile.md`: 단일 VM public hostname과 gateway 생존 계약
- `project-docs/product-scope.md`: wildcard hostname routing 포함, custom domain·multi-node 비범위
- `.env.example`: domain, Edge runtime, Worker와 certificate path 설정

## 남은 결정

1. 실제 관리용 hostname과 배포용 base domain 값은 운영 설정 시 확정한다.
2. wildcard certificate를 수동 배치할지 DNS-01로 자동 발급·갱신할지는 TLS 단계 전에 별도 승인한다.
3. public hostname은 초기에는 누구나 접근 가능한 URL로 가정한다. 로그인 사용자만 접근하는 private
   Preview가 필요하면 인증 Plan과 함께 Edge authorization 경계를 다시 설계한다.
4. 단일 VM의 route 수와 NGINX reload 시간이 운영 기준을 넘으면 config shard 또는 dynamic proxy를
   재검토한다. 초기에는 측정 없이 별도 routing system을 추가하지 않는다.

## 구현 중 검증 기록

- `2026-08-20`: 현재 project gateway가 active generation network의 주 네트워크와
  `127.0.0.1` stable Preview port를 소유하고, 모든 generation 전환에서 동일 port로 재생성되는
  계약을 확인했다.
- `2026-08-20`: Edge를 Control Compose가 소유하면 Control lifecycle 중단이 public request path를
  끊는 문제를 확인해 별도 Edge lifecycle과 외부 고정 network를 선택했다.
- `2026-08-20`: 현재 제품 문서는 구현된 사실만 설명하므로 Plan 작성 시점에는 다른 현재 문서를
  변경하지 않고, 구현 완료 단계에서 관련 문서를 함께 갱신하기로 했다.
