# 서비스 로그 SSE Plan

- 상태: `COMPLETED`
- 날짜: `2026-08-10`
- 승인: 사용자가 기존 snapshot 변경을 커밋·푸시한 뒤 SSE 실시간 로그를 진행하도록 승인
- 선행 기능: `2026-08-07-service-log-snapshot.md`

## 현재 동작과 문제

배포 상세는 화면 진입 시와 사용자가 `새로고침`을 누를 때만 선택한 service container의 최근
200줄 snapshot을 조회한다. API는 owner-only `logs.sock`을 통해 Worker에 요청하고 Worker만 Docker
CLI를 실행하며, 알려진 project secret과 managed database password는 Worker에서 fail-closed
마스킹한다.

운영자가 배포 직후 애플리케이션 기동 과정을 보려면 새로고침을 반복해야 한다. 기존 snapshot
broker는 최대 4개의 짧은 request·response를 전제로 하므로 장시간 follow 연결을 같은 처리 슬롯에
넣으면 SSE client 몇 개만으로 수동 snapshot까지 고갈될 수 있다. 또한 HTTP 연결 종료가 Docker
follow process 종료로 전파되지 않으면 Worker에 child process가 누적된다.

## 목표와 인수 조건

- 배포 상세에서 선택 service의 최근 200줄과 이후 stdout·stderr가 SSE로 이어서 표시된다.
- service를 바꾸면 기존 연결과 Docker follow process를 종료하고 새 service로 연결한다.
- 브라우저 연결이 끊기거나 Worker가 종료되거나 container log follow가 끝나면 관련 socket,
  thread와 child process를 bounded 시간 안에 정리한다.
- 네트워크 단절 시 브라우저가 다시 연결하고, 새 연결의 최근 200줄로 화면 buffer를 교체해 중복을
  누적하지 않는다.
- API에는 Docker socket을 전달하지 않고 exact container ID·managed/project/deployment label 검증과
  secret redaction을 계속 Worker가 소유한다.
- line은 최대 16KiB, 화면 buffer는 service당 최근 200줄, Worker live stream은 최대 4개로 제한한다.
- 기존 snapshot endpoint와 수동 `새로고침`을 fallback으로 유지한다.
- raw·redacted 로그 모두 Control DB와 filesystem에 저장하지 않는다.

## 범위

- `GET /api/deployments/{deploymentId}/service-logs/stream?service={serviceName}` SSE endpoint
- SSE `ready`, `log`, `end`, `stream-error` event와 keepalive comment
- runtime root의 별도 owner-only `log-stream.sock`과 bounded streaming protocol
- Worker의 `docker logs --tail 200 --follow --timestamps <immutable-container-id>` lifecycle
- stdout·stderr line 단위 redaction·크기 제한과 backpressure
- Frontend 연결 상태, 자동 재연결, service 전환, 최근 200줄 buffer
- 기존 snapshot 새로고침 fallback
- Backend protocol·cleanup·API 계약 테스트, Frontend 단위·E2E 테스트
- test-owned container로 disconnect·secret redaction·cleanup을 확인하는 Docker smoke

## 비범위

- WebSocket 양방향 protocol
- 로그 검색·다운로드·장기 보존·외부 log backend
- multi-host Worker routing과 사용자별 권한 모델
- 과거 cursor의 완전한 resume와 한 줄도 빠지지 않는 delivery 보장
- Heimdall이 알지 못하는 개인정보·credential의 DLP
- gateway, Docker daemon 또는 Worker 자체 로그

## 공개 계약

```text
GET /api/deployments/{deploymentId}/service-logs/stream?service=web
Accept: text/event-stream

event: ready
data: {"deploymentId":"...","services":["web","api"],"serviceName":"web","connectedAt":"..."}

event: log
data: {"timestamp":"...","stream":"STDOUT","message":"server started","truncated":false}

: keepalive

event: end
data: {"reason":"CONTAINER_LOG_ENDED"}
```

- endpoint는 `text/event-stream`, `Cache-Control: no-store`, `X-Accel-Buffering: no`로 응답한다.
- deployment·service·container·redaction 준비 오류가 첫 `ready` 전에 발생하면 기존 stable HTTP error로
  응답한다.
