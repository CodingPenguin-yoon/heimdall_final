# 5. 운영과 장애 대응

## 기본 원칙

1. 재시작만 필요하면 DB volume을 지우지 않는다.
2. Ubuntu에서는 Control Compose 명령마다 Linux override를 함께 사용한다.
3. 실제 배포 테스트는 기존 프로젝트의 새 배포만으로 충분하다.
4. 실패하면 UI event, Worker log, 실패 diagnostics, Docker 상태 순서로 본다.
5. 이름만 보고 Docker resource를 수동 삭제하지 않는다.

## 현재 branch와 commit 확인

```bash
git status
git branch --show-current
git log -1 --oneline
```

서버에 untracked 또는 modified 파일이 있으면 무조건 reset하지 말고 먼저 내용과 소유자를 확인한다.

## Docker Desktop에서 Control Plane 시작

Edge와 Managed DB가 준비됐다는 전제다.

```bash
docker compose --env-file .env \
  -f infra/dev/compose.yaml \
  up -d --build --wait
```

## Ubuntu Docker Engine에서 Control Plane 시작

```bash
docker compose --env-file .env \
  -f infra/dev/compose.yaml \
  -f infra/dev/compose.linux.yaml \
  up -d --build --wait
```

Linux override는 Deployment Worker와 Routing Worker만 host network로 실행한다. 두 Worker는 host의
loopback candidate, Preview, Edge와 Control DB endpoint를 직접 검사한다.

이 override는 Managed DB network까지 자동 해결하지 않는다. DB 사용 application container에는
실제로 도달 가능한 private Managed DB host와 port를 설정해야 한다.

## 상태 확인

```bash
docker compose --env-file .env \
  -f infra/dev/compose.yaml \
  -f infra/dev/compose.linux.yaml \
  ps
```

Ubuntu Worker의 network mode 확인:

```bash
docker inspect \
  --format '{{.HostConfig.NetworkMode}}' \
  heimdall-python-local-worker-1

docker inspect \
  --format '{{.HostConfig.NetworkMode}}' \
  heimdall-python-local-routing-worker-1
```

둘 다 `host`가 나오면 Linux override가 적용된 것이다.

## 로그 확인

최근 로그만 확인:

```bash
docker compose --env-file .env \
  -f infra/dev/compose.yaml \
  -f infra/dev/compose.linux.yaml \
  logs --tail=200 worker routing-worker api
```

실시간 follow:

```bash
docker compose --env-file .env \
  -f infra/dev/compose.yaml \
  -f infra/dev/compose.linux.yaml \
  logs --follow worker routing-worker
```

`logs --follow`는 새 로그가 올 때까지 기다리므로 멈춘 것처럼 보이는 것이 정상이다. `Ctrl+C`로
빠져나와도 container는 계속 실행된다.

## 안전하게 중지하고 다시 시작

중지:

```bash
docker compose --env-file .env \
  -f infra/dev/compose.yaml \
  -f infra/dev/compose.linux.yaml \
  stop
```

재시작:

```bash
docker compose --env-file .env \
  -f infra/dev/compose.yaml \
  -f infra/dev/compose.linux.yaml \
  up -d --wait
```

Worker만 새 이미지와 설정으로 재생성:

```bash
docker compose --env-file .env \
  -f infra/dev/compose.yaml \
  -f infra/dev/compose.linux.yaml \
  up -d --build --no-deps --force-recreate worker routing-worker
```

## 이번 Ubuntu network 문제를 이해하는 법

### 문제가 있었던 구조

```text
candidate port: host 127.0.0.1에만 공개
Worker: bridge container에서 host.docker.internal로 검사
Ubuntu: host.docker.internal은 bridge gateway
결과: host loopback listener에 도달하지 못함
```

앱 자체는 정상이어도 Worker가 접근하지 못해 `SERVICE_HEALTH_TIMEOUT`으로 판단했다.

### 수정된 구조

```text
Worker: host network
probe: 127.0.0.1:<임시포트>
Control DB: 127.0.0.1:55432
```

Routing Worker도 Edge의 loopback listener를 검사하므로 같은 방식으로 host network를 사용한다.

## 배포 기능을 테스트하는 가장 안전한 방법

DB나 volume을 삭제할 필요가 없다.

