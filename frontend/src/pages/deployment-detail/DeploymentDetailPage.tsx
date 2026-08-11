import { useQuery } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { Link, useParams } from 'react-router';

import { deploymentQuery, deploymentServiceLogsQuery } from '@/entities/deployment/queries';
import { subscribeServiceLogs } from '@/entities/deployment/api';
import {
  deploymentStatusLabels as statusLabels,
  deploymentStatusTone,
  isDeploymentTerminal,
} from '@/entities/deployment/presentation';
import type {
  Deployment,
  DeploymentEvent,
  DeploymentStatus,
  ServiceLogStreamLine,
} from '@/entities/deployment/types';
import type { DeploymentEventConnection } from '@/entities/deployment/useDeploymentEvents';
import { useDeploymentEvents } from '@/entities/deployment/useDeploymentEvents';
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

const connectionLabels: Record<DeploymentEventConnection, string> = {
  CONNECTING: '연결 중',
  LIVE: '실시간',
  RECONNECTING: '재연결 중',
  COMPLETE: '기록 완료',
  ERROR: '연결 오류',
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
  connection,
  loading,
  error,
}: {
  items: DeploymentEvent[];
  active: boolean;
  connection: DeploymentEventConnection;
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
            <i /> {connectionLabels[connection]}
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
  RUNTIME_LOG_STREAM_BUSY:
    '실시간 로그 연결 한도에 도달했습니다. 잠시 뒤 다시 연결하거나 새로고침을 사용해주세요.',
  RUNTIME_LOG_STREAM_UNAVAILABLE:
    'Worker 실시간 로그 연결을 사용할 수 없습니다. 자동 재연결을 기다리거나 새로고침을 사용해주세요.',
};

type ServiceLogConnection = 'CONNECTING' | 'LIVE' | 'RECONNECTING' | 'ENDED' | 'ERROR';

const serviceLogConnectionLabels: Record<ServiceLogConnection, string> = {
  CONNECTING: '연결 중',
  LIVE: '실시간 연결',
  RECONNECTING: '재연결 중',
  ENDED: '스트림 종료',
  ERROR: '연결 오류',
};

interface LiveServiceLogs {
  status: ServiceLogConnection;
  services: string[];
  serviceName: string;
  connectedAt: string | null;
  lines: ServiceLogStreamLine[];
  truncated: boolean;
  errorCode: string | null;
}

