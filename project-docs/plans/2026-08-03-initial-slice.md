# 초기 수직 구현 Plan

- 상태: `APPROVED`
- 날짜: `2026-08-03`
- 승인 근거: 사용자와 Public GitHub, `main`, DRAFT/READY 설정, multi-service, commit 재배포 방향을 순차 합의함

## 목표

새 Python 구조에서 저장소 등록, 프로젝트 설정, commit 선택, 배포 요청까지 하나의 실제 흐름으로 구현하고 화이트톤 React UI에서 사용할 수 있게 한다.

## 공개 계약

- `POST /api/projects`: name과 Public GitHub URL로 DRAFT 생성
- `PUT /api/projects/{id}/settings`: expectedVersion과 service·route aggregate 저장
- `GET /api/projects/{id}/commits`: `main` 최근 commit 최대 20개
- `POST /api/projects/{id}/deployments`: `MAIN_HEAD` 또는 최근 `MAIN_COMMIT` 요청
- project 설정은 JSONB aggregate, deployment는 immutable JSONB snapshot을 소유함
- deployment와 durable job 생성은 하나의 PostgreSQL transaction으로 처리함

## 안전한 중단 지점

초기 단계는 Docker mutation을 수행하지 않는다. API와 UI가 배포 요청을 `QUEUED` job으로 안전하게 저장하는 단계에서 종료하며, 실제 runtime은 별도 Plan 승인 후 연결한다.

## 단계

- [x] 제품 범위와 아키텍처 기준선
- [x] Backend scaffold와 공통 오류 계약
- [x] Public Git URL/main 검증 adapter
- [x] Project DRAFT/READY와 JSONB 설정
- [x] 최근 commit과 deployment request snapshot
- [x] React project list/register/settings/overview
- [x] Backend와 frontend 집계 검증

## 후속 Plan

Docker candidate, 프로젝트별 NGINX activation, Worker lease/recovery, durable SSE log는 현재 공개 상태 모델을 유지하면서 별도 고위험 Plan으로 구현한다.
