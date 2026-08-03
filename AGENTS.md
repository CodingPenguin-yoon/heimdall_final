# Heimdall 작업 규칙

## 기본 원칙

- 사용자와 공동 문서는 한국어로 작성한다.
- 새 파일은 책임이 분명할 때만 만든다. 파일 수나 줄 수 자체를 목표로 삼지 않는다.
- 기능은 `projects`, `deployments`, `project_database`, `runtime` 같은 수직 모듈 안에서 완결한다.
- Router는 HTTP 변환만 담당하고 DB, Git, Docker 명령을 직접 실행하지 않는다.
- 외부 효과는 port와 adapter로 격리하되 구현이 하나뿐인 내부 코드에 불필요한 interface를 만들지 않는다.
- 프로젝트 설정과 배포 설정 snapshot은 하나의 aggregate로 취급한다.
- raw secret은 DB, API, 로그, Git 설정에 저장하지 않는다.

## 변경 절차

1. `.agent-harness/workflow.md`, `project-docs/project-profile.md`와 관련 현재 문서만 확인한다.
2. 공개 계약, 데이터 소유권, 인증, Docker network 또는 배포 상태가 바뀌면 `$plan-change`를 사용한다.
3. 가장 작은 수직 흐름으로 구현하고 해당 단위 테스트를 먼저 실행한다.
4. 완료 전 `$verify-change`를 사용하고 backend와 frontend 집계 검증을 실행한다.

## 아키텍처 경계

- `api -> feature service -> repository/adapter` 방향을 유지한다.
- feature 간 직접 repository 접근을 금지한다.
- deployment orchestration은 `deployments`가 소유하고 Docker/NGINX 구현은 `runtime`이 소유한다.
- Control PostgreSQL은 project, deployment, job, runtime과 managed database metadata의 최종 원본이다.
- Managed PostgreSQL은 project application data만 소유하고 Control DB나 Docker socket에 접근하지 않는다.
- Redis는 초기 제품 의존성에 포함하지 않는다.

## Git과 안전

- 사용자 변경을 되돌리지 않는다.
- commit, push, rebase, destructive Docker/DB 명령은 사용자 요청 없이 실행하지 않는다.
- Git과 Docker는 shell 문자열이 아니라 검증된 argument list로 실행한다.
- project container에는 Docker socket, Control DB, Heimdall runtime root를 전달하지 않는다.

## 하네스 관리

- 하네스 관리 경로는 `AGENTS.md`, `.agent-harness/`, `.agents/skills/plan-change/`, `.agents/skills/verify-change/`다.
- 프로젝트별 사실은 `project-docs/`가 소유한다.
- 하네스 변경 시 `.agent-harness/manifest.toml`의 version과 관리 경로를 함께 확인한다.
- 새 skill은 반복되는 독립 workflow가 실제로 생긴 경우에만 추가한다.
