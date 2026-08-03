# Heimdall 작업 흐름

## 라우팅

```text
요구사항 확인
-> 관련 코드와 현재 동작 조사
-> 위험도 판단
-> 일반 변경: 바로 작은 수직 구현
-> 고위험 변경: $plan-change와 사용자 승인
-> 관련 검증
-> $verify-change
-> 결과와 남은 위험 보고
```

## 고위험 조건

다음 변경은 `$plan-change`를 사용한다.

- DB schema, transaction, 데이터 소유권
- 인증, 권한, secret
- Git, PostgreSQL, Docker, NGINX 운영 계약
- 배포 상태, job queue, lease, fencing, retry, recovery
- 공개 API 호환성
- feature 경계와 주요 아키텍처
- 데이터 삭제와 넓은 runtime cleanup

파일이 많거나 작업이 오래 걸린다는 이유만으로 고위험으로 올리지 않는다.

## 구현 규율

- 한 단계에서 사용자에게 보이는 수직 흐름 하나를 완성한다.
- Router는 transport 변환만 한다.
- repository는 자기 feature의 저장 상태만 다룬다.
- 긴 Git·Docker·filesystem 작업 중 DB transaction을 유지하지 않는다.
- Python module이 여러 변경 이유를 가지면 책임 단위로 나눈다.
- 모든 계층에 동일한 파일 세트를 강제하지 않는다.

## 문서

- `project-docs`는 현재 제품·아키텍처·공개 계약만 설명한다.
- ADR은 중요한 선택과 감수한 단점이 장기간 유효할 때만 만든다.
- Plan은 고위험 작업에만 만든다.
- Git과 PR이 일반 변경 이력을 소유한다.

## 검증

- 구현 중에는 관련 테스트와 Lint만 빠르게 실행한다.
- 단계 완료에는 backend 또는 frontend 집계 gate를 한 번 실행한다.
- 실제 Docker·network·cleanup smoke는 승인된 release gate에서만 실행한다.
- 완료 전 `$verify-change`로 diff, 검증, 문서 영향을 대조한다.
