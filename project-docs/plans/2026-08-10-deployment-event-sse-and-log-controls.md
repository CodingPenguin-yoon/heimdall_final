# 배포 이벤트 SSE와 로그 제어 Plan

- 상태: `COMPLETED`
- 날짜: `2026-08-10`
- 승인: 사용자가 남은 작업 1번 실시간 서비스 로그 UX와 2번 구조화 배포 이벤트 SSE 진행을 요청
- 선행 기능: `2026-08-10-service-log-sse.md`

## 현재 동작과 문제

서비스 로그는 SSE로 최근 200줄과 새 stdout·stderr를 받지만 새 line마다 항상 아래로 이동한다. 운영자가
이전 line을 읽는 동안에도 화면이 움직이며, 자동 스크롤을 멈추거나 대기 중인 새 로그 수를 확인하고
최신 위치로 돌아가는 제어가 없다.

구조화 deployment event는 Control PostgreSQL의 `deployment_events`에 durable하게 저장되지만 배포
상세와 프로젝트 상세가 active deployment 동안 1초마다 `GET /events`를 호출한다. 변화가 없어도
브라우저·API·DB 요청이 반복되고, 화면의 `실시간` 표시는 실제 stream 연결 상태를 나타내지 않는다.

## 목표와 인수 조건

- 서비스 로그는 SSE를 끊지 않은 채 자동 스크롤을 일시정지·재개할 수 있다.
- 사용자가 이전 로그를 보기 위해 위로 스크롤하면 자동 추적을 멈추고 이후 도착한 line 수를 표시한다.
- `최신 로그로 이동`은 즉시 마지막 line으로 이동하고 자동 추적을 다시 켠다.
- 구조화 event는 초기 durable snapshot 이후 SSE로 새 event만 전달한다.
- SSE event ID는 `deployment_events.id`이며 reconnect의 `Last-Event-ID`와 query cursor 중 큰 값 뒤부터
  재개해 중복을 누적하지 않는다.
- PostgreSQL row가 최종 원본이며 NOTIFY는 깨우기 신호로만 사용한다. 신호 유실 시 bounded cursor
  재조회로 복구한다.
- terminal deployment를 확인하면 남은 event를 모두 보낸 뒤 stream을 종료한다.
- Browser의 deployment event 1초 polling을 제거한다.

## 범위

- `GET /api/deployments/{deploymentId}/events/stream?after={eventId}` SSE endpoint
- SSE `ready`, `deployment-event`, `end`, `stream-error`와 keepalive comment
- event insert transaction의 PostgreSQL `pg_notify` wake-up
- event row cursor 조회와 최대 4개의 API-side LISTEN subscription
- 배포 상세와 프로젝트 상세가 공유하는 event stream hook
- service log 자동 스크롤 일시정지·재개, 새 line count, 최신 위치 이동
- Backend repository/service/router/stream 계약 테스트와 Frontend 단위·E2E 테스트
- 현재 문서와 product scope의 SSE 범위 동기화

## 비범위

- deployment event의 WebSocket 전환
- application service log의 durable cursor와 정확히 한 번 delivery
- 로그 검색·다운로드·장기 저장
- deployment status API와 전역 배포 목록 polling의 전환
- Redis, 외부 message broker와 multi-host fan-out

## 공개 계약

```text
GET /api/deployments/{deploymentId}/events/stream?after=42
Accept: text/event-stream
Last-Event-ID: 44

event: ready
data: {"deploymentId":"...","after":44}

id: 45
event: deployment-event
data: {"id":45,"deploymentId":"...","stage":"BUILDING","code":"IMAGES_BUILDING",...}

: keepalive

event: end
data: {"reason":"DEPLOYMENT_TERMINAL"}
```

- HTTP query `after`와 `Last-Event-ID`는 non-negative bigint decimal만 허용한다.
- 첫 연결은 deployment 존재 여부, subscription capacity와 LISTEN 준비를 확인한 뒤 200을 시작한다.
- 연결 이후 DB 오류는 `stream-error` stable code를 보낸 뒤 종료한다.
- native EventSource reconnect는 마지막으로 받은 SSE `id`를 자동 전송한다.

## 데이터·보안·외부 효과

- `deployment_events` row와 status가 계속 최종 원본이다. DB schema migration은 없다.
- `_insert_event`는 row insert와 같은 transaction에서 deployment UUID와 event ID만 NOTIFY payload로
  보낸다. stage·message, application output과 secret은 payload에 넣지 않는다.
- NOTIFY는 transaction commit 뒤에만 전달되며 유실 가능성을 인정한다. subscription은 wake-up마다
  `id > cursor` row를 조회하고 keepalive timeout에도 한 번 cursor를 재확인한다.
- LISTEN connection은 최대 4개로 제한해 API DB pool 8개 중 최소 4개를 일반 요청에 남긴다.
- connection close 전에 `UNLISTEN`하고 pool에 반환한다. client disconnect가 wait 중이면 bounded
  timeout 뒤 subscription을 정리한다.