- 연결 이후 오류는 `stream-error` event의 stable code로 알린 뒤 연결을 닫는다.
- client가 끊어지면 API subscription을 닫고 Worker socket send 실패 또는 keepalive에서 이를 감지해
  Docker follow를 종료한다.
- native `EventSource` 재연결은 cursor resume 대신 새 session의 tail 200으로 UI buffer를 교체한다.
  따라서 transient disconnect 구간에 정확히 한 번 전달되는 것을 보장하지 않지만 중복이 계속 쌓이지
  않는다.

## 데이터·보안·외부 효과

### 권한과 소유권

- API는 고정 `log-stream.sock`에 deployment UUID, 검증된 service name과 protocol version만 보낸다.
- socket parent는 symlink가 아닌 현재 사용자 소유 `0700`, socket은 `0600`이어야 한다.
- Worker는 immutable deployment snapshot에서 service와 deterministic container 이름을 해석하고
  `heimdall.managed`, project ID, deployment ID label이 모두 정확한 container ID만 follow한다.
- 임의 Docker argument, container name 또는 tail 크기를 HTTP·IPC 입력으로 받지 않는다.

### Secret과 크기 제한

- follow process를 시작하기 전에 선택 service의 known secret을 모두 읽고 line-safe redaction 가능성을
  검증한다. 준비 실패 시 Docker logs를 시작하지 않는다.
- 각 line은 Worker에서 exact redaction한 뒤에만 socket으로 전달한다. oversized line은 redaction 후
  16KiB로 자르고 `truncated=true`로 표시한다.
- application이 encoding·분할한 secret이나 Heimdall이 모르는 개인정보를 출력할 수 있다는 기존 잔여
  위험과 단일 관리자 전제를 UI에 계속 표시한다.

### Resource와 backpressure

- snapshot broker와 live stream broker의 동시성 한도를 분리해 live client가 snapshot fallback을
  막지 않게 한다.
- Worker live stream은 최대 4개다. stdout·stderr reader와 전송 사이에는 bounded queue를 사용하며
  느린 client에는 무한 메모리 적재 대신 process pipe·socket backpressure가 전달된다.
- protocol frame, socket read·write와 initial handshake에는 크기·시간 제한을 둔다. keepalive로 출력이
  없는 연결의 단절도 감지한다.
- generator close, socket failure, Worker stop에서 process group에 terminate 후 bounded wait와 kill을
  적용한다.

## 선택한 방향과 감수한 단점

- 로그는 서버에서 브라우저로만 흐르므로 WebSocket보다 HTTP 운영과 브라우저 재연결이 단순한 SSE를
  선택한다.
- live stream 전용 socket을 둬 snapshot broker를 보호한다. socket lifecycle 코드가 하나 늘지만
  장기 연결과 단기 요청의 capacity·timeout을 독립적으로 관리할 수 있다.
- reconnect cursor를 영속화하지 않고 매 session tail 200을 다시 받는다. 정확한 delivery보다 비영속
  preview 운영 로그의 단순성과 bounded resource를 우선한다.
- stdout·stderr는 별도 pipe에서 도착한 순서로 전송한다. Docker timestamp는 보존하지만 두 stream의
  같은 시각에 대한 전역 total order는 보장하지 않는다.

## 수직 단계와 검증

### 1. Bounded Docker follow와 취소

- generic subprocess stream adapter에 bounded queue, stdout·stderr 구분, keepalive tick과 close 시
  process-group 정리를 추가한다.
- Docker adapter가 snapshot과 같은 exact inspect·secret redaction 규칙으로 ready·log·end event를
  만든다.
- unit test에서 argument list, redaction, oversized line, process exit와 consumer cancel cleanup을
  검증한다.

안전한 중단 지점: Worker 내부 adapter와 테스트만 존재하며 HTTP에는 노출되지 않는다.

- 구현 증거 (`2026-08-10`): bounded stdout·stderr queue와 line reader, process-group
  terminate/kill, immutable container ID 기반 `docker logs --tail 200 --follow --timestamps`와
  Worker-side byte redaction을 구현했다.
- 검증: stdout·stderr, oversized/invalid UTF-8 line, nonzero exit, consumer close process cleanup과
  project/database secret redaction 단위 테스트 통과.

### 2. Streaming broker와 SSE API

