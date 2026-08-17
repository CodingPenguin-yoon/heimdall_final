# Runtime 복구 강화 Plan

- 상태: `APPROVED`
- 구현 결과: `COMPLETE`
- 날짜: `2026-08-05`
- 승인 근거: 사용자가 실제 active deployment 확인과 반복 Worker crash 제한을 하나의 작업 세트로 진행하도록 승인함

## 현재 동작과 문제

정상 activation은 candidate health 확인, NGINX 설정 교체·reload·route probe, Control DB의
active runtime 갱신 순서로 진행된다. 일반 예외는 last-known-good 설정으로 되돌리지만,
NGINX 전환 뒤 DB 갱신 전에 process가 강제 종료되면 예외 처리 자체가 실행되지 않는다.

새 Worker는 현재 DB의 active deployment만 확인한다. DB가 target deployment를 가리키지
않으면 같은 deployment의 Docker candidate를 먼저 정리하고 다시 build하므로, NGINX가
이미 사용 중인 target container·network를 삭제해 preview를 중단할 수 있다.

또한 job claim마다 attempt는 증가하지만 `worker_max_attempts`는 Worker가 처리 중 예외를
잡은 경우에만 적용된다. process가 반복 종료되면 expired claim이 계속 회수되어 project의
새 deployment를 무기한 차단할 수 있다.

## 목표

- 재시작 Worker가 Control DB, 실제 NGINX generation과 Docker label을 함께 확인한다.
- target generation이 실제로 정상 서비스 중이면 외부 resource를 다시 만들지 않고 DB와
  deployment terminal 상태를 마무리한다.
- 이전 generation이 실제 서비스 중일 때만 target candidate를 정리하거나 다시 build한다.
- 실제 generation을 확정할 수 없으면 candidate를 삭제하지 않는다.
- 반복 process crash도 설정된 최대 attempt 뒤 안정적인 실패 상태로 종료한다.

## 범위

- generated NGINX response에 현재 deployment ID marker 추가
- gateway marker, route 응답과 exact Docker label 기반 activation recovery
- recovery 결과 `ACTIVE`, `SAFE_TO_RETRY`, `UNCERTAIN` 구분
- unsafe recovery failure에서 candidate cleanup 생략
- expired claim을 포함한 attempt 상한 적용
- NGINX 전환 crash window와 반복 claim regression test
- 현재 architecture·README의 복구 계약 동기화

## 비범위

- 새 deployment 상태나 공개 API endpoint
- 관리자 cancel·강제 reconcile UI
- 일반 Docker orphan retention과 project 삭제
- application runtime 장애 자동 healing
- multi-host recovery와 분산 transaction

## 복구 판정

| DB active | 실제 gateway marker | target Docker | 처리 |
|---|---|---|---|
| target | target 또는 이미 확정됨 | 기존 metadata 사용 | 남은 job을 `SUCCEEDED`로 완료 |
| previous/없음 | target | exact label resource가 정상 | DB active를 target으로 맞추고 `SUCCEEDED` |
| previous/없음 | previous/legacy | 관계없음 | target 설정을 last-known-good로 복구한 뒤 안전하게 재시도 |
| previous/없음 | target | resource 누락·비정상, previous 복구 확인 | previous로 복구한 뒤 안전하게 재시도 |
| previous/없음 | 불명·관찰 실패 | 누락·충돌·관찰 실패 | candidate를 삭제하지 않고 `RECOVERY_STATE_UNCERTAIN` |

같은 commit은 여러 deployment에서 사용할 수 있으므로 commit SHA가 아니라 Heimdall이 생성한
deployment ID를 generation identity로 사용한다.

## 데이터·외부 효과와 실패 영향

- DB schema와 공개 response model은 변경하지 않는다.
- NGINX config에 `X-Heimdall-Deployment-Id` response header를 추가한다. raw secret이나
  host path는 포함하지 않는다.
- recovery는 Docker 이름만 믿지 않고 `heimdall.managed`와 deployment ID label을 다시
  검사한다.
- gateway generation이 불확실하면 cleanup보다 보존을 선택한다. 해당 deployment는 안정적인
  recovery failure로 종료될 수 있으며 보존된 resource는 후속 운영 retention 대상이다.
