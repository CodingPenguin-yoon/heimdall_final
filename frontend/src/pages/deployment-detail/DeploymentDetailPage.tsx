import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { Link, useParams } from 'react-router';

import {
  deploymentEventsQuery,
  deploymentKeys,
  deploymentQuery,
  deploymentServiceLogsQuery,
} from '@/entities/deployment/queries';
import {
  deploymentStatusLabels as statusLabels,
  deploymentStatusTone,
  isDeploymentTerminal,
} from '@/entities/deployment/presentation';
import type {
  Deployment,
  DeploymentEvent,
  DeploymentStatus,
  ServiceLogSnapshot,
} from '@/entities/deployment/types';
import { RuntimeReconciliationPanel } from '@/features/reconcile-runtime/RuntimeReconciliationPanel';
import { ApiError } from '@/shared/api/client';
import { formatDate, shortSha } from '@/shared/lib/format';
import { Icon } from '@/shared/ui/Icon';

const progressSteps: { status: DeploymentStatus; label: string }[] = [
  { status: 'QUEUED', label: '배포 대기' },
  { status: 'PREPARING', label: '소스 준비' },
  { status: 'BUILDING', label: '이미지 빌드' },
  { status: 'STARTING', label: '서비스 시작' },
  { status: 'HEALTH_CHECKING', label: '서비스 상태 확인' },
  { status: 'ACTIVATING', label: 'Preview 전환' },
  { status: 'SUCCEEDED', label: '배포 완료' },
];

const failureSteps: Record<string, DeploymentStatus> = {
  CONFIGURATION: 'PREPARING',
  SOURCE: 'PREPARING',
  SECRET: 'PREPARING',
  BUILD: 'BUILDING',
  DOCKER: 'STARTING',
  START: 'STARTING',
  RUNTIME: 'STARTING',
  CLEANUP: 'STARTING',
  HEALTH: 'HEALTH_CHECKING',
  HEALTH_CHECK: 'HEALTH_CHECKING',
  ACTIVATION: 'ACTIVATING',
  GATEWAY: 'ACTIVATING',
  RECOVERY: 'ACTIVATING',
};

const eventStageLabels: Record<string, string> = {
  QUEUED: '배포 대기',
  PREPARING: '소스 준비',
  BUILDING: '이미지 빌드',
  STARTING: '서비스 시작',
  HEALTH_CHECKING: '상태 확인',
  ACTIVATING: 'Preview 전환',
  SUCCEEDED: '배포 완료',
  FAILED: '배포 실패',
};

type ProgressState = 'pending' | 'current' | 'complete' | 'failed';

function progressState(deployment: Deployment, step: DeploymentStatus): ProgressState {
  if (deployment.status === 'FAILED') {
    const failedStatus = failureSteps[deployment.failureStage ?? ''] ?? 'PREPARING';
    const failedIndex = progressSteps.findIndex((item) => item.status === failedStatus);
    const stepIndex = progressSteps.findIndex((item) => item.status === step);
    if (step === failedStatus) return 'failed';
    return stepIndex < failedIndex ? 'complete' : 'pending';
  }

  const currentIndex = progressSteps.findIndex((item) => item.status === deployment.status);
  const stepIndex = progressSteps.findIndex((item) => item.status === step);
  if (deployment.status === 'SUCCEEDED' || stepIndex < currentIndex) return 'complete';
  if (stepIndex === currentIndex) return 'current';
  return 'pending';
}

function sourceLabel(deployment: Deployment): string {
  return deployment.sourceType === 'MAIN_HEAD' ? 'main 최신 commit' : 'main 특정 commit';
}

function formatEventTime(value: string): string {
  return new Intl.DateTimeFormat('ko-KR', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).format(new Date(value));
}

function eventTone(stage: string): string {
  if (stage === 'SUCCEEDED') return 'succeeded';
  if (stage === 'FAILED') return 'failed';
  return 'active';
}

