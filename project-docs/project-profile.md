# 프로젝트 프로필

- 상태: `APPROVED`
- 기준일: `2026-08-05`
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
- exact managed label의 정지 gateway는 다음 배포에서 저장된 preview port로 복원한다. 기존 active
  network의 last-known-good 복원과 candidate 검증 뒤 candidate network 기준으로 다시 생성·검증한
  후에만 active metadata를 전환한다.
- Worker는 PostgreSQL claim token과 lease로 fencing하며 만료된 작업은 새 Worker가 회수한다.
- 회수 Worker는 DB·NGINX response marker·Docker deployment label을 비교해 실제 active
  generation을 확정하고, 불확실한 candidate는 삭제하지 않으며 attempt 상한을 적용한다.
- Docker resource는 project·deployment label과 deterministic generation name을 사용한다.
- cleanup은 label과 deployment ID가 일치하는 candidate와 이전 generation만 대상으로 한다.
- 불확실한 failed candidate는 설정된 기간 동안 보존하고 durable reconciliation Worker가
  재확인한다. 자동 경로는 안전 판정이 없으면 삭제하지 않으며 관리자 force cleanup은 전체
  deployment ID 확인과 DB active guard를 요구한다.
- project runtime은 active deployment와 host loopback stable preview port를 Control DB에 저장한다.
- 전역 배포 활동은 기존 Deployment 공개 필드로 최근 100건을 최신순 조회하고, project 이름은 기존
  project 목록과 UI에서 결합한다. 진행 중 배포가 있을 때만 목록을 자동 갱신한다.
- 구조화 deployment event는 raw application output 없이 Control DB에 저장한다. 별도 서비스 로그
  snapshot과 SSE live follow는 Worker가 exact container label을 확인하고 알려진 secret을 fail-closed
  마스킹한 뒤 service당 최근 200줄 buffer로만 제공한다. snapshot과 stream capacity를 분리하고
  disconnect 시 Docker follow를 정리하며 로그를 저장하지 않는다.
- service별 사용자 환경변수와 project database 접근 여부를 설정한다.
- 사용자 환경변수와 Heimdall 예약 `DATABASE_*`, `HEIMDALL_*` 값을 배포 시 합성한다.
- project database password와 사용자 secret은 Heimdall이 관리하며 API·Control DB·deployment snapshot에 raw 값을 남기지 않는다.
- user secret kind 환경변수에는 raw 값이 아니라 `/run/secrets/heimdall/environment/<name>` file path를 전달한다.
