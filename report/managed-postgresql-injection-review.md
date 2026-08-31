# Heimdall Managed PostgreSQL 주입 방식 검토 보고서

- 작성일: 2026-08-31
- 대상: `CodingPenguin-yoon/heimdall_final`
- 재현 프로젝트: `CodingPenguin-yoon/heimdall-test-chat`
- 재현 프로젝트 기준 커밋: `85e3be6`

## 1. 요약

현재 Heimdall의 Managed PostgreSQL 주입 방식은 보안 의도는 좋지만, 일반적인 프로젝트를 수정 없이 배포하기에는 마찰이 큰 편이다.

Heimdall은 DB 비밀번호 원문을 환경변수에 넣지 않고 읽기 전용 파일로 전달한다. 이 방식은 비밀번호가 Docker 환경변수, 배포 snapshot, API 응답 또는 로그에 노출될 가능성을 줄인다.

하지만 현재 구현에는 다음 두 가지 호환성 문제가 있다.

1. 애플리케이션이 `DATABASE_URL` 대신 Heimdall 전용 `DATABASE_*` 변수 여섯 개를 조합해야 한다.
2. 비밀번호 파일이 root 소유 `0400`으로 전달되어 `USER node`, `USER app` 같은 non-root 이미지에서 읽을 수 없다.

실시간 채팅 프로젝트는 이 두 문제로 연속해서 기동에 실패했다.

- 1차 오류: `ECONNREFUSED 127.0.0.1:5432`
- 2차 오류: `EACCES /run/secrets/heimdall/project-database-password`

결론적으로 다음 방향을 권고한다.

> 현재의 file-secret 보안 원칙은 유지하되, Heimdall이 서비스 실행 UID/GID에 맞는 전용 Secret 파일을 만들어 제공해야 한다. 애플리케이션이 비밀번호를 읽기 위해 root로 시작하는 구조는 플랫폼 수준에서 제거하는 것이 바람직하다.

## 2. 현재 주입 계약

서비스 설정에서 `projectDatabaseAccess=true`를 선택하면 해당 서비스에만 Managed PostgreSQL 연결 정보가 주입된다.

주입되는 값은 다음과 같다.

| 환경변수 | 내용 | 민감 정보 여부 |
| --- | --- | --- |
| `DATABASE_HOST` | Managed PostgreSQL endpoint | 아니요 |
| `DATABASE_PORT` | PostgreSQL port | 아니요 |
| `DATABASE_NAME` | 프로젝트 전용 database | 아니요 |
| `DATABASE_USER` | 프로젝트 전용 role | 식별 정보 |
| `DATABASE_SCHEMA` | 프로젝트 전용 schema | 아니요 |
| `DATABASE_PASSWORD_FILE` | 비밀번호 파일 경로 | 경로만 전달 |

비밀번호 원문은 `DATABASE_PASSWORD_FILE`이 가리키는 파일에 저장된다.

기본 경로는 다음과 같다.

```text
/run/secrets/heimdall/project-database-password
```

관련 구현:

- [`projects/schemas.py`](../backend/src/heimdall/projects/schemas.py)
- [`project_database/service.py`](../backend/src/heimdall/project_database/service.py)
- [`runtime/docker.py`](../backend/src/heimdall/runtime/docker.py)
- [`secrets/store.py`](../backend/src/heimdall/secrets/store.py)

### 2.1 서비스별 opt-in

`projectDatabaseAccess`의 기본값은 `false`다. DB 연결을 활성화한 서비스만 연결 정보와 비밀번호 파일을 받는다.

이 경계는 frontend처럼 DB 접근이 불필요한 서비스에 credential을 전달하지 않는다는 점에서 적절하다.

### 2.2 예약 환경변수

사용자가 프로젝트 설정에서 직접 추가하는 환경변수는 `DATABASE_*`와 `HEIMDALL_*` 이름을 사용할 수 없다.

따라서 사용자가 UI에서 별도의 `DATABASE_URL`을 직접 만들어 현재 계약을 우회할 수 없다.

### 2.3 Compose 파일은 실행되지 않음