function DeploymentEventLog({
  items,
  active,
  loading,
  error,
}: {
  items: DeploymentEvent[];
  active: boolean;
  loading: boolean;
  error: boolean;
}) {
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const log = logRef.current;
    if (log) log.scrollTop = log.scrollHeight;
  }, [items.length]);

  return (
    <section className="panel deployment-log-panel">
      <div className="panel-heading deployment-log-heading">
        <div>
          <span className="eyebrow">Deployment events</span>
          <h2>실시간 배포 로그</h2>
          <p>
            Worker가 남긴 단계별 이벤트이며 애플리케이션 stdout과 민감한 값은 포함하지 않습니다.
          </p>
        </div>
        <div className="deployment-log-summary">
          <span className={`deployment-log-state ${active ? 'live' : 'complete'}`}>
            <i /> {active ? '실시간' : '기록 완료'}
          </span>
          <small>{items.length} events</small>
        </div>
      </div>

      <div
        ref={logRef}
        className="deployment-log-body"
        role="log"
        aria-label="배포 이벤트 로그"
        aria-live={active ? 'polite' : 'off'}
        aria-busy={loading}
      >
        {loading ? <p className="deployment-log-empty">배포 로그를 불러오는 중입니다.</p> : null}
        {error ? (
          <p className="deployment-log-empty deployment-log-error">
            배포 로그를 불러오지 못했습니다.
          </p>
        ) : null}
        {!loading && !error && items.length === 0 ? (
          <p className="deployment-log-empty">
            아직 기록된 이벤트가 없습니다. Worker가 배포를 시작하면 이곳에 표시됩니다.
          </p>
        ) : null}
        {!loading && !error && items.length > 0 ? (
          <ol>
            {items.map((event, index) => (
              <li
                key={event.id}
                className={`${eventTone(event.stage)} ${index === items.length - 1 ? 'latest' : ''}`}
              >
                <time dateTime={event.createdAt}>{formatEventTime(event.createdAt)}</time>
                <span>{eventStageLabels[event.stage] ?? event.stage.replaceAll('_', ' ')}</span>
                <code>{event.code}</code>
                <p>{event.message}</p>
              </li>
            ))}
          </ol>
        ) : null}
      </div>
    </section>
  );
}

const serviceLogErrors: Record<string, string> = {
  SERVICE_LOGS_UNAVAILABLE:
    '이 generation의 컨테이너가 아직 생성되지 않았거나 이미 정리되어 로그를 볼 수 없습니다.',
  RUNTIME_LOG_BROKER_UNAVAILABLE:
    'Worker 로그 조회 연결을 사용할 수 없습니다. Worker 실행 상태를 확인해주세요.',
  SERVICE_LOG_REDACTION_UNAVAILABLE:
    '민감정보 마스킹을 준비하지 못해 로그 원문을 표시하지 않았습니다.',
  SERVICE_LOG_SERVICE_NOT_FOUND: '배포 snapshot에 포함된 서비스를 다시 선택해주세요.',
};

