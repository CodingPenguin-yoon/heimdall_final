# 실행 중 Gateway 네트워크 Rebase Plan

- 상태: `COMPLETED`
- 날짜: `2026-08-11`
- 승인 근거: 사용자가 새 preview 배포마다 실행 중 Gateway를 candidate network 기준으로
  재생성하고 동일 Preview 포트를 재검증하는 방향으로 구현 시작을 승인함
- 관련 Plan: `2026-08-06-stopped-gateway-recovery.md`

## 현재 동작과 문제

프로젝트 Gateway는 첫 배포 generation network를 주 네트워크로 생성한 뒤, 다음 배포에서는 실행
중인 동일 컨테이너를 candidate network에 추가 연결한다. NGINX config reload와 host Preview route
probe를 통과하면 Control DB active metadata를 candidate로 바꾸고, 마지막에 이전 generation
network에서 Gateway를 분리한다.

Docker Desktop에서는 Gateway가 최초로 생성된 주 네트워크를 분리하면 컨테이너 내부 NGINX와
candidate upstream은 정상이어도 기존 `127.0.0.1:{previewPort}` publish가 응답하지 않는다. 현재
route probe는 이전 network를 분리하기 전에 실행되므로 이 상태를 발견하지 못하고 deployment를
`SUCCEEDED`로 확정한다.

실제 `heimdall-test` 배포에서도 Backend 직접 호출, Web을 통한 Backend 호출과 Gateway 내부 호출은
모두 DB 연결을 포함해 성공했지만 stable Preview 포트의 host 요청만 timeout됐다. Control DB와
Docker가 관찰한 Preview 포트 번호는 같았으므로 포트 변경이 아니라 Gateway의 최초 network endpoint
제거 뒤 host publish가 stale해지는 것이 실패 경계다.

## 목표

- 기존 active network와 다른 candidate network로 전환할 때 실행 중 Gateway도 network rebase한다.
- candidate config를 기존 Gateway에서 먼저 검증한 뒤 동일 Preview 포트로 candidate network에서
  Gateway를 재생성한다.
- 재생성된 Gateway의 host Preview route를 다시 검증한 뒤에만 Control DB active metadata를 바꾸고
  이전 generation을 회수한다.
- 재생성 또는 최종 probe가 실패하면 이전 active network에서 last-known-good Gateway를 복구하고
  기존 active metadata와 application generation을 보존한다.

## 범위

- 실행 중 exact managed Gateway의 active network와 candidate network 차이 감지
- 기존 Gateway activation rebase·restore 경로 재사용
- 실행 중 Gateway 성공 전환과 실패 복원 단위 회귀 테스트
- 실제 Docker에서 실행 중 Gateway를 거치는 연속 두 배포와 stable Preview port smoke
- Runtime architecture와 현재 운영 설명 동기화

## 비범위

- 배포 요청이 없을 때의 주기적 Gateway watchdog 또는 자동 healing
- application service container 장애 감지·재시작
- 새 deployment 상태, DB schema, 공개 API 또는 Preview URL 계약 변경
- public domain, TLS, multi-host routing

## 외부 효과와 실패 영향

1. Gateway 이름, managed·project·gateway label과 running 상태를 기존 방식으로 검증한다.
2. stored active network와 candidate network가 다를 때만 running Gateway rebase를 예약한다.
3. candidate network 연결, NGINX config test·reload와 1차 host route probe를 먼저 통과한다.
4. exact managed Gateway를 제거하고 candidate network의 동일 stored Preview 포트에서 재생성한다.
5. Docker published port가 stored port와 같은지 확인하고 모든 route를 host에서 다시 probe한다.
6. 2차 probe 뒤에만 Control DB active metadata를 candidate로 전환하고 이전 generation을 회수한다.
7. Gateway 재생성이나 2차 probe가 실패하면 current config를 이전 값으로 복원하고 stored active
   network에서 동일 Preview 포트로 Gateway를 복구한다.
8. 복구 실패도 기존 application generation, Managed PostgreSQL과 secret을 삭제하지 않는다.

## 선택한 방향과 감수한 단점

- 기존 `_recreate_gateway_on_network`와 `_restore_previous_gateway` 경로를 실행 중 Gateway에도
  사용한다. 새 복구 체계를 만들지 않아 변경 범위는 작지만 배포 전환마다 Gateway가 짧게 재생성된다.
- stable port와 last-known-good 보존을 우선하고 무중단 Gateway identity는 보장하지 않는다.
- 고정 Gateway 전용 network는 장기 대안이지만 network lifecycle과 cleanup 계약이 커지므로 이번
  수정에는 포함하지 않는다.

