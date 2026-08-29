# 로컬 HTTP 관리자 로그인

- 상태: `APPROVED`
- 승인일: `2026-08-25`

## 현재 동작과 문제

관리자 로그인 API는 올바른 자격 증명에 `200`을 반환하지만, 세션 쿠키가 항상
`Secure`인 `__Host-heimdall-session`이어서 `http://localhost:5173`에서 브라우저가 쿠키를
저장하거나 전송하지 않는다. 그 결과 로그인 직후 세션 확인과 보호 API가 `401`을 반환한다.
현재 기본 동작은 운영용 HTTPS 계약에는 맞지만 로컬 Docker Compose에서 HTTP 로그인을
검증할 명시적 개발 모드가 없다.

## 목표와 인수 조건

- `HEIMDALL_AUTH_COOKIE_SECURE=false`를 API에 명시한 경우에만 로컬 HTTP 로그인이 동작한다.
- 기본값과 `true` 설정은 기존 `Secure` `__Host-heimdall-session` 계약을 그대로 유지한다.
- HTTP 개발 모드는 `__Host-` 브라우저 제약을 위반하지 않는 별도 로컬 쿠키 이름을 사용한다.
- 두 모드 모두 `HttpOnly`, host-only, `SameSite=Strict`, `/`, 8시간 만료와 기존 CSRF 계약을
  유지한다.
- 잘못된 boolean 값은 API 시작 전에 거부한다.
- `false`는 `.localhost` 관리 hostname에서만 허용한다.
- 로컬 쿠키와 운영 Secure 쿠키는 서명 용도를 분리해 이름을 바꾼 교차 재사용을 거부한다.
- 이 설정은 Control Compose의 API에만 전달되며 비밀값은 아니다.
- 로컬 `.env`에 개발 옵션을 적용하고 실제 브라우저 또는 동등한 HTTP 세션 흐름에서 로그인,
  세션 조회, 보호 API, 로그아웃을 확인한다.

## 범위와 비범위

범위는 Backend 설정·세션 미들웨어, Control Compose 전달, 예제 환경 설정과 현재 인증 문서다.
TLS 발급·종료, 운영 HTTP 허용, Frontend 인증 상태 모델 변경, 비밀번호·세션 저장 방식 변경은
포함하지 않는다.

## 보안과 실패 영향

HTTP 개발 모드는 네트워크에서 쿠키 기밀성을 제공하지 않으므로 loopback 개발 환경에서만
사용한다. 기본값을 안전한 `true`로 두고 `.localhost` 관리 hostname의 명시적 `false`만 허용한다.
로컬 모드에는 `heimdall-local-session`을 사용해 `Secure`가 필수인 `__Host-` 접두사 규칙을
위반하지 않으며, 운영 쿠키와 다른 purpose-derived signing key를 사용해 교차 재사용을 막는다.

## 구현과 검증

1. 설정 기본값·파싱과 두 쿠키 모드에 대한 실패 테스트를 추가한다.
2. 설정을 세션 미들웨어에 연결하고 Control Compose API에만 전달한다.
3. `.env.example`, README와 현재 제품·아키텍처 문서를 동기화한다.
4. 관련 Backend 테스트, Ruff, 전체 Backend gate, Frontend gate와 Compose config를 실행한다.
5. API를 재빌드·재생성한 뒤 로컬 HTTP 로그인 전체 흐름을 검증한다.

안전한 중단점은 각 단계의 테스트가 통과한 커밋 전 상태다. 롤백은 옵션과 연결 코드를
제거하고 API를 기본 Secure 모드로 재생성하는 것이며 인증 비밀 파일이나 DB 데이터 변경은 없다.

## 검증 기록

- 실패 재현: 설정·HTTP 세션 회귀 테스트를 구현 전에 실행해 `auth_cookie_secure` 부재와
  HTTP 세션 미지원으로 7건 실패하는 것을 확인했다. 보안 리뷰 뒤 추가한 non-localhost 거부와
  로컬→Secure 쿠키 교차 재생 테스트도 보강 전 각각 실패하는 것을 확인했다.
- 변경 단위: 설정·startup 테스트 `29 passed`; 변경 Backend 파일 Ruff format/check 통과.
- Backend 집계: Ruff format `121 files already formatted`, Ruff check 통과, pytest
  `258 passed, 18 skipped`. 이전 전체 실행 중 기존 Unix 로그 브로커 테스트가 한 번 transient
  transport 오류로 실패했으나 해당 테스트 3회, 모듈 8건, 이후 전체 suite가 모두 통과했다.
- Frontend 집계: format, ESLint, TypeScript, build와 Vitest `15 files / 50 tests`, Chromium
  E2E `11 tests` 통과.
- Compose: 렌더링 결과 `HEIMDALL_AUTH_COOKIE_SECURE=false`는 API에만 있고 두 Worker와
  frontend에는 없음을 확인했다. 명시적 빈 값은 안전 기본값으로 치환하지 않고 API에 전달되어
  strict Backend parser가 시작 전에 거부한다.
- Runtime: API 이미지를 재빌드·재생성했고 컨테이너 설정이 `secure=False`, 쿠키 이름
  `heimdall-local-session`, 관리 hostname은 `heimdall.localhost`이며 healthy임을 확인했다.
  Frontend 경유 login page와 health는 `200`, 미인증 session과 잘못된 로그인은 각각 `401`이었다.
- HTTP 세션 회귀 테스트에서 올바른 admin 로그인, session 조회, 보호된 project API, CSRF logout,
  logout 뒤 `401`을 확인했다. non-localhost 설정 거부와 로컬 쿠키의 Secure 모드 교차 재생 `401`도
  확인했다. 실제 관리자 비밀번호를 사용하는 브라우저 smoke는 비밀번호를 도구에 노출하지 않기
  위해 운영자가 같은 `localhost` hostname에서 최종 확인한다.