function ServiceLogPanel({ deploymentId }: { deploymentId: string }) {
  const [serviceName, setServiceName] = useState<string>();
  const queryClient = useQueryClient();
  const logs = useQuery(deploymentServiceLogsQuery(deploymentId, serviceName));
  const snapshot = logs.data;
  const rootSnapshot = queryClient.getQueryData<ServiceLogSnapshot>(
    deploymentKeys.serviceLogs(deploymentId),
  );
  const services = snapshot?.services ?? rootSnapshot?.services ?? [];

  const selectedService = serviceName ?? snapshot?.serviceName ?? services[0] ?? '';
  const errorCode = logs.error instanceof ApiError ? logs.error.code : 'REQUEST_FAILED';
  const errorMessage = serviceLogErrors[errorCode] ?? '서비스 로그를 불러오지 못했습니다.';

  return (
    <section className="panel service-log-panel">
      <div className="panel-heading service-log-heading">
        <div>
          <span className="eyebrow">Application output</span>
          <h2>서비스 로그</h2>
          <p>선택한 컨테이너의 stdout·stderr 최근 200줄을 조회 시점 기준으로 보여줍니다.</p>
        </div>
        <div className="service-log-controls">
          <label>
            <span>Service</span>
            <select
              aria-label="로그 서비스 선택"
              value={selectedService}
              disabled={services.length === 0}
              onChange={(event) => setServiceName(event.target.value)}
            >
              {services.length === 0 ? <option value="">서비스 확인 중</option> : null}
              {services.map((service) => (
                <option key={service} value={service}>
                  {service}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            className="button secondary service-log-refresh"
            disabled={logs.isFetching}
            onClick={() => void logs.refetch()}
          >
            <Icon name="refresh" /> {logs.isFetching ? '조회 중' : '새로고침'}
          </button>
        </div>
      </div>

      <div className="service-log-notice" role="note">
        <Icon name="shield" />
        <p>
          Heimdall이 아는 secret은 마스킹하지만 애플리케이션 로그에는 알 수 없는 개인정보나 인증
          정보가 포함될 수 있습니다. 이 snapshot은 저장하지 않습니다.
        </p>
      </div>

      {snapshot && !logs.isError ? (
        <div className="service-log-meta">
          <span>
            <strong>{snapshot.serviceName}</strong> · {snapshot.lines.length} lines
            {snapshot.truncated ? ' · 일부 생략됨' : ''}
          </span>
          <time dateTime={snapshot.retrievedAt}>조회 {formatDate(snapshot.retrievedAt)}</time>
        </div>
      ) : null}

      <div
        className="service-log-output"
        role="log"
        aria-label="서비스 컨테이너 로그"
        aria-live="polite"
        aria-busy={logs.isFetching}
      >
        {logs.isLoading ? (
          <p className="service-log-empty">서비스 로그를 조회하는 중입니다.</p>
        ) : null}
        {logs.isError ? (
          <div className="service-log-empty service-log-error">
            <strong>{errorMessage}</strong>
            <small>{errorCode}</small>
          </div>
        ) : null}
        {snapshot && snapshot.lines.length === 0 && !logs.isError ? (
          <p className="service-log-empty">아직 출력된 서비스 로그가 없습니다.</p>
        ) : null}
        {snapshot && snapshot.lines.length > 0 && !logs.isError ? (
          <ol>
            {snapshot.lines.map((line, index) => (
              <li key={`${line.timestamp}-${line.stream}-${index}`}>
                <time dateTime={line.timestamp}>{formatEventTime(line.timestamp)}</time>
                <span className={`service-log-stream ${line.stream.toLowerCase()}`}>
                  {line.stream}
                </span>
                <pre>{line.message}</pre>
              </li>
            ))}
          </ol>
        ) : null}
      </div>
    </section>
  );
}

function DeploymentResult({ deployment }: { deployment: Deployment }) {
  if (deployment.status === 'FAILED') {
    return (
      <section className="panel deployment-result deployment-result-failed">
        <div>
          <span className="eyebrow">Deployment result</span>
          <h2>배포 실패</h2>
          <p>새 generation은 활성화되지 않았으며 기존 정상 Preview가 있다면 그대로 유지됩니다.</p>
        </div>
        <dl>
          <div>
            <dt>실패 단계</dt>
            <dd>{deployment.failureStage ?? 'UNKNOWN'}</dd>
          </div>
          <div>
            <dt>실패 코드</dt>
            <dd>{deployment.failureCode ?? 'UNKNOWN_FAILURE'}</dd>
          </div>
        </dl>
      </section>
    );
  }

  if (deployment.status === 'SUCCEEDED') {
    return (
      <section className="panel deployment-result deployment-result-succeeded">
        <div>
          <span className="eyebrow">Deployment result</span>
          <h2>배포 완료</h2>
          <p>모든 상태 확인을 통과했고 이 generation이 안정 Preview로 활성화되었습니다.</p>
        </div>
      </section>
    );
  }

  return (
    <section className="panel deployment-result deployment-result-active">
      <div>
        <span className="eyebrow">Current operation</span>
        <h2>{statusLabels[deployment.status]}</h2>
        <p>현황은 배포가 완료되거나 실패할 때까지 자동으로 갱신됩니다.</p>
      </div>
    </section>
  );
}

export function DeploymentDetailPage() {
  const { deploymentId = '' } = useParams();
  const deployment = useQuery(deploymentQuery(deploymentId));
  const deploymentActive = Boolean(
    deployment.data && !isDeploymentTerminal(deployment.data.status),
  );
  const events = useQuery(deploymentEventsQuery(deploymentId, deploymentActive));

  if (deployment.isLoading) {
    return <div className="loading-page">배포 현황을 불러오는 중입니다.</div>;
  }
  if (!deployment.data) {
    return <div className="loading-page">배포 이력을 찾을 수 없습니다.</div>;
  }

  const item = deployment.data;
  const statusTone = deploymentStatusTone(item.status);

  return (
    <div className="page-stack deployment-detail-page">
      <header className="deployment-detail-hero">
        <div>
          <div className="back-line">
            <Link to={`/projects/${item.projectId}`}>Projects</Link>
            <span>/</span>
            <span>{shortSha(item.resolvedCommitSha)}</span>
          </div>
          <span className="eyebrow">Deployment run</span>
          <div className="deployment-detail-title">
            <h1>배포 현황</h1>
            <span className={`deployment-status deployment-status-${statusTone}`}>
              {isDeploymentTerminal(item.status) ? statusLabels[item.status] : '진행 중'}
            </span>
          </div>
          <p>
            <code>{shortSha(item.resolvedCommitSha)}</code> · {sourceLabel(item)} · Config v
            {item.configVersion}
          </p>
        </div>
        <Link to={`/projects/${item.projectId}`} className="button secondary">
          프로젝트로 돌아가기 <Icon name="arrow" />
        </Link>
      </header>

      <section className="panel">
        <div className="panel-heading">
          <div>
            <span className="eyebrow">Immutable snapshot</span>
            <h2>배포 정보</h2>
          </div>
          <span className="result-count">{item.id}</span>
        </div>
        <dl className="deployment-metadata">
          <div>
            <dt>Source</dt>
            <dd>{sourceLabel(item)}</dd>
          </div>
          <div>
            <dt>설정 snapshot</dt>
            <dd>Config v{item.configVersion}</dd>
          </div>
          <div className="deployment-commit">
            <dt>Resolved commit</dt>
            <dd>
              <code>{item.resolvedCommitSha}</code>
            </dd>
          </div>
          <div>
            <dt>요청 시각</dt>
            <dd>{formatDate(item.createdAt)}</dd>
          </div>
          <div>
            <dt>{item.terminalAt ? '종료 시각' : '최근 갱신'}</dt>
            <dd>{formatDate(item.terminalAt ?? item.updatedAt)}</dd>
          </div>
        </dl>
      </section>

      <section className="panel">
        <div className="panel-heading">
          <div>
            <span className="eyebrow">Progress</span>
            <h2>배포 단계</h2>
          </div>
          <span className="result-count">{statusLabels[item.status]}</span>
        </div>
        <ol className="deployment-progress">
          {progressSteps.map((step, index) => {
            const state = progressState(item, step.status);
            return (
              <li key={step.status} className={state}>
                <span>{state === 'complete' ? <Icon name="check" /> : index + 1}</span>
                <div>
                  <strong>{step.label}</strong>
                  <small>{statusLabels[step.status]}</small>
                </div>
              </li>
            );
          })}
        </ol>
      </section>

      <DeploymentEventLog
        items={events.data?.items ?? []}
        active={deploymentActive}
        loading={events.isLoading}
        error={events.isError}
      />

      <ServiceLogPanel deploymentId={item.id} />

      <DeploymentResult deployment={item} />

      {item.failureCode === 'RECOVERY_STATE_UNCERTAIN' ? (
        <RuntimeReconciliationPanel deployment={item} projectId={item.projectId} />
      ) : null}
    </div>
  );
}
