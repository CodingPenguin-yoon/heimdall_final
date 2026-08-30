# 2. 실행 원리와 배포 흐름

## 시스템을 처음 켤 때

세 개의 수명주기를 순서대로 생각하면 된다.

```text
1. Edge lifecycle
2. Managed DB lifecycle
3. Control Plane lifecycle
```

### 1단계: Shared Edge 시작

`infra/edge/compose.yaml`이 Shared Edge NGINX와 고정 `heimdall-edge` network를 준비한다. Control
frontend와 Project Gateway가 나중에 이 network에 참여한다.

### 2단계: Managed PostgreSQL 시작

별도 저장소 또는 별도 VM의 Managed PostgreSQL을 시작한다. DB를 사용하지 않는 프로젝트만 시험할
때도 Control Plane 설정에 따라 이 단계가 필요할 수 있다.

### 3단계: Control Plane 시작

`infra/dev/compose.yaml`이 다음을 시작한다.

- Control PostgreSQL
- FastAPI API
- Deployment Worker
- Routing Worker
- Control frontend

Docker Desktop은 base Compose만 사용한다. Ubuntu Docker Engine에서는 두 Worker가 host loopback
endpoint를 검사할 수 있도록 `infra/dev/compose.linux.yaml`을 함께 적용한다.

## 로그인할 때

Heimdall에는 고정 관리자 `admin` 한 명만 있다. 회원가입이나 사용자 DB table은 없다.

```text
브라우저 로그인 요청
-> Backend가 owner-only 파일의 Argon2id password hash 확인
-> 8시간 signed session cookie 발급
-> 이후 관리 API는 cookie 검사
-> 변경 요청은 session 전용 CSRF token도 검사
```

password hash와 session signing key는 저장소 밖의 관리자 전용 파일에 있다. API만 read-only로
mount하며 Worker, frontend, project container에는 전달하지 않는다.

## 프로젝트를 등록할 때

```text
관리자가 public GitHub URL 입력
-> API가 URL과 main branch 접근 확인
-> Control DB에 project 저장
-> 초기 상태 DRAFT
```

프로젝트 등록은 아직 배포가 아니다. 서비스와 route 설정이 유효해야 `READY`가 된다.

## 프로젝트 설정을 저장할 때

운영자는 다음을 지정한다.

- service 이름
- Docker build context와 Dockerfile
- container 내부 port
- health check path
- plain 환경변수와 secret 환경변수
- project database 사용 여부
- Project Gateway의 path route와 목적 service

현재 편집 가능한 최신 설정은 project aggregate에 저장된다. 이 설정을 나중에 바꾸더라도 이미 요청한
배포의 설정은 바뀌지 않는다.

## 배포 버튼을 누르면

### 1. API가 요청을 접수한다

API는 선택한 commit이 허용된 `main` 이력에 포함됐는지 확인한다. 그런 다음 현재 프로젝트 설정을
그대로 복사해 immutable deployment snapshot을 만든다.

```text
현재 편집 설정 version 7
-> 배포 요청
-> deployment 안에 version 7 snapshot 저장
-> 이후 프로젝트 설정을 version 8로 바꿔도 진행 중 배포는 version 7 사용
```

이 원칙은 "배포 중에 설정이 바뀌어 결과를 설명할 수 없는 상황"을 막는다.

### 2. Deployment Worker가 작업을 claim한다

Worker는 Control DB queue를 주기적으로 확인한다. 작업 하나를 가져갈 때 claim token과 lease를 받는다.

- claim token: 이 Worker가 현재 작업 소유자라는 증표
- lease: 소유권이 유효한 시간
- heartbeat: 긴 build 중에도 Worker가 살아 있음을 갱신

Worker가 죽어 lease가 만료되면 다른 Worker가 작업을 회수할 수 있다. 이전 Worker의 늦은 결과는
claim token 검사를 통과하지 못하므로 현재 상태를 덮어쓸 수 없다.

### 3. 정확한 source를 준비한다

Worker는 deployment에 기록된 exact commit SHA를 전용 workspace에 checkout한다. 단순히 "현재 main"
을 다시 사용하지 않으므로 기록된 배포와 실제 source가 일치한다.

### 4. Docker image를 build한다

