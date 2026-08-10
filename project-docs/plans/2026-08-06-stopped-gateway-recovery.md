# 정지 Gateway 배포 복구 Plan

- 상태: `APPROVED`
- 구현 결과: `COMPLETE`
- 날짜: `2026-08-06`
- 기존 승인: 사용자가 stopped managed gateway를 동일 Preview 포트와 기존 active network로
  재생성하는 방향을 승인함
- 재승인 근거: 실제 Docker smoke에서 기존 active network를 gateway의 주 네트워크로 재사용한 뒤
  해당 network를 분리하면 host publish가 영구적으로 응답하지 않는 동작이 확인되었고, 사용자가
  아래 2단계 재생성 계약으로 계속 진행하는 것을 승인함

## 현재 동작과 문제

프로젝트별 NGINX gateway는 한 번 생성한 컨테이너를 재사용하고 Control DB의
`project_runtimes.preview_port`가 안정 Preview 주소를 소유한다. 현재 activation은 같은 이름의
컨테이너가 있으면 `heimdall.managed`, project ID, gateway kind label만 확인하고 곧바로
`docker port`를 조회한다.

관리자나 외부 운영 작업이 gateway에 명시적인 stop을 보내면 Docker의 `unless-stopped` 정책은
그 컨테이너를 자동 재시작하지 않는다. 정지된 컨테이너도 `docker inspect`와 label 검증은
통과하지만 published port는 관찰되지 않으므로, 다음 배포는 service build·start·health를 모두
통과한 뒤 `ACTIVATION/GATEWAY_PORT_UNAVAILABLE`로 실패한다. Control DB는 기존 deployment와
Preview 포트를 계속 `ACTIVE`로 보유해 실제 Docker 상태와 어긋난다.

실제 재현 상태에서는 gateway가 `SIGQUIT`을 받고 exit code 0으로 정상 정지했으며, Control DB는
Preview port `55468`과 이전 active deployment를 유지했다. 동일 commit 재배포 여부는 실패 조건이
아니며 여러 deployment가 같은 commit을 사용하는 것은 기존 승인 계약이다.

초기 승인안대로 실제 Docker smoke를 실행한 결과, gateway를 기존 active network에서 재생성하고
candidate network를 연결해 새 route probe까지는 성공했다. 그러나 이전 network를 분리한 뒤에는
컨테이너가 `running=true`여도 동일 host port가 10초 이상 응답하지 않았다. 따라서 gateway가 처음
붙어 시작한 주 네트워크를 activation 완료 시 제거하는 방식은 stable Preview 계약을 만족하지 않는다.

## 목표

- 새 배포 activation이 exact Heimdall label을 가진 정지 gateway를 구분한다.
- 정지 gateway를 저장된 동일 Preview 포트로 안전하게 재생성한다.
- 기존 active network와 last-known-good NGINX config로 1차 복원한 뒤 새 candidate를 연결한다.
- candidate route를 검증한 gateway는 candidate network를 주 네트워크로 사용하도록 동일 포트에서
  한 번 더 재생성하고 재검증한 뒤 이전 generation을 회수한다.
- unmanaged 이름 충돌, 실행 중 gateway와 애플리케이션 generation은 변경하지 않는다.
- 같은 commit을 포함한 다음 배포가 gateway 정지 상태에서 정상적으로 수렴한다.

## 범위

- gateway inspect 결과에 managed label과 실제 running 상태 포함
- exact managed·project·gateway label을 가진 stopped container만 교체
- Control DB에 runtime이 있으면 저장된 Preview port와 기존 active network로 1차 복원
- 검증된 candidate 전환 뒤 gateway를 candidate network와 동일 Preview port로 2차 재생성
- runtime이 아직 없으면 기존 첫 배포의 dynamic loopback port 생성 계약 유지
- stopped gateway unit regression test
- 실제 Docker에서 성공 배포 → gateway stop → 다음 배포 성공과 stable port 유지 smoke
- architecture와 현재 Runtime 문서의 gateway 복구 계약 동기화