Heimdall은 저장소의 `docker-compose.yml`을 직접 실행하지 않는다. 등록된 서비스를 각 Dockerfile 기준으로 개별 build하고 Heimdall이 generation network와 container를 생성한다.

따라서 로컬 Compose에 다음 설정이 있어도 Heimdall 배포에는 적용되지 않는다.

```yaml
services:
  backend:
    environment:
      DATABASE_URL: postgres://chat:chat@db:5432/chat

  db:
    image: postgres:17-alpine
```

Compose 직접 실행이 비범위라는 사실은 [`project-docs/product-scope.md`](../project-docs/product-scope.md)에 명시되어 있다.

## 3. 장애 재현

### 3.1 1차 장애: localhost 연결 시도

최초 채팅 백엔드는 `DATABASE_URL`만 사용했다. 해당 값이 없으면 다음 주소를 기본값으로 사용했다.

```text
postgres://chat:chat@127.0.0.1:5432/chat
```

Heimdall은 `DATABASE_URL`을 주입하지 않기 때문에 백엔드는 자기 컨테이너의 loopback 주소로 접속했다.

관찰된 오류:

```text
Error: connect ECONNREFUSED 127.0.0.1:5432
code: 'ECONNREFUSED'
address: '127.0.0.1'
port: 5432
```

직접 원인은 다음과 같다.

1. 애플리케이션은 `DATABASE_URL`만 읽었다.
2. Heimdall은 여섯 개의 분리된 `DATABASE_*` 값만 주입했다.
3. 저장소의 Compose 설정은 Heimdall 배포에서 사용되지 않았다.
4. 애플리케이션이 localhost fallback을 사용했다.

이를 해결하기 위해 채팅 프로젝트에 [`database-config.js`](../../heimdall-test-chat/backend/src/database-config.js)를 추가했다.

이 모듈은 다음 두 실행 환경을 모두 지원한다.

- 로컬 Docker Compose: `DATABASE_URL`
- Heimdall: 분리된 `DATABASE_*` 값과 `DATABASE_PASSWORD_FILE`

### 3.2 2차 장애: 비밀번호 파일 접근 거부

분리된 환경변수를 지원한 뒤 애플리케이션은 실제 비밀번호 파일을 읽는 단계까지 진행했다.

그러나 다음 오류가 발생했다.

```text
Error: EACCES: permission denied,
open '/run/secrets/heimdall/project-database-password'
code: 'EACCES'
```

원인은 다음 두 설정의 충돌이다.

Heimdall Secret store:

```python
descriptor = os.open(temporary, flags, 0o400)
os.chmod(target, 0o400)
```

채팅 백엔드 이미지:

```dockerfile
USER node
```

Secret 파일은 컨테이너에서 `root:root 0400`으로 보였고, UID 1000인 `node` 프로세스는 파일을 읽을 수 없었다.

## 4. 채팅 프로젝트에 적용한 임시 대응

채팅 프로젝트는 현재 Heimdall 계약에서 안전하게 동작하기 위해 다음 순서를 사용한다.

1. 컨테이너가 root로 시작한다.
2. root 전용 DB 비밀번호 파일만 읽는다.
3. supplementary group을 제거한다.
4. GID를 `node`로 변경한다.
5. UID를 `node`로 변경한다.
6. Express, PostgreSQL driver와 애플리케이션 코드를 동적으로 import한다.
7. DB 연결과 HTTP listen을 시작한다.

권한 강등 순서:

```javascript
process.setgroups([]);
process.setgid("node");
process.setuid("node");
```

관련 파일:

- [`runtime-user.js`](../../heimdall-test-chat/backend/src/runtime-user.js)
- [`server.js`](../../heimdall-test-chat/backend/src/server.js)
- [`backend/Dockerfile`](../../heimdall-test-chat/backend/Dockerfile)

애플리케이션 시작 코드가 런타임 사용자에 의해 변조되지 않도록 `src`는 root 소유, node 읽기 전용으로 이미지에 복사했다.

```dockerfile
COPY --chown=root:root src ./src
```

이 대응은 해당 프로젝트를 복구하는 데에는 유효하지만, 모든 Heimdall 배포 프로젝트가 같은 bootstrap 코드를 작성하도록 요구해서는 안 된다.

## 5. 검증 결과

