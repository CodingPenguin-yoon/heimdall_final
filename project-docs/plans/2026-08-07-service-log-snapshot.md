# 서비스 로그 Snapshot Plan

- 상태: `COMPLETED`
- 날짜: `2026-08-07`
- 승인: `2026-08-10` 사용자가 현재 변경을 별도 브랜치에 checkpoint한 뒤 이 방향으로 진행하도록 승인
- 초기 방향: 사용자가 구조화 deployment event 다음 단계로 서비스별 최근 컨테이너 로그 200줄을
  먼저 제공하고 실시간 streaming은 후속 단계로 분리하는 방향에 동의함
- 승인 내용: Worker 매개 Unix socket과 application log의 잔여 민감정보 위험을 포함한 아래 보안 계약

## 현재 동작과 문제

배포 상세는 Worker가 Control PostgreSQL에 저장한 구조화 deployment event를 최대 100건까지
시간순으로 보여준다. 이 event에는 단계·stable code·bounded message만 있으며 application
stdout/stderr 원문은 포함하지 않는다.

사용자가 실제 service container의 최근 실행 로그를 확인하려면 현재는 host에서 Docker CLI를 직접
실행해야 한다. Heimdall API에는 service 이름 목록이나 container log 조회 계약이 없고, Frontend도
service별 로그 상태를 표시할 수 없다.

현재 아키텍처는 Worker만 Docker socket을 사용하며 API process와 project container에는 Docker
socket을 전달하지 않는다. API에서 `docker logs`를 직접 실행하게 만들면 HTTP 공격면이 host 전체
권한을 가진 Docker socket으로 연결되므로 기존 격리 원칙을 훼손한다.

## 목표

- 배포 상세에서 immutable snapshot에 포함된 service 목록을 선택한다.
- 선택한 service container의 stdout/stderr 최근 200줄을 timestamp와 stream 구분과 함께 조회한다.
- API process에 Docker socket을 전달하지 않고 Worker만 Docker CLI를 실행한다.
- deterministic container 이름과 exact managed·project·deployment label을 모두 확인한 뒤 로그를 읽는다.
- Heimdall이 관리하는 project environment secret과 managed database password를 응답 전에 마스킹한다.
- 조회 결과는 Control DB와 runtime root에 저장하지 않고 요청 응답 동안에만 메모리에 유지한다.
- 첫 단계는 snapshot 조회와 수동 새로고침만 제공하고 실시간 streaming은 별도 Plan으로 분리한다.

## 범위

- runtime root 아래 owner-only Unix domain socket을 통한 API → Worker 로컬 로그 조회
- Worker의 bounded request parser와 Docker service log reader
- `GET /api/deployments/{deploymentId}/service-logs?service={serviceName}` 공개 API
- service를 생략하면 root route가 가리키는 service를 기본 선택
- 응답의 service 이름 목록, 선택 service, 조회 시각, stdout/stderr line 목록
- fixed tail 200, line별 최대 16KiB, 전체 응답 최대 256KiB
- exact container label 확인, container 없음·Worker 없음·redaction 실패의 stable error
- 배포 상세의 service selector, 새로고침, loading·empty·unavailable·error 상태
- Backend 계약·runtime·IPC 단위 테스트와 Frontend 화면 테스트
- 승인된 test-owned container를 이용한 실제 Docker log smoke

## 비범위

- SSE, WebSocket, follow mode 또는 자동 polling 기반 실시간 streaming
- 로그 검색, 다운로드, 장기 보존, Control DB 저장과 외부 log backend
- gateway·Docker daemon·Heimdall Worker 자체 로그
- 과거 generation container 복원 또는 이미 정리된 container 로그 복구
- project application이 자체 생성한 개인정보·token·DB row 전체를 완전하게 판별하는 DLP
- 사용자별 권한 모델과 multi-host log aggregation

## 공개 계약

```text
GET /api/deployments/{deploymentId}/service-logs?service=web

200
{
  "deploymentId": "...",
  "services": ["web", "api"],
  "serviceName": "web",
  "retrievedAt": "...",
  "lines": [
    {
      "timestamp": "2026-08-07T01:00:00.123456789Z",
      "stream": "STDOUT",
      "message": "server started"
    }
  ],
  "truncated": false
}
```

- `service`는 deployment snapshot의 service 이름과 정확히 일치해야 한다.
- service 생략 시 `/` route의 target service를 선택한다.
- line은 Docker timestamp 기준으로 stdout/stderr를 다시 합쳐 오래된 순서로 반환한다.
- container가 아직 생성되지 않았거나 이미 정리되었으면
  `409 SERVICE_LOGS_UNAVAILABLE`을 반환한다.
- Worker socket이 없거나 제한 시간 안에 응답하지 않으면
  `503 RUNTIME_LOG_BROKER_UNAVAILABLE`을 반환한다.