- target이 실제 active일 때의 DB reconcile은 기존 runtime repository transaction을 사용한다.

## 선택한 방향과 감수한 단점

- NGINX와 PostgreSQL을 하나의 transaction으로 묶지 않고 관찰 후 수렴시킨다.
- marker는 별도 내부 path 대신 response header를 사용해 사용자 route를 예약하지 않는다.
- 불확실한 상태를 자동 삭제하지 않으므로 resource가 남을 수 있지만 정상 preview 삭제보다
  안전하다.
- 독립 heartbeat thread는 추가하지 않는다. Docker command는 기존 heartbeat runner를
  사용하고, 이번 범위는 process crash 뒤의 정확한 recovery와 bounded attempt에 집중한다.

## 수직 단계와 검증

1. Worker recovery 계약
   - recovery 결과와 cleanup 정책 추가
   - active recovery, uncertain 보존, max attempt regression unit test
2. 실제 generation 관찰
   - NGINX deployment marker와 HTTP observation
   - Docker candidate exact-label observation
   - target finalize, previous restore, unknown preserve unit test
3. 통합 검증과 문서
   - PostgreSQL expired claim·attempt 동작 검증
   - backend Ruff·pytest 집계 gate
   - frontend verify와 Compose config 정적 검증
   - 실제 Docker crash smoke는 기존 release smoke 환경에서 opt-in으로 실행

## 인수 조건

- NGINX가 target deployment를 서비스하고 DB만 이전 상태면 candidate를 삭제하지 않고 target을
  active로 확정한다.
- NGINX가 이전 deployment를 서비스하면 target current config를 last-known-good로 복구한 뒤에만
  candidate 재생성을 허용한다.
- gateway 또는 Docker 상태가 불확실하면 target candidate cleanup 명령을 실행하지 않는다.
- 반복 lease expiry가 `worker_max_attempts`를 넘어도 같은 deployment를 다시 실행하지 않는다.
- 복구된 old Worker token은 기존 fencing 계약대로 DB를 변경하지 못한다.
- 정상 배포, route probe rollback, secret·Managed DB 주입 계약이 회귀하지 않는다.

## 안전한 중단과 rollback

- Worker 계약 단계만 적용한 상태에서는 recovery observer가 없는 production processor를 활성화하지
  않는다.
- marker 관찰이 불가능하면 자동 cleanup하지 않고 `UNCERTAIN`으로 종료한다.
- 코드 rollback 시 DB migration이 없으므로 이전 binary로 되돌릴 수 있다. 새 header가 포함된
  NGINX config는 이전 binary에서도 유효하다.

## 문서 영향

- `architecture.md`: deployment ID marker와 recovery 판정 흐름
- `project-profile.md`: 실제 gateway·Docker 관찰 기반 recovery 규칙
- `README.md`: Worker 재시작 복구 설명

## 구현 결과

- [x] Worker recovery 계약과 bounded attempt
- [x] NGINX·Docker 실제 generation 관찰과 reconcile
- [x] 관련 unit·integration 검증
- [x] 집계 gate와 문서 동기화

검증 결과:

- Backend 집계: Ruff format·lint 통과, pytest `60 passed, 6 skipped`
- 실제 Docker·PostgreSQL opt-in integration: Docker Engine `29.6.1`에서 `6 passed`
- 실제 단일 service gateway 응답 header와 DB metadata rewind 후 reconcile 확인
- Frontend 집계: Vitest 4 files·6 tests와 production build 통과
- Compose config 정적 검증 통과
- 격리 smoke project의 container·network·volume 제거 확인, 기존 Heimdall resource 보존

남은 운영상 위험:

- NGINX와 Control DB 사이에 분산 transaction은 없으며 marker·label 관찰로 수렴한다.
- 실제 smoke는 NGINX 전환 완료 상태에서 DB metadata를 되감아 동일한 crash 결과 상태를
  재현했다. OS process를 특정 instruction에서 강제 종료하는 fault injection은 추가하지 않았다.
- 상태가 끝내 불확실하면 deployment는 attempt 상한 뒤 실패하지만 Docker resource는 보존된다.
  일반 orphan retention과 관리자 강제 reconcile은 후속 범위다.
