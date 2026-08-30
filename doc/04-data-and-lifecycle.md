# 4. 데이터와 생명주기

이 문서는 "무엇을 내리거나 지우면 무엇이 사라지는가"를 설명한다. 운영 중 가장 중요한 부분이다.

## 네 종류의 상태

### 1. Control PostgreSQL

Heimdall 운영 장부다.

저장하는 것:

- project와 설정
- deployment snapshot과 상태
- deployment/routing job
- active runtime metadata
- public route desired/applied 상태
- event와 실패 diagnostics
- Managed DB metadata와 secret reference

저장하지 않는 것:

- application 업무 row
- raw project secret
- admin password 원문
- Docker image와 container 자체

Control DB volume을 삭제하면 UI에서 보던 프로젝트와 배포 기록을 잃는다. Docker resource가 자동으로
같이 삭제되는 것은 아니므로 DB와 실제 Docker 사이에 orphan 상태가 생길 수 있다.

### 2. Managed PostgreSQL

배포된 애플리케이션의 실제 데이터다.

예:

- 애플리케이션 사용자
- 게시글
- 주문
- 프로젝트 도메인 model

Control Plane과 별도 Compose 또는 VM으로 운영한다. Control DB를 backup했다고 Managed DB가 backup된
것은 아니며 반대도 마찬가지다.

### 3. 저장소 밖 secret file

두 부류가 있다.

- 관리자 인증: password hash와 session signing key
- project runtime secret: 환경변수 secret과 Managed DB password file

Control DB는 file의 reference, version, fingerprint 같은 metadata만 가진다. container에는 필요한
secret file만 read-only mount하며 raw 값을 environment로 복사하지 않는다.

### 4. Docker resource

- Control Plane container와 volume
- Shared Edge container와 network
- Project Gateway
- generation network
- application container
- service image

Docker resource 이름과 label에는 소유 project/deployment identity가 들어간다. cleanup은 이름뿐 아니라
정확한 label까지 확인한다.

## 누가 무엇을 소유하는가

| 대상                         | 소유 lifecycle        | 일반적으로 사용하는 Compose/주체      |
| ---------------------------- | --------------------- | ------------------------------------- |
| Control PostgreSQL           | Control Plane         | `infra/dev/compose.yaml`              |
| API, 두 Worker, frontend     | Control Plane         | `infra/dev/compose.yaml`              |
| Shared Edge와 edge network   | Edge                  | `infra/edge/compose.yaml`             |
| Project Gateway와 generation | Project runtime       | Deployment Worker가 Docker CLI로 관리 |
| Managed PostgreSQL           | Managed DB            | 별도 Compose 또는 VM                  |
| admin auth files             | 운영자                | 저장소 밖 owner-only directory        |
| project secret files         | Heimdall runtime root | Deployment Worker와 secret store      |

## `stop`, `down`, `down -v`의 차이

### `stop`

container를 멈추지만 container, network, volume 정의는 보존한다. 다시 `start` 또는 `up`하기 쉽다.
일반적인 점검과 재시작에 가장 안전하다.

### `down`

해당 Compose project의 container와 network를 제거한다. named volume은 `-v`를 붙이지 않으면 보존한다.
다시 `up`하면 container가 새로 만들어진다.

### `down -v`

container와 network뿐 아니라 Compose가 소유한 named volume도 삭제한다. Control Compose에서 실행하면
Control PostgreSQL 데이터가 사라진다. Managed DB Compose에서 실행하면 application data가 사라진다.

> `-v`는 "깨끗하게 재시작" 옵션이 아니라 "데이터 삭제" 옵션이다.

## Control Plane을 멈추면

| 대상                    | 영향                                         |
| ----------------------- | -------------------------------------------- |
| 관리 UI/API             | 사용할 수 없음                               |
| 새 배포와 hostname 변경 | 진행되지 않음                                |
| 기존 public hostname    | Edge와 Project Gateway가 살아 있으면 유지    |
| 기존 Preview            | Project Gateway와 service가 살아 있으면 유지 |
| Managed PostgreSQL      | 별도 lifecycle이므로 계속 실행 가능          |

이미 적용된 사용자 요청 경로에는 API, Worker, Control DB가 포함되지 않기 때문이다.

## Edge를 멈추면

- 모든 public hostname 요청이 실패한다.
- loopback Preview는 Project Gateway를 직접 사용하므로 유지될 수 있다.
- 관리 hostname도 Edge를 통해 들어오면 사용할 수 없다.

Edge는 모든 public hostname의 공용 입구이므로 영향 범위가 크다.

## 한 Project Gateway가 멈추면

- 해당 프로젝트 public hostname과 Preview가 실패한다.
- 다른 프로젝트 Gateway와 Shared Edge는 계속 동작한다.

프로젝트마다 Gateway를 분리한 이유 중 하나다.

## Managed DB를 멈추면

- DB를 사용하지 않는 프로젝트는 계속 동작할 수 있다.
- DB를 사용하는 프로젝트의 요청은 실패하거나 기능 일부가 고장난다.
- Control Plane과 public routing 자체는 계속 동작할 수 있다.

## Docker daemon 또는 Runtime VM을 멈추면

Control Plane, Edge, Project Gateway, application container가 모두 같은 host에 있다면 전체가 영향을
받는다. 현재 자동 VM failover는 없다.

## 배포 세대의 생명주기

```text
생성 전
-> image build
-> generation network 생성
-> candidate container 생성
-> health 검사
-> Gateway activation
-> active generation
-> 다음 배포 성공 후 이전 generation 정리
```

실패 candidate는 안전하다고 판단될 때 정리한다. crash 시점이 애매하면 자동 삭제하지 않고 보존해
reconciliation이 다시 확인한다.

## DB만 초기화하면 위험한 이유

예를 들어 Control DB를 지웠지만 기존 Project Gateway와 application container가 남아 있다고 하자.

```text
Docker: Project A가 실행 중
Control DB: Project A 기록 없음
```

Heimdall은 그 resource가 현재 정상 서비스인지, 어느 deployment 소유인지, 어떤 Preview port를
사용하는지 장부로 확정할 수 없다. 무작정 adopt하거나 삭제하지 않도록 설계되어 있으므로 관리가 더
어려워질 수 있다.

완전 초기화가 필요하면 먼저 다음 범위를 따로 확인해야 한다.

1. Control DB metadata
2. Project Gateway와 generation resource
3. Edge route snapshot
4. Managed DB application data
5. runtime secret files
6. admin auth files

이들은 서로 다른 소유자와 복구 의미를 가진다.

## 백업 관점

최소한 다음은 별도로 생각해야 한다.

- Control PostgreSQL dump 또는 volume backup
- Managed PostgreSQL dump 또는 volume/VM backup
- admin authentication directory의 안전한 backup
- project secret runtime root의 안전한 backup
- `.env`와 운영자 관리 Edge/reverse-proxy 설정

Docker image와 Git source는 다시 만들 수 있지만 DB와 secret은 같은 방식으로 복구할 수 없을 수 있다.
