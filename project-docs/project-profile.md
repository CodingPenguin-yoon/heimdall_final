# 프로젝트 프로필

- 상태: `APPROVED`
- 기준일: `2026-08-13`
- 프로젝트명: `Heimdall Python`
- 목적: Public GitHub 저장소의 애플리케이션을 단일 호스트 Docker preview로 수동 배포하고 상태·로그·이력을 관리한다.
- 사용자: 신뢰된 저장소를 관리하는 단일 관리자
- 형태: FastAPI backend와 React frontend를 함께 관리하는 monorepo
- 하네스 버전: `1.0.0`

## 기술 방향

- Backend: Python, FastAPI, Pydantic, psycopg
- Control DB와 durable job queue: Control PostgreSQL
- Project application data: 별도 Managed PostgreSQL cluster의 project별 database·role
- Secret: repository 밖 runtime root의 versioned owner-only file, Control DB에는 reference·version·fingerprint만 저장
- Source: Git CLI, Public HTTPS, `main` 고정
- Runtime: Docker CLI, generation candidate, 프로젝트별 NGINX gateway
- Local control plane: Docker Desktop Compose, API·Worker·NGINX frontend와 Control PostgreSQL volume
- Managed DB: 별도 Compose/VM lifecycle, private TCP endpoint와 독립 PostgreSQL volume
- Frontend: React, TypeScript, React Router, TanStack Query, CSS Modules
- 검증: pytest, Ruff, Vitest, Testing Library, Playwright

정확한 dependency version은 lockfile과 `pyproject.toml`/`package.json`에서 관리한다.

## 승인된 제품 규칙

- 저장소 등록과 배포 설정을 분리한다.
- 프로젝트는 `DRAFT` 또는 `READY` 상태다.
- 설정은 Heimdall DB가 원본이며 저장소에 전용 YAML을 요구하지 않는다.
- 배포는 `main` 최신 commit 또는 최근 목록에서 선택한 commit만 허용한다.
- 배포마다 source를 다시 build하며 image registry와 즉시 image rollback은 초기 비범위다.
- multi-service는 generation network의 고유 DNS alias로 통신한다.
- 프로젝트별 NGINX 하나가 안정 preview port를 소유한다.
- 모든 candidate가 정상일 때만 gateway를 전환하고 실패 시 기존 preview를 유지한다.
- 모든 generation 전환은 candidate route를 먼저 검증하고 project gateway를 candidate network의
  동일 preview port에 다시 생성·검증한 후에만 active metadata를 전환한다. exact managed label의
  정지 gateway는 그 전에 기존 active network의 last-known-good 상태로 1차 복원한다.
- Worker는 PostgreSQL claim token과 lease로 fencing하며 만료된 작업은 새 Worker가 회수한다.
- 회수 Worker는 DB·NGINX response marker·Docker deployment label을 비교해 실제 active
  generation을 확정하고, 불확실한 candidate는 삭제하지 않으며 attempt 상한을 적용한다.
- Docker resource는 project·deployment label과 deterministic generation name을 사용한다.
- cleanup은 label과 deployment ID가 일치하는 candidate와 이전 generation만 대상으로 한다.
- 불확실한 failed candidate는 설정된 기간 동안 보존하고 durable reconciliation Worker가
  재확인한다. 자동 경로는 안전 판정이 없으면 삭제하지 않으며 관리자 force cleanup은 전체
  deployment ID 확인과 DB active guard를 요구한다.
- project runtime은 active deployment와 host loopback stable preview port를 Control DB에 저장한다.
- 로컬 Control Plane은 `heimdall-python-local` Compose project가 소유한다. Docker socket은 Worker에만
  전달하고 API·Worker log broker socket은 전용 named volume에서 공유한다. Managed PostgreSQL은
  별도 `heimdall-managed-db` Compose project/VM이 소유하며 외부 TCP endpoint로만 연결한다.
- 성공한 project service와 project별 NGINX gateway는 Control Plane과 별도 container로 실행한다.
  API·Worker·frontend 또는 Control PostgreSQL만 중단되면 기존 Preview는 유지하지만 새 배포와 관리
  기능은 중단된다. Managed PostgreSQL lifecycle은 Control Plane과 분리되어 DB 사용 project의
  application data path는 유지된다.
- 전역 배포 활동은 기존 Deployment 공개 필드로 최근 100건을 최신순 조회하고, project 이름은 기존
  project 목록과 UI에서 결합한다. 진행 중 배포가 있을 때만 목록을 자동 갱신한다.
- 구조화 deployment event는 raw application output 없이 Control DB에 저장하고, 초기 snapshot 뒤
  PostgreSQL LISTEN/NOTIFY wake-up과 durable event ID cursor 기반 SSE로 전달한다. 별도 서비스 로그
  snapshot과 SSE live follow는 Worker가 exact container label을 확인하고 알려진 secret을 fail-closed
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
