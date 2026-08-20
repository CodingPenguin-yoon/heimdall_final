# Runtime 보존·관리자 복구 Plan

- 상태: `APPROVED`
- 구현 결과: `COMPLETE`
- 날짜: `2026-08-05`
- 승인 근거: 사용자가 불확실한 Docker 자원 정리와 관리자 복구 기능을 다음 작업 세트로 승인함

## 현재 동작과 문제

Worker recovery가 실제 gateway generation을 확정하지 못하면 deployment는
`FAILED/RECOVERY/RECOVERY_STATE_UNCERTAIN`으로 종료하고 target Docker resource를 보존한다.
정상 preview를 실수로 삭제하지 않는 안전한 기본값이지만, 이후 상태가 정상화돼도 자동 재확인이나
정리 작업이 없으며 관리 UI에서도 운영자가 조치할 수 없다.

Docker socket은 Worker만 소유한다. API에서 직접 Docker 명령을 실행하는 관리자 endpoint는 현재
권한 경계와 긴 외부 작업의 durable 실행 계약을 깨뜨린다.

## 목표

- 불확실한 target resource를 설정된 보존 기간 동안 그대로 둔다.
- 보존 기간 뒤 Worker가 실제 DB·NGINX·Docker 상태를 다시 확인한다.
- target이 실제 active면 runtime metadata와 deployment 상태를 성공으로 수렴시킨다.
- 이전 generation이 안전하게 서비스 중이면 exact-label target resource만 정리한다.
- 여전히 불확실하면 자동 삭제하지 않고 `BLOCKED`로 남긴다.
- 관리자가 UI에서 즉시 안전 재확인 또는 명시적 강제 정리를 요청할 수 있다.

## 범위

- durable runtime reconciliation job과 lease·claim token fencing
- 기본 72시간의 configurable retention
- Worker의 automatic·admin reconciliation 처리
- 관리자 reconciliation 조회·요청 API
- Deployment ID 확인을 요구하는 force cleanup
- 불확실 deployment용 관리 UI
- unit·PostgreSQL integration·실제 Docker cleanup smoke

## 비범위

- project 전체 삭제와 Managed PostgreSQL·secret purge
- 일반 Docker host 전체 garbage collection
- active deployment의 수동 rollback
- 다중 사용자 RBAC와 별도 인증 체계
- public host 운영과 다중 Worker scheduler

## 상태와 사용자 동작

```text
FAILED/RECOVERY_STATE_UNCERTAIN
-> RETAINED: 보존 기간 대기
-> PENDING: 자동 또는 관리자 요청이 durable job으로 등록됨
-> RUNNING: Worker lease 소유
-> RESOLVED/ACTIVE: target 실제 active 확인, deployment SUCCEEDED로 수렴
-> RESOLVED/CLEANED: previous가 active이거나 force 확인 후 target resource 정리
-> BLOCKED/UNCERTAIN: 관찰 불가, resource 보존
```

- `RECONCILE`은 안전 판정만 수행한다. 불확실하면 삭제하지 않는다.
- `FORCE_CLEANUP`은 요청 payload의 confirmation이 deployment ID와 정확히 같아야 한다.
- force도 DB가 target을 active로 기록한 경우에는 거부한다.
- 자동 retention은 절대 force mode를 사용하지 않는다.

## 데이터·보안·외부 효과

- Control DB에 deployment별 `runtime_reconciliations` job을 추가한다.
- job은 action, requester, state, result, attempts, available time, lease owner·expiry와 claim token을
  저장한다. raw Docker 출력과 secret은 저장하지 않는다.
- API는 DB에 요청만 기록하고 Docker socket을 받지 않는다.
- Worker는 deployment job을 우선 처리하고 idle cycle에서 reconciliation job을 처리한다.
- Docker 삭제 전후에 deterministic name과 `heimdall.managed`, project ID, deployment ID label을
  검사한다. 이름 충돌·Docker 관찰 실패는 정리 성공으로 간주하지 않는다.
- 긴 Docker·NGINX 작업 중 Control DB transaction을 유지하지 않고 heartbeat로 lease를 갱신한다.

## 선택한 방향과 감수한 단점

- 별도 durable job을 사용해 API request와 외부 효과를 분리한다. 즉시 실행보다 느리지만 Worker
  crash 뒤 회수와 fencing이 가능하다.