snapshot에 포함된 각 service의 Dockerfile로 image를 만든다. resource에는 project ID와 deployment
ID를 포함한 deterministic 이름과 label이 붙는다.

label은 cleanup 안전장치다. 같은 이름만 보인다고 삭제하지 않고, 정확한 관리 label과 deployment
identity가 맞아야 Heimdall resource로 취급한다.

### 5. candidate generation을 실행한다

새 generation 전용 Docker network와 service container를 만든다. 여러 service는 이 network 안의
DNS alias로 통신한다.

각 service port는 health 검사를 위해 host의 임시 loopback port에 publish된다.

```text
127.0.0.1:<임시 포트> -> candidate service 내부 port
```

아직 Project Gateway가 candidate를 사용자에게 연결하지 않았으므로 기존 서비스는 계속 트래픽을
처리한다.

### 6. 모든 service health를 검사한다

Worker는 service별 health path를 호출한다. 모든 service가 성공해야 다음 단계로 간다.

```text
web /health 200
api /health 200
worker 판단: candidate healthy
```

하나라도 제한시간 안에 성공하지 않으면 `SERVICE_HEALTH_TIMEOUT` 같은 안정적인 오류 코드로 실패한다.
기존 active generation은 유지하고 실패 candidate를 안전하게 정리한다.

### 7. Project Gateway를 전환한다

Worker는 새 generation의 service alias와 route를 반영한 NGINX 설정을 만든다. Gateway를 candidate
network 기준으로 재생성·검사하고, 기존 stable Preview port가 그대로 유지되는지도 확인한다.

성공한 뒤에만 Control DB의 `active_*` runtime metadata를 새 deployment로 갱신한다. 중간에 실패하면
가능한 경우 last-known-good generation으로 Gateway를 복구한다.

### 8. 성공 처리와 정리

배포 상태를 `SUCCEEDED`로 기록하고 public route 작업을 깨운다. 안전하다고 확인된 이전 generation과
candidate resource를 정리한다. 불확실한 resource는 즉시 지우지 않고 reconciliation 대상으로 남긴다.

## 배포 상태를 읽는 법

대표적인 흐름은 다음과 같다.

```text
QUEUED
-> PREPARING
-> BUILDING
-> STARTING
-> HEALTH_CHECKING
-> ACTIVATING
-> SUCCEEDED
```

실패하면 어느 단계에서 어떤 stable error code가 발생했는지 기록된다. UI의 event와 실패 artifact는
"어디까지 성공했고 어디서 실패했는지" 찾는 데 사용한다.

## public hostname을 설정하면

배포와 public routing은 별도 작업이다.

```text
관리자가 subdomain 요청
-> API가 desired hostname과 revision 저장
-> routing job 생성
-> Routing Worker claim
-> Project Gateway 존재와 identity 확인
-> Gateway를 heimdall-edge network에 deterministic alias로 연결
-> 전체 Edge config 생성
-> nginx -t
-> config 원자적 교체와 reload
-> management/project hostname probe
-> applied revision과 ACTIVE 상태 저장
```

desired는 사용자가 원하는 상태이고 applied는 Edge에 실제로 확인된 상태다. 둘을 분리하면 reload 중
장애가 나도 "요청은 했지만 아직 적용되지 않은 상태"를 정확히 표현할 수 있다.

## 실패했을 때 기존 서비스가 유지되는 이유

새 candidate는 검사가 끝날 때까지 기존 Project Gateway에 연결되지 않는다. 따라서 build 실패,
container 시작 실패, health 실패는 보통 기존 active generation에 영향을 주지 않는다.

이것이 Heimdall의 가장 중요한 안전 원칙이다.

> 새 버전이 정상임을 먼저 증명하고, 그 다음 연결한다.

## Worker가 중간에 죽으면

재시작한 Worker는 DB만 믿고 무조건 삭제하지 않는다. DB active metadata, Docker label, Gateway 상태,
response marker를 비교해 다음을 판단한다.

- 이미 activation이 끝났음: 성공 상태를 마무리
- 다시 시도해도 안전함: candidate 정리 후 재시도
- 확실하지 않음: `UNCERTAIN`으로 남기고 자동 삭제 중단

불확실할 때 보존하는 이유는 잘못된 cleanup으로 실제 서비스까지 지우는 것보다 orphan resource를
잠시 남기는 편이 안전하기 때문이다.
