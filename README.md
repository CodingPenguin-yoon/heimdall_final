# Heimdall Python

Public GitHub 저장소의 `main` commit을 단일 호스트 Docker preview로 배포하는 관리 도구다.

현재 첫 수직 구현 범위는 다음과 같다.

- Public HTTPS 저장소 등록과 `main` 검증
- `DRAFT -> READY` 프로젝트 설정
- multi-service, route, health check 설정
- service별 plain·secret 환경변수와 PostgreSQL 접근 선언
- 별도 Managed PostgreSQL의 project database·role 생성
- owner-only secret file과 non-secret DB 연결정보
- 최근 `main` commit 조회
- 최신 또는 특정 commit 배포 요청과 immutable 설정 snapshot
- 밝은 화이트톤 관리 UI

Docker candidate의 환경변수·secret mount와 프로젝트별 NGINX activation Worker는 다음 구현 단계이며, 공개 계약과 상태 모델은 현재 기준선에 포함돼 있다.

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
