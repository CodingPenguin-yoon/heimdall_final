import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { listRecentDeployments } from '@/entities/deployment/api';
import type { Deployment } from '@/entities/deployment/types';
import { listProjects } from '@/entities/project/api';
import type { Project } from '@/entities/project/types';

import { DeploymentActivityPage } from './DeploymentActivityPage';

const longProjectName = 'Runtime-cbe9db37889e479fa5106c431785e740';

vi.mock('@/entities/deployment/api', () => ({
  listRecentDeployments: vi.fn(),
}));

vi.mock('@/entities/project/api', () => ({
  listProjects: vi.fn(),
}));

const projects: Project[] = [
  {
    id: '11111111-1111-4111-8111-111111111111',
    name: 'Console',
    repositoryUrl: 'https://github.com/example/console',
    branch: 'main',
    status: 'READY',
    configVersion: 2,
    deploymentConfig: null,
    createdAt: '2026-08-06T03:00:00Z',
    updatedAt: '2026-08-06T03:00:00Z',
  },
  {
    id: '22222222-2222-4222-8222-222222222222',
    name: longProjectName,
    repositoryUrl: 'https://github.com/example/gateway',
    branch: 'main',
    status: 'READY',
    configVersion: 1,
    deploymentConfig: null,
    createdAt: '2026-08-06T04:00:00Z',
    updatedAt: '2026-08-06T04:00:00Z',
  },
];

const deployments: Deployment[] = [
  {
    id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    projectId: projects[1].id,
    sourceType: 'MAIN_HEAD',
    requestedCommitSha: null,
    resolvedCommitSha: 'b'.repeat(40),
    configVersion: 1,
    status: 'FAILED',
    failureStage: 'ACTIVATION',
    failureCode: 'GATEWAY_ROUTE_PROBE_FAILED',
    createdAt: '2026-08-06T05:00:00Z',
    updatedAt: '2026-08-06T05:02:00Z',
    terminalAt: '2026-08-06T05:02:00Z',
  },
  {
    id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
    projectId: projects[0].id,
    sourceType: 'MAIN_COMMIT',
    requestedCommitSha: 'a'.repeat(40),
    resolvedCommitSha: 'a'.repeat(40),
    configVersion: 2,
    status: 'SUCCEEDED',
    failureStage: null,
    failureCode: null,
    createdAt: '2026-08-06T04:00:00Z',
    updatedAt: '2026-08-06T04:03:00Z',
    terminalAt: '2026-08-06T04:03:00Z',
  },
];

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/deployments']}>
        <DeploymentActivityPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('DeploymentActivityPage', () => {
  beforeEach(() => {
    vi.mocked(listRecentDeployments).mockResolvedValue({ items: deployments });
    vi.mocked(listProjects).mockResolvedValue({ items: projects });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('shows project context and links each deployment to its existing detail page', async () => {
    renderPage();

    expect(await screen.findByRole('heading', { name: '배포 활동' })).toBeInTheDocument();
    const table = await screen.findByRole('table');
    expect(within(table).getByText(longProjectName)).toBeInTheDocument();
    expect(within(table).getByText('Console')).toBeInTheDocument();
    const failedRow = within(table).getByText(longProjectName).closest('tr');
    expect(within(failedRow!).getByText(longProjectName).closest('a')).toHaveAttribute(
      'title',
      longProjectName,
    );
    expect(within(failedRow!).getByText('b'.repeat(8))).toHaveAttribute(
      'title',
      deployments[0].resolvedCommitSha,
    );
    expect(within(failedRow!).getByText(deployments[0].id.slice(0, 8))).toHaveAttribute(
      'title',
      deployments[0].id,
    );
    expect(within(failedRow!).getByText('GATEWAY_ROUTE_PROBE_FAILED')).toHaveAttribute(
      'title',
      'GATEWAY_ROUTE_PROBE_FAILED',
    );
    expect(screen.getByRole('link', { name: 'bbbbbbbb 배포 상세 보기' })).toHaveAttribute(
      'href',
      `/deployments/${deployments[0].id}`,
    );
    expect(within(table).getAllByText('상세 보기')).toHaveLength(deployments.length);
  });

  it('filters the recent activity by status and project', async () => {
    renderPage();
    const table = await screen.findByRole('table');

    fireEvent.click(screen.getByRole('button', { name: '성공' }));
    expect(within(table).getByText('Console')).toBeInTheDocument();
    expect(within(table).queryByText(longProjectName)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '전체' }));
    fireEvent.change(screen.getByRole('combobox', { name: '프로젝트 필터' }), {
      target: { value: projects[1].id },
    });
    expect(within(table).getByText(longProjectName)).toBeInTheDocument();
    expect(within(table).queryByText('Console')).not.toBeInTheDocument();
  });
});
