---
name: verify-change
description: Heimdall 코드나 설정 변경을 완료했다고 보고하기 전에 요구사항, diff, 아키텍처 경계, 보안·정합성, 테스트·Lint·타입·빌드 결과와 project-docs 동기화를 확인한다. 기능 구현, 버그 수정, 리팩터링, 고위험 Plan 단계의 완료 시 사용한다.
---

# Verify Change

실행한 증거를 기준으로 완료 여부를 판정한다.

## 대조

1. 사용자 요구사항과 관련 Plan의 인수 조건을 확인한다.
2. 최종 diff에서 범위 밖 변경, 임시 코드, 거대 책임 집중을 확인한다.
3. Router의 DB·Git·Docker 직접 접근과 feature 간 repository 침범을 확인한다.
4. project별 단일 배포, exact SHA, snapshot, candidate 보존 규칙의 회귀를 확인한다.
5. raw secret, shell 문자열 실행, 경로 탈출, 과도한 network 노출을 확인한다.

LOC 숫자만으로 파일 분리를 요구하지 않는다. 서로 다른 변경 이유가 섞이거나 독립 테스트가 어려운 근거가 있을 때만 책임 분리를 요구한다.

## 검증

변경 영역에 맞는 빠른 검사를 먼저 실행하고 완료 시 집계 gate를 한 번 실행한다.

- Backend: `pytest`, `ruff check`, 필요 시 `ruff format --check`
- Frontend: `pnpm verify`
- 외부 adapter: 승인된 Plan이 요구할 때만 실제 Git·PostgreSQL·Docker smoke

실행하지 않은 검사를 성공으로 표현하지 않는다. 실패를 숨기기 위해 규칙이나 테스트를 약화하지 않는다.

## 문서 영향

다음이 실제로 바뀐 경우에만 기존 `project-docs`를 갱신한다.

- 제품 범위와 사용자 동작
- 기술 스택과 실행 명령
- 아키텍처, 데이터 소유권, 상태 전이
- 공개 API 또는 DB 계약
- 주요 성공·실패·복구 흐름

일반 변경 이력 문서를 새로 만들지 않는다.

## 결과

- 구현 결과
- 실행한 검증과 결과
- 갱신한 현재 문서
- 미실행 검사와 남은 위험
