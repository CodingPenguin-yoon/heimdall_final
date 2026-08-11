import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  getDeployment,
  getServiceLogs,
  listDeploymentEvents,
  subscribeDeploymentEvents,
  subscribeServiceLogs,
} from '@/entities/deployment/api';
import type {
  DeploymentEventStreamHandlers,
  ServiceLogStreamHandlers,
} from '@/entities/deployment/api';
import type { Deployment, DeploymentEvent, ServiceLogSnapshot } from '@/entities/deployment/types';
import { ApiError } from '@/shared/api/client';

import { DeploymentDetailPage } from './DeploymentDetailPage';

vi.mock('@/entities/deployment/api', () => ({
  getDeployment: vi.fn(),
  getServiceLogs: vi.fn(),
  listDeploymentEvents: vi.fn(),
  subscribeDeploymentEvents: vi.fn(),
  subscribeServiceLogs: vi.fn(),
}));

const deployment: Deployment = {
  id: '8a7d1b1a-1df0-4a75-af80-2cbc60b734b9',
  projectId: 'f3efae34-9544-48f1-b635-c84ca09d95e7',
  sourceType: 'MAIN_COMMIT',
  requestedCommitSha: 'a'.repeat(40),
  resolvedCommitSha: 'a'.repeat(40),
  configVersion: 3,
  status: 'BUILDING',
  failureStage: null,
  failureCode: null,
  createdAt: '2026-08-06T03:00:00Z',
  updatedAt: '2026-08-06T03:01:00Z',
  terminalAt: null,
};

const events: DeploymentEvent[] = [
  {
    id: 1,
    deploymentId: deployment.id,
    stage: 'PREPARING',
    code: 'JOB_CLAIMED',
    message: 'Worker claimed the deployment job',
    createdAt: '2026-08-06T03:00:05Z',
  },
  {
    id: 2,
    deploymentId: deployment.id,
    stage: 'BUILDING',
    code: 'IMAGES_BUILDING',
    message: 'Building service images from the selected commit',
    createdAt: '2026-08-06T03:01:00Z',
  },
];

