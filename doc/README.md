# Heimdall 사용자 이해 가이드

이 문서는 Heimdall을 직접 운영하지만 아직 전체 구조가 익숙하지 않은 사람을 위한 설명서다.
코드를 읽지 않아도 "무엇이 왜 존재하고, 버튼을 누르면 내부에서 무슨 일이 생기며, 어떤 데이터를
지우면 무엇이 사라지는지" 이해하는 것을 목표로 한다.

기존 `project-docs/`는 구현 계약과 정확한 기술 규칙을 보존하는 개발자용 문서다. 이 `doc/` 폴더는
그 내용을 쉬운 말과 운영자 관점으로 다시 설명한다. 두 문서가 다르게 보이면 `project-docs/`와 실제
코드가 최종 기준이다.

## 가장 먼저 알아야 할 한 문장

Heimdall은 GitHub의 코드를 받아 새 Docker 컨테이너로 시험 실행한 뒤, 정상이라고 확인된 버전만
기존 서비스 대신 연결해 주는 단일 서버용 배포 관리 도구다.

## 식당에 비유하면

| Heimdall 구성요소  | 쉬운 비유            | 하는 일                                                       |
| ------------------ | -------------------- | ------------------------------------------------------------- |
| 관리 화면          | 주문 화면            | 프로젝트 등록, 설정, 배포 요청                                |
| API                | 접수 담당자          | 요청을 검사하고 DB에 작업을 기록                              |
| Control PostgreSQL | 운영 장부            | 프로젝트 설정, 배포 상태, 작업, 라우팅 정보 저장              |
| Deployment Worker  | 조리 담당자          | 코드 checkout, 이미지 build, 컨테이너 실행, health 검사, 교체 |
| Routing Worker     | 주소록 담당자        | public hostname과 Project Gateway 연결                        |
| Docker Engine      | 작업장               | 실제 컨테이너, 이미지, network 실행                           |
| Project Gateway    | 프로젝트 전용 스위치 | 현재 정상 버전으로 요청 전달                                  |
| Shared Edge        | 건물 정문 안내원     | hostname을 보고 관리 화면 또는 프로젝트로 전달                |
| Managed PostgreSQL | 별도 데이터 창고     | 배포된 애플리케이션의 실제 업무 데이터 저장                   |

## 추천 읽기 순서

1. [전체 아키텍처](01-architecture.md): 구성요소와 네트워크의 큰 그림
2. [실행 원리와 배포 흐름](02-execution-flow.md): 버튼을 누른 뒤 실제로 일어나는 일
3. [저장소와 코드 구조](03-repository-structure.md): 폴더와 파일의 책임
4. [데이터와 생명주기](04-data-and-lifecycle.md): DB, Docker resource, volume, secret의 소유자
5. [운영과 장애 대응](05-operations.md): 시작, 중지, 테스트, 로그, 초기화 판단

## 10분 요약

### Heimdall이 관리하는 것은 두 종류다

- Control Plane: 관리 화면, API, Worker, Control DB처럼 "배포를 관리하는 시스템"
- Project Runtime: 실제 사용자가 접속하는 프로젝트 서비스와 Project Gateway

Control Plane을 잠시 내려도 이미 실행 중인 Project Runtime은 원칙적으로 계속 동작한다. 반대로
Docker daemon이나 Runtime VM 자체가 내려가면 둘 다 영향을 받는다.

### 배포는 바로 교체하지 않는다

새 버전은 먼저 candidate라는 시험용 세대로 실행된다. 모든 서비스의 health check가 성공해야
Project Gateway가 새 세대를 바라본다. 실패하면 기존 정상 세대는 그대로 유지하고 candidate만
정리한다.

### public hostname과 Preview는 입구만 다르다

```text
Preview: 운영자 -> 127.0.0.1:고정포트 -> Project Gateway -> 현재 서비스
Public:  사용자 -> Shared Edge -> Project Gateway -> 현재 서비스
```

두 경로는 같은 Project Gateway에서 합쳐진다. Shared Edge가 애플리케이션 컨테이너를 직접 가리키지
않는 이유는 배포 때마다 바뀌는 컨테이너 정보를 전역 라우터가 알 필요 없게 하기 위해서다.

### DB는 두 개의 역할로 나뉜다

- Control PostgreSQL: Heimdall 자체의 프로젝트·배포·작업·라우팅 장부
- Managed PostgreSQL: 배포된 프로젝트가 사용하는 애플리케이션 데이터

Control DB만 지우면 Docker 컨테이너가 남아도 Heimdall이 그 컨테이너의 상태와 소유 관계를 잃을 수
있다. 따라서 `down -v`는 단순 재시작 명령이 아니라 데이터 초기화 명령이다.

## 자주 쓰는 용어

| 용어                | 뜻                                                                         |
| ------------------- | -------------------------------------------------------------------------- |
| candidate           | 아직 사용자 트래픽을 받지 않는 새 배포 시험 세대                           |
| generation          | 한 번의 배포로 만들어진 서비스 컨테이너와 전용 network 묶음                |
| active generation   | Project Gateway가 현재 연결한 정상 세대                                    |
| stable Preview port | 배포가 바뀌어도 유지되는 프로젝트별 host loopback 포트                     |
| Project Gateway     | 한 프로젝트의 route와 active generation을 관리하는 NGINX                   |
| Shared Edge         | hostname을 보고 관리 화면이나 Project Gateway로 전달하는 공용 NGINX        |
| Control Plane       | 배포를 지시하고 상태를 관리하는 API, Worker, Control DB, UI                |
| Data Plane          | 이미 적용된 실제 사용자 요청 경로                                          |
| snapshot            | 배포 요청 순간의 설정을 복사해 이후 수정과 분리한 불변 기록                |
| claim/lease         | 여러 Worker가 같은 작업을 동시에 수행하지 않게 하는 작업 소유권과 유효시간 |
| reconciliation      | DB 기록과 실제 Docker 상태를 비교해 중단된 작업을 안전하게 복구하는 과정   |

## 이 문서의 범위

현재 Heimdall은 범용 클라우드 배포 플랫폼이 아니다. 단일 Docker host, public GitHub 저장소,
`main` branch, 한 명의 고정 관리자라는 명확한 범위를 가진 operator-specific alpha다. Kubernetes,
자동 배포 webhook, private Git, 다중 사용자 권한, 다중 서버 failover는 현재 범위가 아니다.
