# Ubuntu Docker Engine Worker host network 지원

- 상태: `APPROVED`
- 기준일: `2026-08-29`

## 현재 동작과 문제

Control Plane의 기본 Compose는 Docker Desktop을 전제로 deployment Worker와 Routing Worker를
일반 bridge network에서 실행한다. deployment Worker는 `host.docker.internal`을 통해 candidate
service와 stable Preview를 probe하고, Routing Worker도 같은 경로로 host의 Edge listener를 probe한다.

반면 project candidate와 project gateway, Edge의 기본 host publish는 외부 노출을 막기 위해
`127.0.0.1`에만 bind한다. Docker Desktop은 container에서 host loopback publish로 이어지는 경로를
제공하지만 Ubuntu Docker Engine의 `host.docker.internal:host-gateway`는 bridge gateway를 가리킨다.
따라서 bridge Worker는 host loopback에만 열린 임시 port에 도달하지 못한다. candidate 자체의 내부
health가 정상이어도 `SERVICE_HEALTH_TIMEOUT`으로 배포가 취소되고, gateway가 준비되지 않아 후속
route 작업이 `GATEWAY_START_FAILED`로 대기하거나 실패한다.

## 목표와 인수 조건

목표는 loopback-only publish 보안 경계를 유지하면서 Ubuntu Docker Engine에서 두 Worker가 host의
candidate, Preview, Edge endpoint를 정상적으로 검증하게 하는 것이다.

인수 조건은 다음과 같다.

- Docker Desktop의 기존 기본 Compose 동작은 바뀌지 않는다.
- Ubuntu용 Compose 실행에서는 deployment Worker와 Routing Worker가 host network를 사용한다.
- Ubuntu용 두 Worker는 Control PostgreSQL에 `127.0.0.1:<configured host port>`로 연결한다.
- deployment Worker의 runtime probe와 Routing Worker의 Edge probe는 `127.0.0.1`을 사용한다.
- candidate, stable Preview, Control PostgreSQL, Edge listener의 loopback-only bind는 유지한다.
- 합성된 Desktop/Ubuntu Compose 구성이 모두 유효하고, Ubuntu 구성에서 기대한 network와 endpoint가
  명시적으로 검증된다.
- Ubuntu 실행·중지·로그·재생성 절차가 운영 문서에 남는다.

## 범위와 비범위

범위는 local Control Plane의 Ubuntu Docker Engine Compose 계약, 관련 구성 검증, README와 현재
architecture 설명이다. application runtime, deployment 상태 전이, gateway 전환 알고리즘, 공개 API,
DB schema는 변경하지 않는다.

Managed PostgreSQL의 Ubuntu 접근 계약은 이번 장애에서 별도 문제로 확인되지 않았으므로 변경하지
않는다. project container가 Managed PostgreSQL에 접근하는 환경에서는 현재 운영 규칙대로 실제로
도달 가능한 private host/port를 설정해야 한다.

## 선택한 방향과 단점

기본 `infra/dev/compose.yaml`은 Docker Desktop용으로 유지하고, Ubuntu 실행 시 함께 적용하는 작은
override Compose 파일을 추가한다. override는 deployment Worker와 Routing Worker에만
`network_mode: host`를 적용하고 다음 endpoint를 host loopback으로 바꾼다.

- `HEIMDALL_DATABASE_URL`: `127.0.0.1:${HEIMDALL_CONTROL_DB_PORT}`
- `HEIMDALL_RUNTIME_PROBE_HOST`: `127.0.0.1`
- `HEIMDALL_EDGE_PROBE_HOST`: `127.0.0.1`

이 방향은 host에 publish된 port를 `0.0.0.0`으로 넓히지 않아 기존 local-only 보안 경계를 보존한다.
대신 Ubuntu 운영자는 항상 base와 override 두 파일을 같은 순서로 지정해야 한다. 두 Worker는 host
network namespace를 공유하므로 bridge 격리 수준이 낮아지지만, Docker socket을 이미 보유한 신뢰된
control-plane process로 범위를 제한한다.

