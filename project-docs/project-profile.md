# 프로젝트 프로필

- 상태: `APPROVED`
- 기준일: `2026-08-03`
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
- service별 사용자 환경변수와 project database 접근 여부를 설정한다.
- 사용자 환경변수와 Heimdall 예약 `DATABASE_*`, `HEIMDALL_*` 값을 배포 시 합성한다.
- project database password와 사용자 secret은 Heimdall이 관리하며 API·Control DB·deployment snapshot에 raw 값을 남기지 않는다.