- secret reference를 읽고 redaction value를 준비하지 못하면 로그를 반환하지 않고
  `503 SERVICE_LOG_REDACTION_UNAVAILABLE`로 fail closed 한다.

## 데이터·보안·외부 효과

### Docker 권한 경계

- API는 Docker CLI나 socket을 사용하지 않는다.
- Worker는 runtime root의 고정 socket path에서 local request만 받는다.
- socket parent는 symlink가 아닌 owner-only directory이고 socket file은 mode `0600`으로 만든다.
- request는 version, deployment UUID, service name과 fixed tail만 포함하며 최대 byte를 제한한다.
- Worker 종료 시 자기 socket만 제거하고 일반 file·symlink·소유권이 불확실한 path는 덮어쓰거나
  삭제하지 않는다.

### Container 소유권

- service는 immutable deployment snapshot에서 해석한다.
- deterministic name만 계산하고 임의 container 이름을 request로 받지 않는다.
- `heimdall.managed=true`, project ID와 deployment ID label이 모두 정확히 일치해야
  `docker logs --tail 200 --timestamps`를 실행한다.
- label이 다르거나 inspect가 불확실하면 로그를 읽지 않으며 container를 변경하거나 삭제하지 않는다.
- 로그 조회는 read-only이며 deployment 상태, runtime metadata와 container lifecycle을 바꾸지 않는다.

### 민감정보

- Worker는 선택 service의 secret environment reference와 해당 service가 사용하는 managed database
  password reference를 SecretStore에서 읽어 exact raw value를 `[REDACTED]`로 치환한다.
- secret을 읽지 못하면 원문을 반환하지 않는다.
- redaction 뒤의 line만 Unix socket과 HTTP 응답으로 전달한다.
- raw·redacted log 모두 Control DB, deployment event와 runtime root file에 저장하지 않는다.
- response size를 초과한 뒤의 내용은 버리고 `truncated=true`로 표시한다.

application이 secret을 encoding·hashing·분할한 값이나 Heimdall이 모르는 개인정보와 credential을
출력하면 exact-value redaction으로 완전히 탐지할 수 없다. 따라서 이 기능은 신뢰된 단일 관리자만
사용한다는 현재 제품 전제를 유지하며, UI에 application log가 민감정보를 포함할 수 있다는 경고를
표시한다.

## 선택한 방향과 감수한 단점

- API에 Docker socket을 주지 않고 Worker를 local log broker로 사용한다. 별도 IPC 코드와 Worker
  background server lifecycle이 추가되지만 HTTP 취약점이 곧바로 host Docker 권한으로 이어지는
  위험을 피한다.
- Unix socket은 한 host의 API·Worker가 runtime root를 공유한다는 현재 배포 형태에 맞는다. API와
  Worker가 다른 host로 분리되면 별도 인증된 log agent 또는 backend가 필요하다.
- 첫 버전은 최근 200줄 snapshot과 수동 새로고침만 제공한다. 구현과 resource 사용이 bounded하지만
  새 로그가 자동으로 흐르지는 않는다.
- known secret exact-value redaction은 현재 Heimdall 관리 secret 누출을 막지만 일반 DLP는 아니다.
  완전한 비밀정보 탐지를 보장할 수 없다는 잔여 위험을 관리자에게 명시한다.
- container가 이전 generation cleanup으로 제거되면 Docker log도 사라진다. 장기 로그 보존은 별도
  storage 정책 없이는 제공하지 않는다.

## 수직 단계와 검증

### 1. Bounded Docker log reader와 redaction

- process result가 stdout과 stderr를 bounded하게 관찰하도록 하되 기존 command call과 호환한다.
- deployment snapshot의 service를 exact name으로 선택한다.
- deterministic container와 exact labels를 검증하고 최근 200줄만 읽는다.
- Docker timestamp로 두 stream을 정렬하고 line·response 상한을 적용한다.
- known secret과 database credential을 마스킹하며 secret resolution 실패는 fail closed 한다.
- unit test에서 argument list, label mismatch, missing container, ordering, truncation과 secret canary를
  검증한다.

안전한 중단 지점: Worker 내부 read-only reader와 테스트만 존재하며 API에는 노출되지 않는다.

- 구현 증거 (`2026-08-10`): stdout·stderr 분리 capture, exact 3-label inspect, fixed tail 200,
  16KiB line 제한, timestamp merge와 project/database secret fail-closed redaction을 구현했다.
- 검증: reader·기존 Docker/NGINX 관련 targeted test 통과.

### 2. Worker local broker와 API 계약

- Worker startup·shutdown에 owner-only Unix socket server lifecycle을 연결한다.
- protocol version과 request·response size, 동시 처리 수와 timeout을 제한한다.
- API log client와 deployment log service를 추가해 Router는 HTTP 변환만 수행한다.
- 공개 endpoint의 기본 root service, service validation과 stable error를 계약 테스트로 검증한다.