채팅 프로젝트 수정 후 다음 항목을 확인했다.

| 검증 항목 | 결과 |
| --- | --- |
| Backend 단위 테스트 | 11/11 통과 |
| Secret 소유권과 모드 | `root:root 0400` 확인 |
| 서버 PID 1 UID/GID | `1000:1000` 확인 |
| supplementary groups | 없음 확인 |
| node 사용자의 Secret 재접근 | `EACCES` 확인 |
| node 사용자의 source 수정 | `EACCES` 확인 |
| DB health | HTTP `200` 확인 |
| 실시간 SSE | 클라이언트 두 개에서 동일 메시지 수신 |
| DB 저장 | 전송 메시지 영속화 확인 |

수정 커밋:

```text
85e3be6 fix: allow reading Heimdall database secret
```

## 6. 일반 프로젝트 호환성 평가

### 6.1 장점

현재 방식의 장점은 명확하다.

- raw 비밀번호가 Docker 환경변수에 들어가지 않는다.
- 서비스별로 DB 접근을 opt-in할 수 있다.
- frontend 등 불필요한 서비스에는 credential이 전달되지 않는다.
- Secret reference와 fingerprint 기반으로 값의 무결성을 확인할 수 있다.
- 비밀번호 파일이 read-only로 mount된다.

### 6.2 단점

일반적인 프로젝트 입장에서의 마찰은 다음과 같다.

#### `DATABASE_URL` 전용 프로젝트

Node.js, Django, Rails 등에서는 `DATABASE_URL`을 직접 소비하는 구성이 흔하다. 이런 프로젝트는 Heimdall 전용 adapter를 추가해야 한다.

Heroku와 Render의 공식 문서도 조립된 PostgreSQL connection URL을 주요 연결 방식으로 제공한다.