## 수직 단계와 검증

### 1. 실행 중 Gateway 회귀 테스트

- active network와 candidate network가 다르면 동일 Preview 포트로 candidate network 재생성을
  요구하는 테스트를 먼저 실패시킨다.
- rebase 실패 시 previous network 복구와 DB active 보존을 검증한다.

안전한 중단 지점: 테스트만 추가되고 production Docker 동작은 바뀌지 않는다.

### 2. 최소 activation 수정

- running/stopped 구분과 무관하게 stored active network가 candidate와 다르면 rebase를 예약한다.
- 기존 2차 route probe와 rollback 경로를 그대로 사용한다.
- gateway 단위 테스트와 Ruff를 실행한다.

### 3. 실제 Docker smoke와 집계 gate

- test-owned UUID resource로 실행 중 Gateway를 거치는 연속 두 배포를 실행한다.
- 두 번째 배포 뒤 동일 Preview 포트가 새 deployment marker와 응답을 반환하는지 확인한다.
- backend 전체 pytest·Ruff와 frontend `pnpm verify`를 실행한다.

## 인수 조건

- 실행 중 Gateway의 active network가 candidate와 다르면 candidate network에서 재생성된다.
- 전환 전후 Preview 포트가 Control DB의 stored port와 동일하다.
- 재생성 뒤 host Preview가 candidate deployment marker를 반환한 후에만 active metadata가 바뀐다.
- 재생성 실패 시 previous active deployment·network·application resource가 보존된다.
- first deployment처럼 stored active network가 없는 경우 기존 dynamic port 생성 동작을 유지한다.
- unmanaged 또는 다른 project label의 동명 Gateway는 변경하지 않는다.

## 안전한 중단과 rollback

- DB schema와 공개 API 변경이 없어 코드 rollback으로 기존 activation 동작으로 돌아갈 수 있다.
- actual smoke는 random project·deployment ID의 exact managed label resource만 만들고 `finally`에서
  해당 자원만 정리한다.
- broad Docker cleanup과 기존 사용자 project resource 변경은 금지한다.

## 문서 영향

- `project-docs/architecture.md`: 모든 generation 전환에서 Gateway를 candidate 주 네트워크로 rebase
- `project-docs/project-profile.md`: stable Preview port 전환 완료 조건
- `README.md`: activation 단계의 running Gateway 재생성과 최종 host probe

## 구현 중 검증 기록

- 실행 중 Gateway를 재사용하는 기존 단위 테스트를 candidate network 동일 포트 재생성과 2차 host
  probe를 요구하도록 바꿨고, production 수정 전 `docker rm --force`가 호출되지 않아 실패하는 것을
  확인했다.
- running managed Gateway의 stored active network가 candidate와 다를 때만 기존 rebase 경로를
  활성화했다. first deployment와 이미 같은 network인 경우에는 rebase하지 않는다.
- candidate network Gateway 재생성 실패 시 previous network와 config를 복구하고 active metadata와
  application generation을 보존하는 단위 테스트를 추가했다.
- gateway 단위 테스트 `12 passed`와 관련 Ruff check가 통과했다.
- random project·deployment UUID의 test-owned Docker 자원으로 첫 배포 뒤 실행 중 Gateway를 거치는
  두 번째 배포를 수행했고, 동일 Preview 포트가 새 deployment marker와 응답을 반환했다. 이어
  Gateway를 정지한 세 번째 배포도 동일 포트에서 복구됐으며 smoke `1 passed` 후 exact test 자원을
  정리했다.
- backend 집계 gate는 Ruff format·lint와 pytest `127 passed, 11 skipped`로 통과했다.
- frontend `pnpm verify`는 9개 test file·22개 test, format·lint·typecheck와 production build까지
  통과했다.
- 로컬 Worker가 수정 전 프로세스로 남아 있던 상태에서 실제 재배포가 실패한 뒤, Worker를 수정본으로
  재기동하고 기존 active Gateway를 동일 포트에서 복구했다. 같은 commit의 후속 배포
  `25525480-d64f-4af1-9ec0-10dc952b1f68`은 성공했고, Gateway primary network와 Control DB active
  network가 `hm-pa8ad7e0df8aa-g25525480d64f`로 일치했다. Preview 포트 `55468`은 유지됐으며 Web과
  `/api/status`가 모두 HTTP 200, managed database 연결이 `true`임을 확인했다.
