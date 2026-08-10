import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createDeployment } from '@/entities/deployment/api';
import type { Deployment } from '@/entities/deployment/types';
import { listCommits } from '@/entities/project/api';
import type { Project } from '@/entities/project/types';

import { DeployPanel } from './DeployPanel';

vi.mock('@/entities/deployment/api', () => ({
  createDeployment: vi.fn(),
}));

vi.mock('@/entities/project/api', () => ({
  listCommits: vi.fn(),
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
  status: 'QUEUED',
  failureStage: null,
  failureCode: null,
  createdAt: '2026-08-06T03:00:00Z',
  updatedAt: '2026-08-06T03:00:00Z',
  terminalAt: null,
};

function CurrentPath() {
  return <span data-testid="current-path">{useLocation().pathname}</span>;
}

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/projects/${project.id}`]}>
        <Routes>
          <Route
            path="/projects/:projectId"
            element={
              <>
                <DeployPanel project={project} />
                <CurrentPath />
              </>
            }
          />
          <Route path="/deployments/:deploymentId" element={<CurrentPath />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('DeployPanel', () => {
  beforeEach(() => {
    vi.mocked(listCommits).mockResolvedValue({ items: [] });
    vi.mocked(createDeployment).mockResolvedValue(deployment);
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('moves directly to the accepted deployment dashboard', async () => {
    const user = userEvent.setup();
    renderPanel();

    await user.click(await screen.findByRole('button', { name: '새 preview 배포' }));

    expect(createDeployment).toHaveBeenCalledWith(project.id, { type: 'MAIN_HEAD' });
    expect(await screen.findByTestId('current-path')).toHaveTextContent(
      `/deployments/${deployment.id}`,
    );
  });
});
