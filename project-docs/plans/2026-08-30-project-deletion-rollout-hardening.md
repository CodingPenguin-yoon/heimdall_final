# Project deletion rollout hardening

- 상태: `APPROVED`
- 승인: 운영 수동 보정이 재발하지 않도록 코드 수정 후 테스트 데이터를 초기화하기로 사용자 승인

## 현재 문제

Project deletion 자체는 exact resource identity를 검증하지만 운영 rollout 준비에 두 공백이 있다.

1. 별도 Managed PostgreSQL bootstrap은 provisioner에 `CREATEDB`와 `CREATEROLE`만 부여하고,
   deletion preflight가 요구하는 `pg_signal_backend`의 `SET` privilege를 부여하지 않는다. init script는
   신규 volume에서만 실행되므로 기존 volume도 자동 보정되지 않는다.
2. Ubuntu bind mount의 runtime/Git root가 Worker UID 소유가 아니면 secret operation lock을 만들 수 없다.
   현재 deletion Worker는 unsafe lock 오류까지 정상적인 busy 상태로 처리해 `WAITING_FOR_OPERATIONS`로
   계속 미룬다.

## 목표와 범위

- Managed DB bootstrap/reconcile을 재실행 가능하게 만들고 신규·기존 volume 모두 deletion privilege를
  갖게 한다.
- Worker 시작 시 설정된 runtime root, Git workspace root와 필요한 lock/gateway root만 현재 Worker
  UID/GID의 owner-only directory로 준비한다. 하위 project tree를 재귀 변경하지 않는다.
- 실제 lock contention만 대기로 처리하고 unsafe filesystem 상태는 명시적인 failed deletion으로 남긴다.
- 레거시 Docker label 호환, deletion ordering, API와 DB schema는 변경하지 않는다.

## 구현 단계와 검증

1. Managed DB bootstrap을 실행해 PostgreSQL에 전달되는 SQL과 Compose의 기존-volume reconcile 경로를
   검증하는 실패 테스트를 추가한다.
2. idempotent role grant와 one-shot reconcile service를 최소 구현하고 실제 disposable PostgreSQL에서
   신규·기존 volume 권한을 확인한다.
3. Worker root 준비와 unsafe secret lock 실패를 재현하는 테스트를 먼저 실패시킨다.
4. exact non-recursive directory preparation과 busy/unsafe 분리를 최소 구현한다.
5. 관련 pytest, backend 전체 pytest/Ruff/format, 두 Compose config와 Managed DB smoke를 실행한다.
6. `$verify-change`로 두 저장소 diff와 사용자 소유 문서 변경 미포함을 대조한다.

## 안전과 rollback

- reconcile 실패 시 Managed DB container data는 유지되고 Worker preflight가 fail-fast한다.
- Worker root가 symlink이거나 canonical exact directory로 준비할 수 없으면 Worker는 DB/Docker mutation 전에
  시작을 중단한다.
- directory 준비는 설정된 root와 고정된 `.locks/projects`, `gateways`만 대상으로 하며 재귀 `chown`을
  사용하지 않는다.
- 변경 rollback은 Managed DB reconcile service와 Worker startup preparation을 제거하는 것으로 가능하며
  application data schema는 바뀌지 않는다.

## 검증 기록

- Managed DB tests는 bootstrap SQL에 제한된 `pg_signal_backend` membership이 전달되고 Compose가
  healthy PostgreSQL 뒤 one-shot reconcile service를 실행하는 것을 RED에서 확인한 뒤 GREEN으로
  전환했다.
- disposable PostgreSQL 18.4 volume에서 신규 bootstrap 후 `SET=true`, `INHERIT=false`, `ADMIN=false`를
  확인했다. 같은 기존 volume에서 membership을 revoke해 `SET=false`를 확인한 뒤 일반
  `docker compose up -d --wait` 재실행만으로 `SET=true`가 복구됐다. one-shot은 exit 0이었고 test
  container, network, volume은 제거했다.
- Worker tests는 unsafe secret lock이 기존 구현에서 defer되는 것과 private root가 DB open 전에
  준비되지 않는 것을 RED로 확인했다. 최소 구현 후 busy lock은 defer, unsafe lock은
  `PROJECT_SECRET_CLEANUP_FAILED`, exact root는 현재 UID/GID와 `0700`, 기존 descendant는 비재귀 보존으로
  GREEN이 됐다.
- backend fresh gate는 `324 passed, 23 skipped`, Ruff check와 format check를 통과했다. 첫 전체
  권한 확장 실행에서 기존 Unix broker race가 한 차례 발생했으나 단독 재현되지 않았고 fresh 전체
  rerun은 통과했다.
- frontend `pnpm verify`는 format/lint, 20 files·68 tests, typecheck와 production build를 통과했다.
  dev base, Linux overlay, Edge, Managed DB Compose config와 두 저장소 `git diff --check`도 통과했다.