## 비범위

- 새 deployment 상태, DB migration 또는 공개 API 변경
- background heartbeat나 주기적인 gateway 자동 healing
- 배포 요청 없이 외부 stop을 즉시 감지하는 운영 감시
- application service container 장애 자동 복구
- unmanaged container 삭제, project 전체 cleanup 또는 broad Docker prune
- public domain, TLS와 multi-host routing

## 외부 효과와 안전 조건

1. gateway 이름으로 inspect하되 이름만으로 소유권을 판단하지 않는다.
2. 컨테이너가 실행 중이면 현재 port 관찰 경로를 그대로 사용한다.
3. 컨테이너가 정지 상태여도 label이 하나라도 다르면 `GATEWAY_NAME_CONFLICT`로 종료하고 mutation을
   수행하지 않는다.
4. exact label과 stopped 상태가 함께 확인된 경우에만 해당 gateway 컨테이너 하나를 제거한다.
5. Control DB runtime이 있으면 stored Preview port를 명시하고, active network가 있으면 그 network로
   last-known-good gateway를 먼저 생성한다.
6. candidate network 연결, config test, reload와 route probe를 통과한 뒤, 현재 activation에서 만든
   exact managed gateway만 제거하고 candidate network를 주 네트워크로 동일 포트에서 재생성한다.
7. 2차 gateway가 같은 route marker와 응답을 통과한 뒤에만 DB active 전환과 이전 generation 회수를
   수행한다.
8. 2차 재생성이나 probe가 실패하면 current config를 이전 값으로 복원하고 stored active network에서
   last-known-good gateway 복원을 시도하며 DB active와 이전 application generation은 변경하지 않는다.
9. gateway 재생성이 실패해도 기존 application container·network, Managed PostgreSQL과 secret은
   삭제하지 않는다. 이미 정지했던 stateless gateway만 교체 대상이며 config file은 runtime root에
   보존된다.

실행 중 gateway에서 port가 관찰되지 않는 비정상 상태는 자동 삭제하지 않고 기존
`GATEWAY_PORT_UNAVAILABLE` 실패로 남긴다. 불확실한 running resource보다 자동 복구 범위를 stopped
resource로 제한한다.

## 선택한 방향과 감수한 단점

- `docker start` 대신 stopped gateway를 동일 포트로 재생성한다. dynamic publish로 만든 컨테이너는
  재시작 시 Control DB가 소유한 stable port와 달라질 수 있으므로 포트 정합성을 명시적으로 지킨다.
- gateway는 bind-mounted config를 사용하는 stateless container라 exact label과 stopped 상태를
  확인한 교체가 가능하다. 대신 gateway 자체의 container identity는 새로 생성된다.
- Docker host publish가 gateway의 최초 주 네트워크 분리 후 깨지는 동작을 피하기 위해, stopped 복구
  경로에서만 gateway를 두 번 생성한다. 짧은 추가 전환 시간이 생기지만, candidate network를 주
  네트워크로 고정한 뒤 성공을 확정하므로 이전 network를 안전하게 회수할 수 있다.
- 자동 healing은 다음 deployment 시점에만 수행한다. 구현 범위와 외부 mutation을 작게 유지하지만,
  배포 요청이 없으면 외부 stop 이후 Preview는 계속 중단된 상태다.

## 수직 단계와 검증

### 1. Gateway 상태 관찰 계약

- inspect 응답에서 label과 running 상태를 함께 파싱한다.
- missing, running managed, stopped managed, unmanaged collision을 각각 구분한다.
- 단위 테스트에서 stopped managed만 교체 명령을 허용한다.

안전한 중단 지점: 상태 관찰과 테스트만 추가하고 production mutation 경로는 활성화하지 않는다.

### 2. 동일 포트 재생성과 activation

