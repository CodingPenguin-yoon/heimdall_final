# 로컬 Control Plane Docker Compose Plan

- 상태: `COMPLETED`
- 날짜: `2026-08-11`
- 승인 근거: 사용자가 Heimdall의 다음 우선 작업으로 Docker Compose 구현을 선택함
- 대상 환경: 로컬 Docker Desktop

## 현재 동작과 문제

현재 `infra/dev/compose.yaml`은 Control PostgreSQL과 Managed PostgreSQL만 실행한다. FastAPI API,
deployment Worker와 Vite frontend는 별도 terminal 또는 임시 `screen` 세션에서 수동 실행한다.
이 구조에서는 API process가 실행 세션과 함께 종료돼도 자동 복구되지 않고, frontend는 살아 있어도
`/api` proxy가 502를 반환한다. API, Worker와 frontend의 시작 순서와 환경변수도 운영자가 직접
맞춰야 한다.

현재 데이터는 Compose project `heimdall-python-local`의
`heimdall-python-local_control-postgres`와 `heimdall-python-local_managed-postgres` volume에 있다.
새 project나 새 volume으로 전환하면 기존 project·deployment metadata와 application database가
사라진 것처럼 보일 수 있으므로 같은 project와 volume을 재사용해야 한다.

Worker를 컨테이너화할 때는 다음 경계를 추가로 보존해야 한다.

- Docker socket은 Worker에만 mount하고 API, frontend와 project container에는 전달하지 않는다.
- Worker가 Docker daemon에 넘기는 build context, NGINX config와 secret source path는 host daemon도
  해석할 수 있는 동일한 절대 경로여야 한다.
- API와 Worker의 service-log Unix socket은 Docker Desktop bind mount 대신 전용 named volume에서
  공유한다.
- candidate health port와 stable preview port는 계속 host `127.0.0.1`에만 publish하되, Worker는
  `host.docker.internal`을 통해 이를 probe한다. 현재 project container에서 이 경로로 stable
  preview가 HTTP 200을 반환하는 것을 확인했다.

## 목표

- `docker compose up -d --build --wait` 한 번으로 PostgreSQL, API, Worker와 frontend를 실행한다.
- API, Worker와 frontend에 `restart: unless-stopped`를 적용해 임시 terminal 수명과 분리한다.
- 기존 Control/Managed PostgreSQL volume과 현재 배포 runtime metadata를 보존한다.
- frontend는 같은 origin의 `/api`를 API service로 proxy하고 SSE buffering을 비활성화한다.
- Compose Worker도 기존 Docker deployment, Gateway rebase, health/route probe와 log broker 계약을
  유지한다.

## 범위

- Backend API·Worker 공용 image와 Worker 전용 Docker CLI/socket
- 정적 frontend image와 internal API reverse proxy
- 기존 local PostgreSQL Compose에 API, Worker와 frontend service 추가
- container probe host와 broker socket root의 환경 설정 분리
- 기존 `heimdall-python-local` project·volume 보존 전환
- 실제 `heimdall-test` 배포를 통한 Docker Desktop smoke
- README와 현재 architecture/profile 동기화

## 비범위

- Linux host와 production orchestrator용 이식성 보장
- public domain, TLS, 인증과 외부 network 공개
- Docker socket proxy 또는 rootless Docker 도입
- Gateway 주기 watchdog과 application service 상시 모니터링
- DB schema, 공개 API와 deployment 상태 계약 변경

## 데이터·보안·외부 효과와 실패 영향

1. Compose project 이름을 기존 `heimdall-python-local`로 고정하고 기존 named volume을 재사용한다.
2. API와 Worker는 같은 Backend image를 사용하지만 `/var/run/docker.sock`은 Worker service에만
   mount한다.
3. runtime과 Git workspace는 `.env`의 host 절대 경로를 container 안의 동일 경로에 bind해 Worker가
   생성한 Docker build/mount source를 host daemon도 해석하게 한다.
4. raw secret을 포함하는 runtime bind는 API와 Worker에만 read-write로 제공하고 frontend에는
   제공하지 않는다.