- 별도 owner-only socket server/client와 versioned newline JSON frame을 추가한다.
- initial ready 전 stable error는 HTTP error로, ready 이후 error는 stream event로 변환한다.
- API disconnect가 subscription close로 전파되는지, 동시성 초과가 snapshot과 분리되는지 검증한다.

안전한 중단 지점: 공개 endpoint를 Frontend가 아직 사용하지 않으며 snapshot 화면은 그대로 동작한다.

- 구현 증거 (`2026-08-10`): 별도 `log-stream.sock`, 최대 live stream 4개, 5초 keepalive와
  versioned bounded JSON frame을 구현하고 FastAPI SSE `ready/log/end/stream-error` 계약을 연결했다.
- 검증: owner-only socket, round trip, client disconnect, capacity isolation, fail-closed error와 Router
  SSE 계약 테스트 통과. Router의 runtime 직접 import 없이 feature 경계를 유지했다.

### 3. 배포 상세 live UI

- service 선택마다 하나의 EventSource만 유지하고 ready 시 최근 buffer를 교체한다.
- `연결 중·실시간·재연결 중·종료·오류` 상태를 표시하며 최대 200줄만 유지한다.
- 수동 새로고침은 기존 snapshot endpoint를 호출해 fallback으로 사용할 수 있게 남긴다.
- Testing Library와 Playwright로 연결, service 전환, line append, reconnect reset과 fallback을 검증한다.

- 구현 증거 (`2026-08-10`): native EventSource 자동 재연결, ready 시 tail buffer 교체, 최근 200줄
  제한, service 전환, 연결 상태 표시와 수동 snapshot fallback을 구현했다.
- 검증: Frontend 9개 test file의 19개 테스트와 production build, Chromium E2E 3개 통과.

### 4. 집계 gate와 실제 smoke

- Backend Ruff format·lint·pytest와 Frontend `pnpm verify`, Chromium E2E를 실행한다.
- 고유 이름의 test-owned container만 생성해 tail+follow, stdout/stderr secret canary redaction과 client
  disconnect 뒤 follow process·container fixture cleanup을 확인한다.
- 기존 배포 container, network, image와 Control DB 상태는 변경하지 않는다.

- 완료 검증 (`2026-08-10`): Backend Ruff format·lint 통과, pytest `118 passed, 10 skipped`,
  Frontend `pnpm verify`와 Chromium E2E `3 passed`.
- 실제 Docker smoke: 고유 test-owned container의 startup tail과 연결 이후 stdout·stderr 4줄에서 known
  secret이 모두 `[REDACTED]`로 치환되고 raw canary가 없으며 fixture network가 정리됨을 확인했다
  (`1 passed, 2 deselected`).
- 실제 local runtime: API·Worker를 재시작해 runtime directory `0700`, 두 socket `0600`, API health와
  기존 성공 deployment의 SSE `ready → log`를 확인했다. client close 뒤 `docker logs --follow`
  process가 남지 않았다.

## Rollback과 안전한 중단

- DB migration이 없고 기존 snapshot 계약을 유지하므로 Frontend의 EventSource 사용과 SSE endpoint,
  stream broker wiring을 제거하면 즉시 수동 snapshot 상태로 돌아간다.
- stream socket startup 실패는 경고만 남기고 snapshot broker와 deployment Worker loop는 계속 실행한다.
- Docker inspect·logs·stream 실패는 deployment 상태나 runtime metadata를 변경하지 않는다.
- 실제 smoke는 test-owned exact target만 `finally`에서 정리한다.

## 문서 영향

- `project-docs/architecture.md`: live broker, SSE 흐름과 resource cleanup
- `project-docs/project-profile.md`: snapshot-only 규칙을 bounded live stream 허용으로 변경
- `README.md`: stream socket·SSE 동작과 운영 제한
- `.env.example`: 새 외부 설정 없이 fixed capacity·heartbeat를 사용하므로 변경 없음

## 완료된 결정과 후속

- SSE와 snapshot은 별도 owner-only socket·capacity로 운영한다.
- reconnect는 durable cursor 없이 새 tail 200으로 교체하고 snapshot 새로고침을 fallback으로 유지한다.
- multi-host와 durable log backend가 필요해질 때 인증된 log agent와 cursor 보존을 별도 아키텍처로
  결정한다.