안전한 중단 지점: endpoint를 Frontend에서 사용하지 않아 기존 UI는 그대로 동작한다. rollback은
broker와 endpoint wiring 제거만 필요하며 DB migration은 없다.

- 구현 증거 (`2026-08-10`): owner-only `logs.sock`, versioned bounded JSON protocol, 최대 동시 처리
  4개, 256KiB 응답 제한과 API stable error mapping을 구현했다. unsafe/active socket은 덮어쓰지 않고
  broker startup 실패 시 deployment loop를 계속한다.
- 검증: broker lifecycle·timeout·동시성 및 deployment service/router 계약 targeted test 통과.

### 3. 배포 상세 service log snapshot UI

- 구조화 deployment event와 별도인 `서비스 로그` 영역을 추가한다.
- snapshot의 service selector, 최근 200줄, stream 표시와 수동 새로고침을 제공한다.
- unavailable·Worker down·redaction failure를 구분해 운영자가 다음 행동을 이해하도록 표시한다.
- application log의 잔여 민감정보 위험과 비영속 범위를 화면에 표시한다.
- Testing Library로 service 선택, 긴 line, empty/error와 새로고침을 검증한다.

- 구현 증거 (`2026-08-10`): 배포 상세에 service selector, stdout/stderr 구분, 조회 시각, 수동
  새로고침, empty·unavailable·redaction error와 비영속·잔여 민감정보 경고를 추가했다.
- 검증: 상세 화면 테스트 6개와 TypeScript production build 통과.

### 4. 집계 gate와 실제 smoke

- Backend Ruff format·lint·pytest와 Frontend `pnpm verify`를 실행한다.
- test-owned container가 stdout·stderr, multiline과 known secret canary를 출력하도록 한다.
- API 응답이 tail·순서·stream·상한을 지키고 canary를 포함하지 않는지 확인한다.
- container와 socket은 exact test target만 정리하며 기존 deployment resource를 변경하지 않는다.
- 실제 배포 상세에서 service 선택과 수동 새로고침을 확인한다.

- 완료 검증 (`2026-08-10`): Backend Ruff format·lint 통과, pytest `100 passed, 9 skipped`,
  Frontend `pnpm verify`에서 `18 passed`와 production build 통과, Chromium E2E `3 passed`.
- 실제 Docker smoke: test-owned container가 stdout·stderr에 출력한 known secret을 모두
  `[REDACTED]`로 응답하고 raw canary를 포함하지 않았으며, 종료 뒤 container·network·image가
  남지 않음을 확인했다 (`1 passed`).

## 인수 조건

- API process는 Docker socket 없이 service log snapshot을 조회한다.
- Worker가 없으면 bounded timeout 뒤 stable 503으로 종료한다.
- 선택 service가 snapshot에 없거나 container label이 다르면 Docker logs를 실행하지 않는다.
- 응답은 service당 최근 200줄과 256KiB 이하이며 Control DB·filesystem에 log를 남기지 않는다.
- stdout/stderr가 timestamp 순으로 구분되어 표시된다.
- known project secret과 database password canary가 socket·API·UI·test output에 나타나지 않는다.
- secret redaction 준비가 실패하면 로그 원문을 반환하지 않는다.
- 이미 정리된 generation은 unavailable로 표시하며 복원이나 Docker mutation을 하지 않는다.
- 구조화 deployment event 화면과 기존 배포·복구 동작이 회귀하지 않는다.

## rollback과 안전한 중단

- DB migration이 없으므로 endpoint, broker wiring과 UI를 제거하면 기존 구조화 event 전용 상태로
  돌아간다.
- socket path가 안전하지 않거나 이미 다른 object가 있으면 Worker는 log broker만 비활성화하고 기존
  deployment processing을 계속한다.
- Docker inspect·logs 실패는 deployment 상태를 변경하지 않는다.
- 실제 smoke는 test-owned managed label container만 사용하고 `finally`에서 정확한 이름으로 정리한다.

## 문서 영향

- `project-docs/architecture.md`: Worker local log broker, Docker 권한 경계와 redaction 흐름
- `project-docs/project-profile.md`: 구조화 event 외 bounded application log snapshot 예외와 잔여 위험
- `README.md`: Worker socket, API·Worker 동시 실행 요구와 서비스 로그 범위

## 완료된 결정과 후속

- 승인 완료: API에 Docker socket을 주지 않고 Worker local Unix socket broker를 추가했다.
- 승인 완료: known Heimdall secret은 fail-closed exact redaction하되 application이 출력하는 알 수 없는
  개인정보·credential까지 완전 탐지할 수 없다는 잔여 위험을 단일 관리자 전제에서 수용했다.
- 후속 후보: live follow는 snapshot 기능의 resource·보안 동작을 확인한 뒤 SSE 또는 WebSocket 중
  하나를 별도 Plan으로 결정했고, `2026-08-10-service-log-sse.md`에서 SSE로 구현·검증했다.
