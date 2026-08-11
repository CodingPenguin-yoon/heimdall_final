# 초기 제품 범위

## 첫 사용자 흐름

```text
Public GitHub 저장소 등록
-> main 검증
-> service와 route 설정
-> 환경변수와 PostgreSQL 접근 service 설정
-> managed project database 생성
-> 최근 commit 확인
-> 최신 또는 특정 commit 배포 요청
-> 상태와 로그 관찰
-> 안정 preview 접근
```

## 포함

- Public HTTPS GitHub repository
- 고정 `main` branch
- multi-service Dockerfile build
- service 내부 Docker DNS 통신
- path 기반 project gateway route
- service별 plain·secret 환경변수
- 별도 Managed PostgreSQL cluster와 project별 database·role
- DB 접근 service 전용 `DATABASE_*` 계약과 password file
- 수동 배포
- exact commit source rebuild
- 배포 상태, 이력, 실패 단계, 로그
- 구조화 deployment event snapshot·SSE와 loopback stable preview link
- 불확실 runtime 보존, 자동 안전 재확인과 관리자 confirmed cleanup

## 초기 비범위

- Private Git과 SSH key
- GitLab과 provider API
- webhook과 자동 배포
- Compose file 직접 실행
- arbitrary branch/tag/SHA
- image registry와 image rollback
- password rotation과 사람용 단기 DB credential
- project database backup·restore·purge 자동화
- volume/data rollback
- 다중 사용자와 역할
- public domain, TLS, multi-host
- application stdout/stderr 무제한 수집·장기 저장·검색·다운로드