5. service-log socket은 `HEIMDALL_BROKER_SOCKET_ROOT` 아래 전용 named volume에 두며 socket
   permission과 fail-closed redaction 계약을 유지한다.
6. health와 Gateway probe URL host는 `HEIMDALL_RUNTIME_PROBE_HOST`로 분리한다. 기본값은 기존
   `127.0.0.1`, Compose Worker만 `host.docker.internal`을 사용한다. Docker publish bind는 계속
   `127.0.0.1`이다.
7. frontend와 API host port도 `127.0.0.1`에만 bind한다.
8. 전환 실패 시 새 API·Worker·frontend service만 중지하고 기존 PostgreSQL container·volume을
   유지한 채 기존 `screen` 실행 방식으로 복구한다.

## 선택한 방향과 감수한 단점

- 현재 장애를 직접 해결하는 로컬 Docker Desktop Compose를 먼저 제공한다. Linux와 production
  배포는 host networking과 socket 권한 정책이 달라 별도 후속 설계가 필요하다.
- runtime/Git path를 host와 container에서 같게 유지해 기존 Docker CLI adapter를 보존한다. 대신
  `.env`에 실제 host 절대 경로가 필요하다.
- API와 Worker가 공용 image를 사용해 build를 단순화한다. API container에는 Docker CLI binary가
  포함되지만 socket은 없어 Docker daemon 권한은 갖지 않는다.
- Worker는 로컬 Docker socket 사용을 위해 root로 실행한다. API와 frontend에는 socket을 주지 않아
  권한 경계를 service 단위로 유지한다.
- frontend는 개발 Vite가 아니라 NGINX 정적 build를 사용한다. hot reload는 Compose 밖의 기존
  선택 실행으로 남긴다.

## 수직 단계와 검증

### 1. 컨테이너 경계 설정

- 기본 host 실행에서 probe URL이 `127.0.0.1`을 유지하는 테스트를 보존한다.
- 설정된 probe host가 candidate health와 Gateway route/recovery URL 모두에 적용되는 실패 테스트를
  먼저 추가한다.
- broker socket root가 runtime file root와 분리되는 설정 테스트를 추가한다.

안전한 중단 지점: Compose는 아직 바뀌지 않고 기존 host 실행도 유지된다.

### 2. Image와 Compose

- Backend image에 Python package, Git과 pinned Docker CLI를 포함한다.
- frontend image는 production build와 SPA fallback, `/api`·SSE proxy를 제공한다.
- 기존 PostgreSQL service에 API, Worker와 frontend를 추가하고 health dependency, loopback port,
  restart policy와 mount 경계를 선언한다.
- `docker compose config`와 image build를 검증한다.

안전한 중단 지점: image build만 완료하며 현재 실행 중 process와 DB는 변경하지 않는다.

### 3. 데이터 보존 전환과 smoke

- 현재 DB volume과 active deployment metadata를 다시 기록한다.
- 수동 API·Worker·frontend `screen`을 정상 종료하고 같은 Compose project로 전체 service를 올린다.
- API 직접 health와 frontend `/api` proxy, Worker socket, 기존 preview를 확인한다.
- test project에 새 deployment를 요청해 build, candidate health, Gateway rebase, DB 연결과 stable
  preview port를 검증한다.

### 4. 집계 gate와 문서

- Backend pytest·Ruff와 frontend `pnpm verify`를 실행한다.
- Compose config, container health, restart policy와 socket mount 경계를 대조한다.
- README, architecture와 project profile을 현재 실행 계약으로 갱신한다.

## 인수 조건

- Compose의 PostgreSQL, API, Worker와 frontend가 모두 running이며 health 대상 service는 healthy다.
- 기존 project, deployment, managed database와 stable preview port가 전환 전후 보존된다.
- `http://127.0.0.1:5173/`과 그 `/api` proxy가 HTTP 200을 반환한다.
- API service에는 Docker socket mount가 없고 Worker service에만 있다.
- Compose Worker의 새 실제 배포가 성공하고 stable preview에서 새 deployment marker와 DB 연결
  `true`를 반환한다.