const longLogLine = `request completed ${'x'.repeat(500)}`;
const serviceLogs: ServiceLogSnapshot = {
  deploymentId: deployment.id,
  services: ['web', 'api'],
  serviceName: 'web',
  retrievedAt: '2026-08-06T03:01:30Z',
  lines: [
    {
      timestamp: '2026-08-06T03:01:20.000000000Z',
      stream: 'STDOUT',
      message: longLogLine,
    },
    {
      timestamp: '2026-08-06T03:01:21.000000000Z',
      stream: 'STDERR',
      message: 'upstream retry',
    },
  ],
  truncated: false,
};

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/deployments/${deployment.id}`]}>
        <Routes>
          <Route path="/deployments/:deploymentId" element={<DeploymentDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('DeploymentDetailPage', () => {
  let latestStreamHandlers: ServiceLogStreamHandlers | null;
  let latestEventStreamHandlers: DeploymentEventStreamHandlers | null;

  beforeEach(() => {
    latestStreamHandlers = null;
    latestEventStreamHandlers = null;
    vi.mocked(getDeployment).mockResolvedValue(deployment);
    vi.mocked(listDeploymentEvents).mockResolvedValue({ items: events });
    vi.mocked(getServiceLogs).mockResolvedValue(serviceLogs);
    vi.mocked(subscribeDeploymentEvents).mockImplementation((_, __, handlers) => {
      latestEventStreamHandlers = handlers;
      handlers.onOpen();
      handlers.onReady();
      return vi.fn();
    });
    vi.mocked(subscribeServiceLogs).mockImplementation((_, requestedService, handlers) => {
      latestStreamHandlers = handlers;
      const selectedService = requestedService ?? 'web';
      handlers.onOpen();
      handlers.onReady({
        deploymentId: deployment.id,
        services: ['web', 'api'],
        serviceName: selectedService,
        connectedAt: '2026-08-06T03:01:30Z',
      });
      handlers.onLine({
        timestamp: '2026-08-06T03:01:20.000000000Z',
        stream: 'STDOUT',
        message: selectedService === 'api' ? 'api ready' : longLogLine,
        truncated: false,
      });
      if (selectedService === 'web') {
        handlers.onLine({
          timestamp: '2026-08-06T03:01:21.000000000Z',
          stream: 'STDERR',
          message: 'upstream retry',
          truncated: false,
        });
      }
      return vi.fn();
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('shows the selected source, immutable deployment metadata, and current progress', async () => {
    renderPage();

    expect(await screen.findByRole('heading', { name: '배포 현황' })).toBeInTheDocument();
    expect(screen.getByText('진행 중')).toBeInTheDocument();
    expect(screen.getByText('main 특정 commit')).toBeInTheDocument();
    expect(screen.getByText('Config v3')).toBeInTheDocument();
    expect(screen.getByText('a'.repeat(40))).toBeInTheDocument();
    const progress = screen.getByRole('heading', { name: '배포 단계' }).closest('section');
    expect(within(progress!).getByText('이미지 빌드').closest('li')).toHaveClass('current');
    expect(screen.getByRole('link', { name: '프로젝트로 돌아가기' })).toHaveAttribute(
      'href',
      `/projects/${deployment.projectId}`,
    );
  });

  it('shows structured deployment events as a live chronological log', async () => {
    renderPage();

    expect(await screen.findByRole('heading', { name: '실시간 배포 로그' })).toBeInTheDocument();
    const log = await screen.findByRole('log', { name: '배포 이벤트 로그' });
    const rows = within(log).getAllByRole('listitem');

    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent('JOB_CLAIMED');
    expect(rows[1]).toHaveTextContent('IMAGES_BUILDING');
    expect(rows[1]).toHaveTextContent('Building service images from the selected commit');
    expect(screen.getByText('실시간')).toBeInTheDocument();
    expect(subscribeDeploymentEvents).toHaveBeenCalledWith(deployment.id, 2, expect.any(Object));
  });

  it('appends deployment events delivered by SSE', async () => {
    renderPage();
    await screen.findByText('IMAGES_BUILDING');

    act(() => {
      latestEventStreamHandlers?.onEvent({
        id: 3,
        deploymentId: deployment.id,
        stage: 'STARTING',
        code: 'SERVICES_STARTING',
        message: 'Starting service containers',
        createdAt: '2026-08-06T03:01:10Z',
      });
    });

    const log = screen.getByRole('log', { name: '배포 이벤트 로그' });
    expect(await within(log).findByText('SERVICES_STARTING')).toBeInTheDocument();
    expect(within(log).getAllByRole('listitem')).toHaveLength(3);
  });

  it('shows a stable failure summary on a failed deployment', async () => {
    vi.mocked(getDeployment).mockResolvedValue({
      ...deployment,
      status: 'FAILED',
      failureStage: 'HEALTH',
      failureCode: 'SERVICE_HEALTH_CHECK_FAILED',
      terminalAt: '2026-08-06T03:02:00Z',
    });

    renderPage();

    expect(await screen.findByText('배포 실패')).toBeInTheDocument();
    expect(screen.getByText('HEALTH')).toBeInTheDocument();
    expect(screen.getByText('SERVICE_HEALTH_CHECK_FAILED')).toBeInTheDocument();
    expect(screen.getByText('서비스 상태 확인').closest('li')).toHaveClass('failed');
  });

  it('shows live service logs with stream separation and keeps snapshot refresh as fallback', async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByRole('heading', { name: '서비스 로그' })).toBeInTheDocument();
    const log = await screen.findByRole('log', { name: '서비스 컨테이너 로그' });
    expect(within(log).getByText('STDOUT')).toBeInTheDocument();
    expect(within(log).getByText('STDERR')).toBeInTheDocument();
    expect(within(log).getByText(longLogLine)).toBeInTheDocument();
    expect(screen.getByText('실시간 연결')).toBeInTheDocument();
    expect(screen.getByRole('note')).toHaveTextContent(
      '이 실시간 로그와 snapshot은 저장하지 않습니다',
    );
    expect(getServiceLogs).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: '새로고침' }));
    expect(getServiceLogs).toHaveBeenCalledWith(deployment.id, undefined);
  });

  it('pauses only service-log auto-scroll and exposes newly received lines', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText(longLogLine);

    await user.click(screen.getByRole('button', { name: '자동 스크롤 일시정지' }));
    act(() => {
      latestStreamHandlers?.onLine({
        timestamp: '2026-08-06T03:01:22.000000000Z',
        stream: 'STDOUT',
        message: 'received while paused',
        truncated: false,
      });
    });

    expect(await screen.findByText('received while paused')).toBeInTheDocument();
    const latestButton = screen.getByRole('button', { name: '최신 로그 1개' });
    expect(latestButton).toBeInTheDocument();

    await user.click(latestButton);
    expect(screen.getByRole('button', { name: '자동 스크롤 일시정지' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '최신 로그 1개' })).not.toBeInTheDocument();
  });

  it('pauses auto-scroll when the operator moves away from the latest service log', async () => {
    renderPage();
    const log = await screen.findByRole('log', { name: '서비스 컨테이너 로그' });
    Object.defineProperties(log, {
      scrollHeight: { configurable: true, value: 1_000 },
      clientHeight: { configurable: true, value: 200 },
      scrollTop: { configurable: true, value: 300, writable: true },
    });

    fireEvent.scroll(log);

    expect(screen.getByRole('button', { name: '자동 스크롤 계속' })).toBeInTheDocument();
  });

  it('switches the live stream and refreshes the snapshot only when requested', async () => {
    const user = userEvent.setup();
    vi.mocked(getServiceLogs).mockImplementation(async (_, serviceName) =>
      serviceName === 'api'
        ? {
            ...serviceLogs,
            serviceName: 'api',
            lines: [
              {
                timestamp: '2026-08-06T03:02:00.000000000Z',
                stream: 'STDOUT',
                message: 'api ready',
              },
            ],
          }
        : serviceLogs,
    );
    renderPage();

    await screen.findByRole('option', { name: 'api' });
    const selector = screen.getByRole('combobox', { name: '로그 서비스 선택' });
    await user.selectOptions(selector, 'api');

    expect(await screen.findByText('api ready')).toBeInTheDocument();
    expect(subscribeServiceLogs).toHaveBeenLastCalledWith(deployment.id, 'api', expect.any(Object));
    expect(getServiceLogs).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: '새로고침' }));
    await waitFor(() => expect(getServiceLogs).toHaveBeenCalledWith(deployment.id, 'api'));
  });

  it('explains when logs are withheld because redaction is unavailable', async () => {
    vi.mocked(getServiceLogs).mockRejectedValue(
      new ApiError(503, 'SERVICE_LOG_REDACTION_UNAVAILABLE', 'Service logs were withheld'),
    );
    renderPage();

    await screen.findByText(longLogLine);
    await userEvent.setup().click(screen.getByRole('button', { name: '새로고침' }));

    expect(
      await screen.findByText('민감정보 마스킹을 준비하지 못해 로그 원문을 표시하지 않았습니다.'),
    ).toBeInTheDocument();
    expect(screen.getByText('SERVICE_LOG_REDACTION_UNAVAILABLE')).toBeInTheDocument();
  });

  it('shows reconnecting state and replaces the buffer after the server is ready again', async () => {
    renderPage();

    expect(await screen.findByText(longLogLine)).toBeInTheDocument();
    latestStreamHandlers?.onConnectionError();
    expect(await screen.findByText('재연결 중')).toBeInTheDocument();

    latestStreamHandlers?.onReady({
      deploymentId: deployment.id,
      services: ['web', 'api'],
      serviceName: 'web',
      connectedAt: '2026-08-06T03:02:00Z',
    });
    latestStreamHandlers?.onLine({
      timestamp: '2026-08-06T03:02:01.000000000Z',
      stream: 'STDOUT',
      message: 'reconnected tail',
      truncated: false,
    });

    expect(await screen.findByText('reconnected tail')).toBeInTheDocument();
    expect(screen.queryByText(longLogLine)).not.toBeInTheDocument();
  });
});