1. Control Plane 상태가 healthy인지 확인한다.
2. 기존 프로젝트 또는 새 테스트 프로젝트를 연다.
3. 같은 commit이라도 새 deployment를 요청한다.
4. 상태가 `HEALTH_CHECKING`을 지나 `ACTIVATING`, `SUCCEEDED`가 되는지 본다.
5. stable Preview에 접속한다.
6. public hostname을 사용한다면 Routing Worker가 `ACTIVE`로 적용하는지 본다.
7. 실패하면 deployment 상세 event와 Worker log를 확인한다.

매 배포마다 candidate와 임시 health port를 새로 만들기 때문에 기존 DB를 유지해도 network fix를
검증할 수 있다.

## 자주 만나는 오류

### `SERVICE_HEALTH_TIMEOUT`

확인 순서:

1. application container가 실행 중인지
2. container 내부 service가 선언한 port에서 listen하는지
3. health path가 정확한지
4. application이 실제로 2xx/3xx를 반환하는지
5. Worker에서 publish port로 접근 가능한지
6. Ubuntu에서 Linux override가 적용됐는지

앱 로그의 200만 보고 Worker 경로도 정상이라고 판단하면 안 된다. 앱 내부와 Worker의 network 경로는
다를 수 있다.

### `GATEWAY_START_FAILED`

candidate health가 실패해 Gateway activation이 시작되지 않았거나, Gateway 생성·port 재사용·route
probe가 실패했을 수 있다. deployment error와 routing job error는 서로 다른 작업에서 발생할 수 있다.

확인할 것:

- 바로 앞 deployment의 실패 단계
- Project Gateway container 존재와 상태
- stable Preview port 충돌
- Gateway NGINX config와 probe 결과
- Routing Worker의 Edge attachment 상태

### public hostname은 안 되지만 Preview는 됨

Project Gateway와 application은 정상이고 Shared Edge 또는 public routing 쪽 문제일 가능성이 높다.

- Edge container 상태
- exact hostname이 applied 상태인지
- Routing Worker 로그
- Edge config test/reload 결과
- DNS, TLS, 외부 reverse proxy, private tunnel

외부 DNS/TLS/reverse proxy는 저장소 밖 운영 책임이라는 점도 확인한다.

### 관리 화면은 안 되지만 프로젝트는 됨

Control frontend, API, admin authentication 또는 management hostname 경로 문제일 수 있다. 이미 적용된
project data path는 Control Plane과 분리되어 계속 응답할 수 있다.

## 초기화 수준을 구분하기

### 수준 1: process 재시작

`stop` 후 `up`. 데이터와 container 정의를 보존한다. 대부분의 테스트는 여기서 시작한다.

### 수준 2: Control container 재생성

`up --force-recreate`. named volume을 유지하므로 Control DB 데이터는 보존된다.

### 수준 3: Control Compose `down`

Control container와 Compose network를 제거하지만 `-v`가 없으면 named volume은 보존한다. Project
runtime과 Edge는 별도 lifecycle이라 남을 수 있다.

### 수준 4: `down -v`

데이터 삭제다. Control DB만 지우면 Docker runtime과 metadata가 불일치할 수 있다. Managed DB
Compose의 `down -v`는 application data까지 삭제한다.

완전 초기화가 정말 필요하면 삭제 대상과 복구 가능성을 먼저 목록으로 만들고 별도 절차로 수행한다.

## 운영 체크리스트

### 배포 전

- [ ] 올바른 Git branch와 commit인가
- [ ] 작업 트리에 예상하지 못한 변경이 없는가
- [ ] Edge와 Control Compose가 healthy인가
- [ ] Ubuntu라면 Linux override를 적용했는가
- [ ] Managed DB endpoint가 application container에서 도달 가능한가

### 배포 후

- [ ] deployment가 `SUCCEEDED`인가
- [ ] stable Preview가 응답하는가
- [ ] 기존 서비스가 중단되지 않았는가
- [ ] public route가 `ACTIVE`인가
- [ ] Worker log에 반복 retry나 cleanup 실패가 없는가

### 장애 시

- [ ] 실패 단계와 error code를 기록했는가
- [ ] application log와 Worker log를 구분했는가
- [ ] Control Plane과 Data Plane 중 어디가 실패했는가
- [ ] DB를 삭제하기 전에 backup과 남은 Docker resource를 확인했는가
- [ ] 불확실한 resource를 수동 삭제하기 전에 exact label을 확인했는가
