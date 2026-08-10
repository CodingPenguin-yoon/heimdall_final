import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getDeployment, listDeploymentEvents } from '@/entities/deployment/api';
import type { Deployment, DeploymentEvent } from '@/entities/deployment/types';

import { DeploymentDetailPage } from './DeploymentDetailPage';

vi.mock('@/entities/deployment/api', () => ({
  getDeployment: vi.fn(),
  listDeploymentEvents: vi.fn(),
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
  beforeEach(() => {
    vi.mocked(getDeployment).mockResolvedValue(deployment);
    vi.mocked(listDeploymentEvents).mockResolvedValue({ items: events });
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
});