- stopped gateway를 exact name으로 제거한다.
- stored active network와 Preview port로 gateway를 1차 재생성한다.
- 새 candidate 연결과 config test·reload·route probe를 수행한다.
- 현재 작업에서 만든 gateway를 candidate network와 동일 Preview port로 2차 재생성하고 새 route
  marker를 다시 확인한 뒤 DB 전환과 이전 generation 회수를 수행한다.
- unit regression에서 두 재생성의 명령 순서, 포트·network, 실패 복원과 외부 label 보호를 검증한다.

### 3. 실제 복구 smoke와 집계 gate

- 격리 project 첫 배포 성공과 stable Preview 응답을 확인한다.
- exact gateway만 stop하고 DB runtime이 기존 port와 active deployment를 유지함을 확인한다.
- 다음 배포가 같은 stable port로 성공하며 Preview가 새 deployment marker를 응답하는지 확인한다.
- backend Ruff format·lint·pytest와 승인된 Docker integration을 실행한다.

## 인수 조건

- stopped managed gateway가 있어도 다음 배포는 `GATEWAY_PORT_UNAVAILABLE`로 실패하지 않는다.
- 복구 전후 Preview port는 Control DB에 저장된 값과 동일하다.
- gateway는 기존 active network로 1차 복원된 뒤, 검증된 candidate network를 주 네트워크로 다시
  생성되고 같은 route marker를 응답한다.
- 실행 중 gateway는 제거하거나 재생성하지 않는다.
- unmanaged 또는 project label이 다른 동명 컨테이너에는 remove·start·network mutation을 실행하지
  않는다.
- gateway 재생성 실패 시 기존 application generation, database와 secret을 보존한다.
- 같은 commit을 사용하는 새 deployment도 deployment ID 단위로 정상 activation된다.

## 안전한 중단과 rollback

- DB schema와 공개 API 변경이 없으므로 코드 rollback만으로 기존 동작으로 돌아갈 수 있다.
- gateway를 교체하기 전에 exact label과 stopped 상태가 모두 확인되지 않으면 mutation하지 않는다.
- 실제 smoke는 test-owned project·deployment label resource만 사용하고 `finally`에서 정확한 대상으로
  정리한다.

## 문서 영향

- `architecture.md`: stopped gateway의 다음 배포 시 복구 흐름
- `project-profile.md`: stable Preview port와 gateway 상태 수렴 규칙
- `README.md`: 명시적 gateway stop 이후 다음 배포의 복구 동작

## 남은 결정

- 추천안: stopped gateway만 다음 배포에서 동일 포트로 복구하되, 실제 Docker host publish 특성에
  맞춰 `기존 network 1차 복원 → candidate 검증 → candidate network 2차 재생성·재검증`을 적용한다.
- 후속 후보: 배포 요청 없이 gateway 중단을 감지·복구하는 background reconciliation은 별도 운영
  Plan에서 결정한다.

## 구현 중 검증 기록

- stopped/running/unmanaged 관찰 단위 테스트와 관련 Ruff는 통과했다.
- 초기 승인안의 실제 Docker smoke는 candidate activation probe까지 통과했지만 이전 주 네트워크
  분리 후 host Preview가 10초 이상 응답하지 않아 실패했다. 이 결과로 2단계 재생성안이 필요해졌고
  추가 구현은 재승인 전 중단했으며 이후 사용자가 2단계 재생성안을 승인했다.
- 2단계 재생성과 실패 복원 단위 테스트를 포함한 gateway 테스트 11개가 통과했다.
- 격리 UUID 자원을 사용하는 실제 Docker smoke에서 gateway stop, 동일 Preview 포트 복구,
  candidate network 기준 재생성, 새 deployment marker와 응답을 확인했다.
- backend 집계 gate는 Ruff format·lint와 pytest `75 passed, 8 skipped`로 통과했다.
- frontend `pnpm verify`는 8개 test file·12개 test, typecheck와 production build까지 통과했다.
