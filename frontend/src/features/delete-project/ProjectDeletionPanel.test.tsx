import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { deleteProject, getProjectDeletion, retryProjectDeletion } from '@/entities/project/api';
import type { Project, ProjectDeletion } from '@/entities/project/types';
import { ApiError } from '@/shared/api/client';

import { ProjectDeletionPanel } from './ProjectDeletionPanel';

vi.mock('@/entities/project/api', () => ({
  deleteProject: vi.fn(),
  getProjectDeletion: vi.fn(),
  retryProjectDeletion: vi.fn(),
}));

const projectId = 'f3efae34-9544-48f1-b635-c84ca09d95e7';
const project: Project = {
  id: projectId,
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

const pending: ProjectDeletion = {
  projectId,
  state: 'PENDING',
  phase: 'WAITING_FOR_OPERATIONS',
  attempts: 1,
  availableAt: '2026-08-29T00:00:00Z',
  lastErrorCode: null,
  lastErrorRetryable: null,
  deleteManagedDatabase: false,
  createdAt: '2026-08-29T00:00:00Z',
  updatedAt: '2026-08-29T00:00:00Z',
};

function renderPanel(currentProject: Project = project) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const router = createMemoryRouter(
    [
      {
        path: '/projects/:projectId/settings',
        element: <ProjectDeletionPanel project={currentProject} />,
      },
      { path: '/projects', element: <div>프로젝트 목록</div> },
    ],
    { initialEntries: [`/projects/${projectId}/settings`] },
  );
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { queryClient, router };
}

describe('ProjectDeletionPanel', () => {
  beforeEach(() => {
    vi.mocked(deleteProject).mockResolvedValue(pending);
    vi.mocked(getProjectDeletion).mockResolvedValue(pending);
    vi.mocked(retryProjectDeletion).mockResolvedValue(pending);
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('requires the full project UUID before requesting deletion', async () => {
    const user = userEvent.setup();
    renderPanel();
    const button = await screen.findByRole('button', { name: '프로젝트 영구 삭제' });

    expect(button).toBeDisabled();
    await user.type(screen.getByRole('textbox', { name: '삭제 확인 Project UUID' }), projectId);
    expect(button).toBeEnabled();
    await user.click(button);

    expect(deleteProject).toHaveBeenCalledWith(projectId, {
      confirmation: projectId,
      deleteManagedDatabase: false,
      managedDatabaseConfirmation: null,
    });
  });

  it('requires a separate exact application-data confirmation for an existing database resource', async () => {
    const user = userEvent.setup();
    renderPanel({ ...project, hasManagedDatabase: true });
    await user.type(
      await screen.findByRole('textbox', { name: '삭제 확인 Project UUID' }),
      projectId,
    );
    const button = screen.getByRole('button', { name: '프로젝트와 application data 영구 삭제' });

    expect(button).toBeDisabled();
    await user.type(
      screen.getByRole('textbox', { name: 'Managed DB application data 삭제 확인' }),
      `DELETE ${projectId} APPLICATION DATA`,
    );
    expect(button).toBeEnabled();
    await user.click(button);

    expect(deleteProject).toHaveBeenCalledWith(projectId, {
      confirmation: projectId,
      deleteManagedDatabase: true,
      managedDatabaseConfirmation: `DELETE ${projectId} APPLICATION DATA`,
    });
  });

  it('does not depend on the database endpoint for a non-DB project', async () => {
    const user = userEvent.setup();
    renderPanel();
    await user.type(screen.getByRole('textbox', { name: '삭제 확인 Project UUID' }), projectId);

    expect(screen.getByRole('button', { name: '프로젝트 영구 삭제' })).toBeEnabled();
  });

  it('shows deletion progress and retryable failures through the dedicated retry endpoint', async () => {
    const failed: ProjectDeletion = {
      ...pending,
      state: 'FAILED',
      phase: 'RUNTIME_CLEANUP',
      lastErrorCode: 'PROJECT_RESOURCES_UNCERTAIN',
      lastErrorRetryable: true,
    };
    vi.mocked(getProjectDeletion).mockResolvedValue(failed);
    const user = userEvent.setup();
    renderPanel({ ...project, status: 'DELETING' });

    expect(await screen.findByText('RUNTIME CLEANUP')).toBeVisible();
    expect(screen.getByText('PROJECT_RESOURCES_UNCERTAIN')).toBeVisible();
    expect(screen.getByText('재시도 가능')).toBeVisible();
    const retry = screen.getByRole('button', { name: '삭제 다시 시도' });
    expect(retry).toBeDisabled();
    await user.type(screen.getByRole('textbox', { name: '삭제 확인 Project UUID' }), projectId);
    await user.click(retry);

    expect(retryProjectDeletion).toHaveBeenCalledWith(projectId, {
      confirmation: projectId,
      deleteManagedDatabase: false,
      managedDatabaseConfirmation: null,
    });
  });

  it('clears project-scoped protected caches and replace-navigates after deletion 404', async () => {
    vi.mocked(getProjectDeletion).mockRejectedValue(
      new ApiError(404, 'PROJECT_NOT_FOUND', 'Project was not found'),
    );
    const { queryClient, router } = renderPanel({ ...project, status: 'DELETING' });
    queryClient.setQueryData(['projects', projectId, 'runtime'], { status: 'ACTIVE' });
    queryClient.setQueryData(['projects', projectId, 'deployments'], {
      items: [{ id: 'deployment-1' }],
    });
    queryClient.setQueryData(['deployments', 'deployment-1'], { id: 'deployment-1' });
    queryClient.setQueryData(['deployments', 'other-deployment'], {
      id: 'other-deployment',
      projectId: 'other-project',
    });

    expect(await screen.findByText('프로젝트 목록')).toBeVisible();
    expect(queryClient.getQueryData(['projects', projectId, 'runtime'])).toBeUndefined();
    expect(queryClient.getQueryData(['projects', projectId, 'deployments'])).toBeUndefined();
    expect(queryClient.getQueryData(['deployments', 'deployment-1'])).toBeUndefined();
    expect(queryClient.getQueryData(['deployments', 'other-deployment'])).toEqual({
      id: 'other-deployment',
      projectId: 'other-project',
    });
    await router.navigate(-1);
    await waitFor(() => expect(router.state.location.pathname).toBe('/projects'));
  });

  it('does not treat a missing deletion job as completed project deletion', async () => {
    vi.mocked(getProjectDeletion).mockRejectedValue(
      new ApiError(404, 'PROJECT_DELETION_NOT_FOUND', 'Deletion job was not found'),
    );
    const { queryClient, router } = renderPanel({ ...project, status: 'DELETING' });
    queryClient.setQueryData(['projects', projectId, 'runtime'], { status: 'ACTIVE' });

    expect(await screen.findByText('Deletion job was not found')).toBeVisible();
    expect(router.state.location.pathname).toBe(`/projects/${projectId}/settings`);
    expect(queryClient.getQueryData(['projects', projectId, 'runtime'])).toEqual({
      status: 'ACTIVE',
    });
  });
});
