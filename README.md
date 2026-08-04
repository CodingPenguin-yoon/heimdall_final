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
- 실패 시 last-known-good preview 보존과 candidate label cleanup
- 배포 event polling, 실패 단계와 안정 preview link
- 밝은 화이트톤 관리 UI

preview port는 초기 범위에서 host의 `127.0.0.1`에만 공개한다. public domain, TLS와 multi-host routing은 아직 포함하지 않는다.

## 구조

```text
backend/       FastAPI backend
frontend/      React control UI
project-docs/  제품·아키텍처·구현 기준
infra/         로컬 외부 상태
```

## Backend

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
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

## Frontend

```bash
cd frontend
pnpm install
pnpm verify
pnpm dev
```

## Local PostgreSQL

개발 Compose는 Control PostgreSQL과 Managed Project PostgreSQL을 별도 volume으로 실행한다. `.env`의 provisioner password와 `HEIMDALL_PROJECT_DB_ADMIN_URL` password는 같은 값이어야 한다.

```bash
cp .env.example .env
docker compose --env-file .env -f infra/dev/compose.yaml up -d --wait
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