- event message는 기존 DB 길이 제한과 Worker의 stable bounded message 규칙을 그대로 사용한다.

## 선택한 방향과 감수한 단점

- 단순히 브라우저 polling을 API 내부 1초 polling으로 옮기지 않고 PostgreSQL LISTEN/NOTIFY를 wake-up
  신호로 사용한다. 별도 broker 의존성 없이 commit 시점에 가깝게 전달되지만 SSE client마다 DB
  connection 하나를 점유하므로 4개로 제한한다.
- durable replay는 NOTIFY payload가 아니라 event ID cursor query가 담당한다. 조회가 한 번 더 필요하지만
  notification 유실·재연결에도 일관성을 유지한다.
- service log `일시정지`는 수집 중단이 아니라 자동 스크롤 정지다. SSE와 redaction은 계속 동작하고
  최근 200줄 buffer도 계속 갱신되므로 resume 시 새 Docker process나 중복 tail을 만들지 않는다.

## 수직 단계와 검증

### 1. Durable event cursor와 PostgreSQL wake-up

- repository에 `id > cursor` 오름차순 조회를 추가한다.
- event insert가 ID만 포함한 NOTIFY를 같은 transaction에 예약한다.
- bounded LISTEN subscription이 backlog, wake-up, timeout safety query, terminal end와 close를 처리한다.
- unit·PostgreSQL integration에서 순서, 다른 deployment 신호 무시, cursor replay와 UNLISTEN을 검증한다.

안전한 중단 지점: 기존 GET event API와 Frontend polling은 그대로 동작한다.

### 2. SSE 공개 계약과 Frontend 전환

- service가 deployment 존재 확인과 stable error mapping을 소유한다.
- Router는 cursor validation과 StreamingResponse 변환만 수행한다.
- 배포 상세·프로젝트 상세의 초기 GET 이후 active deployment event를 EventSource로 추가한다.
- ready/reconnecting/end 상태, event ID dedupe와 terminal final refresh를 검증한다.

안전한 중단 지점: EventSource hook을 제거하면 기존 GET snapshot만 남고 DB event row에는 영향이 없다.

### 3. 서비스 로그 자동 스크롤 제어

- output container가 bottom 근처면 새 line마다 최신 위치를 따른다.
- 위로 스크롤하거나 일시정지 버튼을 누르면 자동 이동을 멈추고 pending line 수를 센다.
- 계속 보기·최신 로그 이동·service 전환·reconnect ready에서 상태를 안전하게 초기화한다.
- Testing Library와 Playwright로 scroll 제어와 계속되는 SSE line 수신을 검증한다.

### 4. 집계 검증과 문서 동기화

- Backend Ruff format·lint·pytest, Frontend `pnpm verify`, Chromium E2E를 실행한다.
- 실제 Control PostgreSQL opt-in test에서 commit 후 notification과 cursor replay를 확인한다.
- `architecture.md`, `project-profile.md`, `product-scope.md`, `README.md`를 현재 동작에 맞춘다.

## Rollback과 안전한 중단

- DB migration이 없으므로 Frontend EventSource와 event stream endpoint·NOTIFY 호출을 제거하면 기존
  snapshot polling 상태로 돌아간다.
- stream capacity 초과나 LISTEN 실패는 배포 처리와 event insert를 막지 않고 해당 SSE 연결만 stable
  503으로 실패한다.
- NOTIFY 실패는 같은 transaction을 실패시킬 수 있으므로 SQL channel은 고정하고 payload는 UUID·ID로
  제한하며 repository integration test를 release gate로 둔다.
- 자동 스크롤 제어는 표시 동작만 바꾸며 수집·redaction·Docker lifecycle을 변경하지 않는다.

## 문서 영향

- `project-docs/architecture.md`: durable event row + LISTEN/NOTIFY SSE 흐름
- `project-docs/project-profile.md`: event polling 규칙을 SSE로 변경
- `project-docs/product-scope.md`: 구현된 service log SSE를 비범위에서 제거
- `README.md`: 두 SSE endpoint, cursor 재연결과 UI 제어

## 남은 결정

- 없음. API를 multi-host로 확장할 때 PostgreSQL connection-per-stream 대신 shared listener나 외부
  authenticated event broker를 별도 Plan으로 결정한다.

## 구현 결과

- PostgreSQL event row insert와 같은 transaction에서 ID-only NOTIFY를 보내고, 최대 4개의 LISTEN
  subscription이 durable cursor replay와 terminal 종료를 처리한다.
- 배포 상세와 프로젝트 상세의 1초 event polling을 공용 EventSource hook으로 교체했다.
- 서비스 로그는 stream을 유지한 채 자동 스크롤 일시정지, 새 line count와 최신 위치 이동을 제공한다.
- Backend 전체 `126 passed, 11 skipped`, Frontend `22 passed`, Chromium E2E `4 passed`와 실제 Control
  PostgreSQL notification/cursor 통합 테스트를 통과했다.
