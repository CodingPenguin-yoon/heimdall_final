import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  listDeploymentEvents,
  listDeployments,
  subscribeDeploymentEvents,
} from '@/entities/deployment/api';
import type { Deployment } from '@/entities/deployment/types';
import { getProject } from '@/entities/project/api';
import type { Project } from '@/entities/project/types';
import { getPublicRoute } from '@/entities/public-route/api';
import { getProjectRuntime } from '@/entities/runtime/api';

import { ProjectDetailPage } from './ProjectDetailPage';

vi.mock('@/entities/deployment/api', () => ({
  listDeployments: vi.fn(),
  listDeploymentEvents: vi.fn(),
  subscribeDeploymentEvents: vi.fn(),
}));

vi.mock('@/entities/project/api', () => ({
  getProject: vi.fn(),
}));

vi.mock('@/entities/public-route/api', () => ({
  getPublicRoute: vi.fn(),
  savePublicRoute: vi.fn(),
  disablePublicRoute: vi.fn(),
}));

vi.mock('@/entities/runtime/api', () => ({
  getProjectRuntime: vi.fn(),
}));

vi.mock('@/features/deploy-project/DeployPanel', () => ({
  DeployPanel: () => null,
}));

vi.mock('@/features/reconcile-runtime/RuntimeReconciliationPanel', () => ({
  RuntimeReconciliationPanel: () => null,
}));

const project: Project = {
  id: 'f3efae34-9544-48f1-b635-c84ca09d95e7',
  name: 'heimdall',
  repositoryUrl: 'https://github.com/CodingPenguin-yoon/heimdall',
  branch: 'main',
  status: 'READY',
  configVersion: 1,
  deploymentConfig: {
    services: [
      {
        name: 'web',
        internalPort: 8080,
        build: { context: '.', dockerfile: 'Dockerfile' },
        healthPath: '/health',
        projectDatabaseAccess: false,
        environment: [],
      },
    ],
    routes: [{ path: '/', service: 'web' }],
  },
  createdAt: '2026-08-06T03:00:00Z',
  updatedAt: '2026-08-06T03:00:00Z',
};

const deployment: Deployment = {
  id: '8a7d1b1a-1df0-4a75-af80-2cbc60b734b9',
  projectId: project.id,
  sourceType: 'MAIN_HEAD',
  requestedCommitSha: null,
  resolvedCommitSha: 'a'.repeat(40),
  configVersion: 1,
  status: 'SUCCEEDED',
  failureStage: null,
  failureCode: null,
  createdAt: '2026-08-06T03:00:00Z',
  updatedAt: '2026-08-06T03:02:00Z',
  terminalAt: '2026-08-06T03:02:00Z',
};

describe('ProjectDetailPage deployment history', () => {
  beforeEach(() => {
    vi.mocked(getProject).mockResolvedValue(project);
    vi.mocked(listDeployments).mockResolvedValue({ items: [deployment] });
    vi.mocked(listDeploymentEvents).mockResolvedValue({ items: [] });
    vi.mocked(subscribeDeploymentEvents).mockReturnValue(vi.fn());
    vi.mocked(getPublicRoute).mockResolvedValue(null);
    vi.mocked(getProjectRuntime).mockResolvedValue({
      status: 'NOT_ACTIVE',
      previewPort: null,
      activeDeploymentId: null,
      updatedAt: null,
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('links every history row to its deployment detail', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[`/projects/${project.id}`]}>
          <Routes>
            <Route path="/projects/:projectId" element={<ProjectDetailPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole('link', { name: 'aaaaaaaa 배포 상세 열기' })).toHaveAttribute(
      'href',
      `/deployments/${deployment.id}`,
    );
  });
});
