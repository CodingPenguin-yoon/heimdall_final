import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getProjectDatabase } from '@/entities/database/api';
import { getProject } from '@/entities/project/api';
import type { Project } from '@/entities/project/types';

import { ProjectSettingsPage } from './ProjectSettingsPage';

vi.mock('@/entities/database/api', () => ({ getProjectDatabase: vi.fn() }));
vi.mock('@/entities/project/api', () => ({
  getProject: vi.fn(),
  deleteProject: vi.fn(),
  getProjectDeletion: vi.fn(),
  retryProjectDeletion: vi.fn(),
  saveProjectSettings: vi.fn(),
}));

const project: Project = {
  id: 'f3efae34-9544-48f1-b635-c84ca09d95e7',
  name: 'heimdall',
  repositoryUrl: 'https://github.com/example/heimdall',
  branch: 'main',
  status: 'READY',
  hasManagedDatabase: false,
  configVersion: 1,
  deploymentConfig: null,
  createdAt: '2026-08-29T00:00:00Z',
  updatedAt: '2026-08-29T00:00:00Z',
};

describe('ProjectSettingsPage', () => {
  beforeEach(() => {
    vi.mocked(getProject).mockResolvedValue(project);
    vi.mocked(getProjectDatabase).mockResolvedValue({
      required: false,
      status: 'NOT_CREATED',
      id: null,
      phase: null,
      databaseName: null,
      username: null,
      schemaName: null,
      host: null,
      port: null,
      connectedServices: [],
      failureStage: null,
      failureCode: null,
      updatedAt: null,
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('places project deletion in a settings Danger zone', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[`/projects/${project.id}/settings`]}>
          <Routes>
            <Route path="/projects/:projectId/settings" element={<ProjectSettingsPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole('region', { name: 'Danger zone' })).toBeVisible();
    expect(screen.getByRole('textbox', { name: '삭제 확인 Project UUID' })).toBeVisible();
  });
});