- [Heroku Postgres 연결 문서](https://devcenter.heroku.com/articles/connecting-heroku-postgres)
- [Render Postgres 연결 문서](https://render.com/docs/postgresql-creating-connecting)

#### non-root Docker 이미지

보안을 위해 `USER node`, `USER app`, `USER 1000`을 사용하는 이미지는 현재 root 전용 Secret 파일을 읽지 못한다.

애플리케이션이 root로 시작해서 직접 권한을 낮추는 것은 구현 언어와 이미지마다 방법이 다르고, 잘못 구현하면 서버가 계속 root로 실행될 수 있다.

#### 로컬 환경과 Heimdall 환경의 차이

로컬에서는 Compose의 `db` 서비스와 `DATABASE_URL`을 사용하지만, Heimdall에서는 외부 Managed PostgreSQL과 여섯 개의 환경변수를 사용한다.

따라서 별도의 adapter와 배포 문서가 없으면 로컬에서 정상인 프로젝트가 Heimdall에서 실패하기 쉽다.

#### 오류 발견 시점

현재는 다음 문제를 컨테이너 시작 후 애플리케이션 로그에서 발견한다.

- DB 계약 누락
- localhost fallback
- Secret 파일 접근 권한 불일치

배포 전에 image user와 Secret 파일 권한을 검사하면 더 빠르고 명확한 오류를 제공할 수 있다.

### 6.3 종합 판단

| 평가 항목 | 판단 |
| --- | --- |
| 보안 의도 | 양호 |
| 기존 앱 무수정 배포 | 낮음 |
| non-root 이미지 호환 | 현재 실패 가능 |
| 로컬/배포 환경 일치 | 낮음 |
| 오류 진단성 | 보통 이하 |
| Heimdall 전용 프로젝트 | adapter 작성 시 사용 가능 |

현재 계약은 Heimdall을 고려해 새로 작성한 프로젝트에는 적용할 수 있다. 반면 기존 프로젝트를 수정 없이 가져오는 범용 배포 기능으로는 사용성이 부족하다.

## 7. 대안 비교

| 대안 | 보안 | 호환성 | 구현 난이도 | 판단 |
| --- | --- | --- | --- | --- |
| 현재 방식 유지와 문서 보강 | 애플리케이션 구현에 의존 | 낮음 | 낮음 | 근본 해결 아님 |
| raw `DATABASE_URL` 환경변수 주입 | 환경변수 노출 위험 | 매우 높음 | 낮음 | 기본값으로 부적합 |
| 서비스 UID/GID별 Secret 파일 제공 | 현재 보안 원칙 유지 가능 | non-root 문제 해결 | 중간 | 권고 |
| `DATABASE_URL_FILE` 추가 | raw env 미사용 | adapter 필요 | 중간 | 보조 계약으로 적합 |
| Heimdall bootstrap이 root로 읽고 앱 실행 | 앱 수정 감소 | 높음 | 높음 | entrypoint 침범 위험 |

Docker 공식 문서는 Secret을 `/run/secrets/*` 파일로 전달하는 방식을 권장하지만, `*_FILE`은 일부 공식 이미지가 사용하는 관례라고 설명한다.

- [Docker Compose Secrets](https://docs.docker.com/compose/how-tos/use-secrets/)
- [Docker Compose service secret 권한](https://docs.docker.com/reference/compose-file/services/)

즉 파일 Secret은 적절한 보안 수단이지만, 파일을 읽을 컨테이너 사용자의 identity까지 플랫폼 계약에 포함해야 한다.

## 8. 권고 설계

### 8.1 기본 원칙

다음 원칙은 유지한다.

- raw DB 비밀번호를 API, Control DB, deployment snapshot, event, Docker environment와 로그에 저장하지 않는다.
- DB 접근을 활성화한 서비스에만 credential을 전달한다.
- canonical Secret은 private directory에 owner-only로 보관한다.
- 컨테이너에는 read-only 파일로 전달한다.

### 8.2 서비스별 Secret materialization

배포 시 canonical Secret을 컨테이너에 직접 bind mount하지 않고, 서비스별 전용 사본을 만든다.

권고 흐름:

```text
Canonical Secret
  root 전용 private directory
  reference + fingerprint 검증
          |
          v
Per-service Secret file
  서비스 runtime UID/GID 소유
  mode 0400
  상위 directory는 root 전용
          |
          v
Application container
  기존 Dockerfile USER 유지
  read-only mount
```

서비스마다 실행 UID/GID가 다를 수 있으므로 같은 물리 파일을 여러 서비스에 재사용하지 않는다.

### 8.3 runtime identity 계약

서비스 설정에 숫자형 runtime identity를 명시할 수 있도록 하는 방안을 권고한다.

개념 예시:

```json
{
  "name": "backend",
  "projectDatabaseAccess": true,
  "runtimeIdentity": {
    "uid": 1000,
    "gid": 1000
  }
}
```

이 예시는 권고안이며 현재 공개 schema가 아니다.

정책 제안:

1. 숫자형 UID/GID가 설정되어 있으면 해당 값을 사용한다.
2. 설정이 없고 image `Config.User`가 숫자라면 해당 값을 사용할 수 있다.
3. named user를 안전하게 해석할 수 없으면 배포 전에 명확히 실패시킨다.
4. root 이미지에는 기존 root 소유 `0400` 방식을 사용할 수 있다.

### 8.4 연결 형식 호환성

기본 계약은 현재처럼 다음 값을 유지할 수 있다.

```text
DATABASE_HOST
DATABASE_PORT
DATABASE_NAME
DATABASE_USER
DATABASE_SCHEMA
DATABASE_PASSWORD_FILE
```

추가로 다음을 고려할 수 있다.

- Node.js, Python, Java용 공식 adapter 예제
- 완성된 connection URL을 담은 `DATABASE_URL_FILE`
- 로컬 Compose와 Heimdall 설정을 함께 처리하는 작은 helper library

raw `DATABASE_URL` 환경변수는 호환성은 높지만 현재 Heimdall의 Secret 비노출 원칙과 충돌한다. 지원하더라도 기본값이 아니라 명시적 정책 예외로 분리하는 것이 적절하다.

## 9. 오류 처리 개선

현재의 애플리케이션 `EACCES` 대신 Heimdall이 다음과 같은 배포 전 오류를 제공하는 것이 좋다.

| 상황 | 권고 오류 코드 | 사용자 안내 |
| --- | --- | --- |
| runtime user를 결정할 수 없음 | `SECRET_RUNTIME_USER_REQUIRED` | 숫자형 UID/GID를 지정하도록 안내 |
| Secret target user와 권한 불일치 | `SECRET_PERMISSION_MISMATCH` | image user, file owner, mode를 함께 표시 |
| DB 환경변수 계약 일부 누락 | `DATABASE_CONTRACT_INCOMPLETE` | 누락된 환경변수 목록 표시 |
| Secret reference 또는 fingerprint 불일치 | 기존 Secret 오류 유지 | Secret 재설정 또는 배포 중단 안내 |

UI에도 다음 내용을 표시할 필요가 있다.

- Compose의 `db` 서비스는 실행되지 않음
- Managed PostgreSQL은 별도 외부 서비스임
- 실제 주입되는 환경변수 목록
- 비밀번호는 파일로 전달됨
- non-root 이미지에 필요한 runtime identity 설정

## 10. 단계별 적용 계획

### 단계 0: 문서와 UI 보강

- Compose 직접 실행이 비범위임을 서비스 설정 화면에 표시한다.
- 주입되는 여섯 개 환경변수를 표시한다.
- password file과 container user 권한 요구사항을 표시한다.

이 단계는 즉시 적용할 수 있지만 권한 문제의 근본 해결은 아니다.

### 단계 1: 배포 전 preflight

- image `Config.User`를 확인한다.
- Secret 파일 owner/mode와 실제 접근 가능성을 검증한다.
- 불일치 시 컨테이너 시작 전에 배포를 실패시킨다.

### 단계 2: 서비스별 Secret materialization

- 서비스별 private Secret 파일을 생성한다.
- target UID/GID와 mode `0400`을 적용한다.
- 정확한 service/deployment reference로 lifecycle을 관리한다.
- 실패와 cleanup에서 다른 deployment의 파일을 삭제하지 않도록 fencing한다.

### 단계 3: 애플리케이션 호환성 계층

- 언어별 공식 연결 adapter를 제공한다.
- `DATABASE_URL_FILE` 지원 여부를 결정한다.
- 로컬 Compose와 Heimdall을 함께 다루는 예제를 제공한다.

## 11. 필수 검증 기준

권고 설계를 구현할 경우 다음 테스트가 필요하다.

- [ ] `USER 1000:1000` 이미지가 root 시작 없이 password file을 읽는다.
- [ ] non-root 이미지가 DB health `200`을 반환한다.
- [ ] UID가 다른 두 서비스가 각자의 Secret 파일로 같은 project DB에 연결한다.
- [ ] DB를 사용하지 않는 서비스에는 Secret이 mount되지 않는다.
- [ ] raw password가 Docker inspect environment에 나타나지 않는다.
- [ ] raw password가 API, Control DB, deployment snapshot, event와 로그에 나타나지 않는다.
- [ ] Secret 파일은 target UID/GID, mode `0400`, read-only다.
- [ ] 실패한 deployment의 서비스별 Secret만 정확하게 정리된다.
- [ ] active deployment의 Secret은 candidate 실패로 삭제되지 않는다.
- [ ] named user 또는 잘못된 UID/GID는 컨테이너 시작 전에 명확한 오류로 거부된다.

## 12. 최종 결론

현재 Heimdall의 DB 주입 방식은 보안을 우선한 설계이지만, 애플리케이션이 그 보안 구현의 일부를 직접 책임져야 한다는 문제가 있다.

특히 root 전용 `0400` bind mount는 non-root 컨테이너와 구조적으로 호환되지 않는다. 이 문제를 문서만으로 해결하면 프로젝트마다 root bootstrap과 권한 강등 코드가 반복되고, 구현 실수에 따라 서버가 root로 계속 실행될 위험이 생긴다.

따라서 최종 목표는 다음 문장으로 정의할 수 있다.

> 기존 non-root 애플리케이션 이미지가 root bootstrap 코드를 추가하지 않고도, raw 비밀번호를 환경변수에 노출하지 않은 상태로 Managed PostgreSQL에 연결할 수 있어야 한다.

문서 보강과 preflight는 단기 대응으로 수행하고, 서비스 runtime identity와 서비스별 Secret materialization을 근본 해결책으로 추진하는 것을 권고한다.