- API 또는 frontend process 종료는 Compose restart policy로 복구 가능하다.
- 기존 host 직접 실행의 `127.0.0.1` probe 동작과 전체 테스트가 회귀하지 않는다.

## rollback

- Compose 전환 전에 기존 PostgreSQL container와 volume 이름을 확인하며 volume을 삭제하지 않는다.
- 실패하면 `api`, `worker`, `frontend`만 중지하고 PostgreSQL service는 유지한다.
- 기존 `.env`를 source한 host `screen` 명령으로 API·Worker·frontend를 다시 시작한다.
- `docker compose down -v`, broad Docker cleanup과 database reset은 실행하지 않는다.

## 문서 영향

- `README.md`: 통합 Compose 실행, 개발 hot reload 선택 경로와 전환 주의사항
- `project-docs/architecture.md`: Compose service, Docker socket·runtime/broker volume 경계
- `project-docs/project-profile.md`: 로컬 Control Plane 실행 계약

## 구현 중 검증 기록

- 기존 host 실행의 `127.0.0.1` probe와 Compose의 `host.docker.internal` probe, runtime root와 broker
  socket root 분리를 테스트 우선으로 추가했다. 관련 Docker/Gateway/config 테스트 28개와 Ruff가
  통과했다.
- Backend image에 Python 3.14.6, Git과 Docker CLI 29.6.1을, frontend image에 Node 24.13.0 build와
  NGINX 1.29.8을 구성했다. 두 image build, Backend Docker client/server 29.6.1 호환과 frontend
  `nginx -t`가 통과했다.
- 최초 Compose 전환에서 PostgreSQL volume과 Control DB의 project 11개·deployment 22개는
  보존됐지만, Managed PostgreSQL container 재생성으로 active generation의 동적 network 연결이
  사라져 `heimdall-test` preview가 DB DNS 503을 반환했다. exact active network에 alias를 복원해
  즉시 HTTP 200과 DB 연결 `true`로 복구했다.
- 재발 방지를 위해 Worker 시작 시 active runtime, deployment snapshot과 exact managed network
  label을 대조하고 DB 사용 network에만 Managed PostgreSQL alias를 복원한다. 단위 재현 테스트가
  통과했고 실제 active network 연결 제거 뒤 Compose Worker 재시작만으로 alias와 preview HTTP
  200/DB 연결 `true`가 복원됐다.
- Compose Worker에서 동일 commit의 실제 배포 `d62edde4-3cdb-4da5-acb9-18f570af1281`을 수행해
  PREPARING부터 ACTIVATING까지 거쳐 SUCCEEDED가 됐다. stable Preview 포트 `55468`은 유지됐고
  Gateway와 Managed PostgreSQL이 새 network `hm-pa8ad7e0df8aa-gd62edde43cdb`에 연결됐으며 새
  deployment marker와 DB 연결 `true`를 반환했다.
- 최종 집계 gate에서 Backend Ruff format/check, pytest `133 passed, 11 skipped`, frontend
  `pnpm verify`의 Vitest `22 passed`, TypeScript와 production build가 통과했다.
- Compose 정적 설정과 실행 상태를 재확인했다. PostgreSQL 2개, API, Worker와 frontend가 모두
  running이고 health 대상은 healthy였으며 API 직접 health, frontend, frontend `/api` proxy와
  stable preview가 모두 HTTP 200을 반환했다. API와 Worker의 restart policy는
  `unless-stopped`다.

## 남은 위험

- Docker에 대한 수동 `docker kill`은 운영자 중지로 취급되어 `unless-stopped` 자동 재시작 검증에
  사용할 수 없었다. 비정상 process 종료를 흉내 낸 추가 신호 테스트도 restart count 증가를
  관찰하지 못했으므로, 실제 crash 후 자동 재시작은 이번 검증에서 입증했다고 표현하지 않는다.
  현재 API와 frontend proxy는 복구 후 healthy이며 restart policy 선언과 적용 상태는 확인했다.
