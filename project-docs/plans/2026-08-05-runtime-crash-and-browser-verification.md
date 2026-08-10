# Runtime 강제 종료·브라우저 검증 Plan

- 상태: `APPROVED`
- 날짜: `2026-08-05`
- 승인 근거: 사용자가 로컬 Heimdall의 배포·복구 검증은 유지하고 GitHub Actions 범위는 취소함

## 현재 동작과 문제

NGINX가 target으로 전환된 뒤 Control DB active metadata 기록 전에 Worker가 종료된 상태는 실제
Docker state를 만든 뒤 DB metadata를 되감는 방식으로 검증했다. 상태 동등성은 확인했지만 실제
Worker process가 그 지점에서 `SIGKILL`되고 lease 만료 후 다른 Worker가 회수하는 전체 경로는
자동화하지 않았다.

관리자 runtime reconciliation UI는 Testing Library component test만 있고 실제 Chromium의
navigation과 network mutation 검증이 없었다.

## 목표와 범위

- 실제 Worker process를 NGINX switch 직후 강제 종료하고 새 Worker가 target을 보존·성공 처리한다.
- 관리자 runtime reconciliation UI의 조회·safe/force request를 실제 Chromium에서 검증한다.
- 모든 검증은 로컬에서 명시적으로 실행한다.
- crash-window 전용 deterministic runtime fixture와 실제 `SIGKILL` integration test를 추가한다.
- Playwright Chromium E2E와 Vite test server를 추가한다.

## 비범위

- GitHub Actions, CI/CD와 원격 자동 검사 파이프라인
- GitHub webhook과 push 기반 자동 배포
- Heimdall의 기존 버튼 배포 흐름 변경
- production runtime의 fault injection hook
- Safari·Firefox 전체 matrix와 visual snapshot 기준선

## 실제 crash 재현 방식

- test fixture service는 `/hold` 첫 요청만 일정 시간 block하고 이후 요청은 정상 응답한다.
- Worker는 NGINX reload 뒤 route probe에서 `/hold`에 block된다.
- 부모 test process는 stable preview의 `/`에서 target Deployment ID header를 관찰한 뒤 child Worker에
  `SIGKILL`을 보낸다.
- Control DB가 target을 아직 active로 기록하지 않았음을 확인한다.
- lease 만료 뒤 새 Worker가 같은 deployment를 claim하고 NGINX marker·Docker label·health를
  확인해 DB와 terminal status를 `SUCCEEDED`로 수렴시킨다.
- production 코드에는 sleep, signal 또는 test callback을 추가하지 않는다.

## 외부 효과와 안전한 중단

- Docker crash test는 `HEIMDALL_RUN_DOCKER_SMOKE=true`일 때만 실행한다.
- test가 만든 gateway와 candidate container·network·image는 `finally`에서 정확한 이름과 label로
  정리한다.
- Playwright는 loopback Vite server와 mock API만 사용한다.
- fault test와 Playwright를 제거해도 production bundle과 버튼 배포 동작은 바뀌지 않는다.

## 인수 조건

- child Worker가 종료될 때 NGINX는 target ID를 응답하고 DB active는 아직 target이 아니다.
- 새 Worker는 target container·network·image를 재생성하거나 삭제하지 않고 deployment를 성공시킨다.
- Playwright가 retained 상태, 즉시 safe reconcile과 full-ID force cleanup payload를 검증한다.
- backend Ruff·pytest와 frontend verify가 통과한다.
- opt-in 로컬 PostgreSQL·Docker integration smoke가 통과한다.
- GitHub Actions workflow와 관련 문서가 남지 않는다.

## 문서 영향

- `README.md`: Playwright와 로컬 release smoke 명령
- `project-profile.md`: Playwright를 포함한 로컬 검증 도구
- 제품 공개 계약과 기존 버튼 배포 흐름은 변경하지 않는다.

## 구현 결과

- [x] 실제 Worker `SIGKILL` crash-window recovery smoke
- [x] Playwright Chromium reconciliation E2E
- [x] GitHub Actions와 관련 문서 제거
- [x] 전체 로컬 검증과 문서 동기화