## 데이터·보안·외부 효과와 실패 영향

DB data나 secret 저장 형식은 바뀌지 않는다. Control DB password는 기존 Compose interpolation을
그대로 사용한다. host network는 두 Worker에만 적용하고 API, frontend, Control PostgreSQL,
application container에는 적용하지 않는다.

override를 누락하면 Ubuntu의 기존 timeout 장애가 재발하지만 기존 active Preview는 deployment
실패 경계에 따라 유지된다. 잘못된 Control DB host port를 설정하면 Worker만 시작 실패하며 API와
기존 runtime data path는 유지된다.

## 구현 단계와 검증

1. Ubuntu override의 기대 계약을 검사하는 실패 테스트를 추가한다.
2. 최소 override Compose 파일을 추가하고 관련 테스트를 통과시킨다.
3. `docker compose config`로 Desktop base와 Ubuntu merged configuration을 각각 검증한다.
4. README에 Ubuntu start, stop, logs, recreate 명령과 host-network 이유를 추가하고 architecture의
   Docker Desktop 단일 가정을 platform별 계약으로 갱신한다.
5. 관련 backend 테스트와 backend 집계 gate를 실행한 뒤 `$verify-change`로 diff, 문서, 검증 결과를
   대조한다.

실제 Ubuntu host에서의 Docker network smoke는 해당 환경에서 candidate health, stable Preview,
Edge route까지 확인하는 release gate로 남긴다.

## Rollback과 안전한 중단 지점

override 파일과 문서·계약 테스트만 제거하면 기본 Docker Desktop 동작으로 완전히 돌아간다. DB
migration이나 runtime resource 변환이 없으므로 각 구현 단계 뒤 안전하게 중단할 수 있다. 실행 중
rollback은 Ubuntu Compose를 기존 방식으로 재생성하는 것이며 active project runtime과 Edge는 Control
Compose lifecycle 밖에 있어 제거하지 않는다.

## 문서 영향과 남은 결정

`README.md`의 Local Docker Compose와 운영 중지·재시작 예시, `project-docs/architecture.md`의 local
networking 계약을 갱신한다. 실제 Ubuntu smoke 결과가 확보되면 이 Plan의 검증 기록에 추가한다.

Routing Worker도 같은 host-network override에 포함했다. Edge가 기본적으로 `127.0.0.1`에 bind되므로
제외하면 deployment는 성공해도 public hostname route 적용이 동일한 network-path 불일치로 실패할 수
있기 때문이다.

## 검증 기록

- 회귀 테스트 RED: override 파일 부재로 두 Worker 계약 테스트가 모두 `FileNotFoundError`로 실패했다.
- 회귀 테스트 GREEN: `backend/.venv/bin/pytest backend/tests/test_local_compose.py` — `2 passed`.
  테스트는 Docker Compose CLI의 JSON config를 파싱해 Desktop base와 Ubuntu merged 계약을 검증한다.
- Desktop base Compose: dummy non-secret 값으로 `docker compose ... config --quiet` — 성공.
- Ubuntu merged Compose: 두 Worker 모두 `network_mode=host`, Control DB `127.0.0.1:55432`, deployment
  probe와 Edge probe `127.0.0.1`로 합성됨을 확인했다.
- Backend 집계: `backend/.venv/bin/pytest backend/tests` — `260 passed, 18 skipped`.
- Backend lint: `backend/.venv/bin/ruff check backend/src backend/tests` — 성공.
- Backend format: `backend/.venv/bin/ruff format --check backend/src backend/tests` — 122 files 확인.
- 실제 Ubuntu Docker Engine의 candidate→Preview→public route smoke는 현재 실행 환경에서 수행하지
  못했으며 release gate로 남긴다.
- 독립 review에서 확인된 Managed DB 지원 범위 과장, 문자열-only Compose 테스트, project profile
  불일치는 Worker probe 범위 명시, 실제 Compose merge 테스트, profile 갱신으로 해소했다.