- 보존 기간이 지나도 안전 판정이 안 되면 resource가 계속 남는다. 가용성 보호를 저장 공간보다
  우선한다.
- 현재 제품은 신뢰된 단일 관리자 범위다. force cleanup은 추가 RBAC 대신 exact deployment ID
  confirmation을 요구한다. public 노출 전에 인증 작업이 별도로 필요하다.
- `ACTIVE` reconcile은 실제 target health·route 검증이 끝난 경우에만 FAILED deployment를
  SUCCEEDED로 교정한다.

## 수직 단계와 검증

1. Durable reconciliation 계약
   - migration, model, repository claim·renew·retry·complete·block
   - retention discovery, expired lease recovery와 old token fencing integration
2. Worker와 exact cleanup
   - safe reconcile, active adoption, blocked preservation, confirmed force cleanup
   - exact label conflict·Docker observation failure regression test
3. API와 UI
   - 조회, safe reconcile 요청, exact ID force confirmation
   - RETAINED/PENDING/RUNNING/RESOLVED/BLOCKED 표시와 polling
4. release gate
   - backend·frontend aggregate, Compose config
   - 격리 Docker smoke에서 inactive candidate exact cleanup과 기존 active 보존

## 인수 조건

- 보존 기간 전에는 자동 Docker mutation이 없다.
- automatic·safe reconcile이 `UNCERTAIN`이면 candidate cleanup 명령이 없다.
- previous generation이 실제 응답함을 확인한 경우에만 automatic candidate cleanup을 수행한다.
- target generation이 정상 서비스 중이면 resource를 삭제하지 않고 deployment를 성공으로 수렴한다.
- force cleanup은 exact deployment ID confirmation 없이는 요청할 수 없다.
- DB active target, unmanaged name collision과 Docker 관찰 실패는 force cleanup에서도 보존된다.
- reconciliation Worker crash는 lease 만료 뒤 회수되고 최대 attempt 뒤 `BLOCKED`가 된다.
- API process에 Docker adapter나 socket 의존성이 추가되지 않는다.

## 안전한 중단과 rollback

- migration과 repository까지만 적용된 상태에서는 job을 claim하는 Worker가 없으므로 Docker 변화가 없다.
- API·UI까지만 적용돼 요청이 쌓여도 Worker 단계 전에는 외부 효과가 없다.
- 코드 rollback 시 새 table은 기존 binary가 읽지 않으므로 runtime 동작을 방해하지 않는다.
- cleanup 도중 crash가 나면 exact resource 관찰을 다시 수행해 idempotent하게 수렴한다.

## 문서 영향

- `architecture.md`: reconciliation queue, retention과 force guard
- `project-profile.md`: 보존 resource 운영 계약
- `README.md`, `.env.example`: retention 설정과 관리자 동작

## 구현 결과

- [x] Durable reconciliation job과 retention discovery
- [x] Worker safe·force reconciliation과 exact cleanup
- [x] 관리자 API·UI
- [x] unit·integration·release smoke와 문서 동기화

검증 결과:

- Backend 집계: Ruff format·lint 통과, pytest `72 passed, 7 skipped`
- 실제 PostgreSQL·Docker opt-in integration: `7 passed`
- retention 전 미청구, automatic job 생성, expired lease 회수와 old token fencing 확인
- inactive exact-label candidate의 container·network·image 제거와 기존 active preview 보존 확인
- Frontend 집계: Vitest 5 files·8 tests, TypeScript와 production build 통과
- Compose config와 diff whitespace 검증 통과
- 격리 smoke container·network·volume 제거, 기존 Heimdall gateway 보존 확인

남은 운영상 위험:

- 현재 제품은 신뢰된 단일 관리자 범위라 별도 인증·RBAC가 없다. public API 노출 전 인증이
  필요하다.
- force cleanup은 DB active generation을 거부하고 exact label만 삭제하지만, DB 자체가 실제
  gateway와 어긋난 상태에서 관리자가 명시적으로 force를 선택하면 preview 중단 가능성을
  감수한다.
- automatic reconcile이 계속 불확실하면 `BLOCKED`로 보존되며 관리자 재확인 또는 force 결정이
  필요하다.
- API와 Worker는 같은 `HEIMDALL_RUNTIME_RETENTION_HOURS` 설정을 사용해야 UI 예정 시각과 실제
  scheduler가 일치한다.