function ServiceLogPanel({ deploymentId }: { deploymentId: string }) {
  const [serviceName, setServiceName] = useState<string>();
  const [live, setLive] = useState<LiveServiceLogs>({
    status: 'CONNECTING',
    services: [],
    serviceName: '',
    connectedAt: null,
    lines: [],
    truncated: false,
    errorCode: null,
  });
  const [autoScroll, setAutoScroll] = useState(true);
  const [pendingLines, setPendingLines] = useState(0);
  const autoScrollRef = useRef(true);
  const outputRef = useRef<HTMLDivElement>(null);
  const snapshot = useQuery({
    ...deploymentServiceLogsQuery(deploymentId, serviceName),
    enabled: false,
  });

  useEffect(() => {
    return subscribeServiceLogs(deploymentId, serviceName, {
      onOpen: () =>
        setLive((current) =>
          current.status === 'RECONNECTING' ? current : { ...current, status: 'CONNECTING' },
        ),
      onReady: (event) => {
        setAutoScrollMode(true);
        setLive({
          status: 'LIVE',
          services: event.services,
          serviceName: event.serviceName,
          connectedAt: event.connectedAt,
          lines: [],
          truncated: false,
          errorCode: null,
        });
      },
      onLine: (event) => {
        if (!autoScrollRef.current) setPendingLines((current) => current + 1);
        setLive((current) => {
          const appended = [...current.lines, event];
          const overflow = appended.length > 200;
          return {
            ...current,
            status: 'LIVE',
            lines: overflow ? appended.slice(-200) : appended,
            truncated: current.truncated || event.truncated || overflow,
          };
        });
      },
      onEnd: () => setLive((current) => ({ ...current, status: 'ENDED' })),
      onStreamError: (code) =>
        setLive((current) => ({ ...current, status: 'ERROR', errorCode: code })),
      onConnectionError: () =>
        setLive((current) =>
          current.status === 'ERROR' || current.status === 'ENDED'
            ? current
            : { ...current, status: 'RECONNECTING' },
        ),
    });
  }, [deploymentId, serviceName]);

  useEffect(() => {
    if (!autoScrollRef.current) return;
    const output = outputRef.current;
    if (output) output.scrollTop = output.scrollHeight;
  }, [live.lines.length]);

  const services = live.services;
  const selectedService = serviceName ?? live.serviceName ?? services[0] ?? '';
  const snapshotErrorCode =
    snapshot.error instanceof ApiError
      ? snapshot.error.code
      : snapshot.isError
        ? 'REQUEST_FAILED'
        : null;
  const errorCode = live.errorCode ?? snapshotErrorCode;
  const errorMessage = errorCode
    ? (serviceLogErrors[errorCode] ?? '서비스 로그를 불러오지 못했습니다.')
    : null;

  function setAutoScrollMode(enabled: boolean) {
    autoScrollRef.current = enabled;
    setAutoScroll(enabled);
    if (enabled) setPendingLines(0);
  }

  function moveToLatest() {
    setAutoScrollMode(true);
    const output = outputRef.current;
    if (output) output.scrollTop = output.scrollHeight;
  }

  function handleLogScroll() {
    const output = outputRef.current;
    if (!output) return;
    const atBottom = output.scrollHeight - output.scrollTop - output.clientHeight <= 24;
    if (atBottom && !autoScrollRef.current) setAutoScrollMode(true);
    if (!atBottom && autoScrollRef.current) setAutoScrollMode(false);
  }

  async function refreshSnapshot() {
    const result = await snapshot.refetch();
    if (!result.data) return;
    setAutoScrollMode(true);
    setLive((current) => ({
      ...current,
      services: result.data.services,
      serviceName: result.data.serviceName,
      connectedAt: result.data.retrievedAt,
      lines: result.data.lines.map((line) => ({ ...line, truncated: false })),
      truncated: result.data.truncated,
      errorCode: null,
    }));
  }

  return (
    <section className="panel service-log-panel">
      <div className="panel-heading service-log-heading">
        <div>
          <span className="eyebrow">Application output</span>
          <h2>서비스 로그</h2>
          <p>선택한 컨테이너의 최근 200줄과 새 stdout·stderr 출력을 실시간으로 보여줍니다.</p>
        </div>
        <div className="service-log-controls">
          <label>
            <span>Service</span>
            <select
              aria-label="로그 서비스 선택"
              value={selectedService}
              disabled={services.length === 0}
              onChange={(event) => {
                const nextService = event.target.value;
                setAutoScrollMode(true);
                setLive((current) => ({
                  ...current,
                  status: 'CONNECTING',
                  serviceName: nextService,
                  lines: [],
                  truncated: false,
                  errorCode: null,
                }));
                setServiceName(nextService);
              }}
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
            disabled={snapshot.isFetching}
            onClick={() => void refreshSnapshot()}
          >
            <Icon name="refresh" /> {snapshot.isFetching ? '조회 중' : '새로고침'}
          </button>
        </div>
      </div>

      <div className="service-log-notice" role="note">
        <Icon name="shield" />
        <p>
          Heimdall이 아는 secret은 마스킹하지만 애플리케이션 로그에는 알 수 없는 개인정보나 인증
          정보가 포함될 수 있습니다. 이 실시간 로그와 snapshot은 저장하지 않습니다.
        </p>
      </div>

      <div className="service-log-meta">
        <span>
          <span className={`service-log-connection ${live.status.toLowerCase()}`}>
            {serviceLogConnectionLabels[live.status]}
          </span>
          {live.serviceName ? (
            <>
              {' '}
              <strong>{live.serviceName}</strong> · {live.lines.length} lines
              {live.truncated ? ' · 일부 생략됨' : ''}
            </>
          ) : null}
        </span>
        {live.connectedAt ? (
          <time dateTime={live.connectedAt}>기준 {formatDate(live.connectedAt)}</time>
        ) : null}
        <div className="service-log-follow-controls">
          <button
            type="button"
            className="button secondary"
            onClick={() => (autoScroll ? setAutoScrollMode(false) : moveToLatest())}
          >
            {autoScroll ? '자동 스크롤 일시정지' : '자동 스크롤 계속'}
          </button>
          {!autoScroll && pendingLines > 0 ? (
            <button type="button" className="button primary" onClick={moveToLatest}>
              최신 로그 {pendingLines}개
            </button>
          ) : null}
        </div>
      </div>

      {errorCode && errorMessage ? (
        <div className="service-log-inline-error" role="alert">
          <strong>{errorMessage}</strong>
          <small>{errorCode}</small>
        </div>
      ) : null}

      <div
        ref={outputRef}
        className="service-log-output"
        role="log"
        aria-label="서비스 컨테이너 로그"
        aria-live="off"
        aria-busy={live.status === 'CONNECTING' || snapshot.isFetching}
        onScroll={handleLogScroll}
      >
        {live.status === 'CONNECTING' && live.lines.length === 0 ? (
          <p className="service-log-empty">실시간 서비스 로그에 연결하는 중입니다.</p>
        ) : null}
        {live.status === 'RECONNECTING' && live.lines.length === 0 ? (
          <p className="service-log-empty">연결이 끊겨 자동으로 다시 연결하는 중입니다.</p>
        ) : null}
        {live.status === 'ENDED' && live.lines.length === 0 ? (
          <p className="service-log-empty">컨테이너 로그 스트림이 종료되었습니다.</p>
        ) : null}
        {live.status === 'ERROR' && live.lines.length === 0 ? (
          <p className="service-log-empty">
            새로고침으로 마지막 snapshot을 다시 조회할 수 있습니다.
          </p>
        ) : null}
        {live.status === 'LIVE' && live.lines.length === 0 ? (
          <p className="service-log-empty">연결되었습니다. 아직 출력된 서비스 로그가 없습니다.</p>
        ) : null}
        {live.lines.length > 0 ? (
          <ol>
            {live.lines.map((line, index) => (
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
  const events = useDeploymentEvents(deploymentId, deploymentActive);

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
        items={events.items}
        active={deploymentActive}
        connection={events.connection}
        loading={events.loading}
        error={events.error}
      />

      <ServiceLogPanel deploymentId={item.id} />

      <DeploymentResult deployment={item} />

      {item.failureCode === 'RECOVERY_STATE_UNCERTAIN' ? (
        <RuntimeReconciliationPanel deployment={item} projectId={item.projectId} />
      ) : null}
    </div>
  );
}
