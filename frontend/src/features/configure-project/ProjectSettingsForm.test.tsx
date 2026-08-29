import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { saveProjectSettings } from '@/entities/project/api';
import type { Project } from '@/entities/project/types';

import { ProjectSettingsForm } from './ProjectSettingsForm';

vi.mock('@/entities/project/api', () => ({
  saveProjectSettings: vi.fn(),
}));

const project: Project = {
  id: 'f3efae34-9544-48f1-b635-c84ca09d95e7',
  name: 'example',
  repositoryUrl: 'https://github.com/example/project',
  branch: 'main',
  status: 'READY',
  configVersion: 3,
  deploymentConfig: {
    services: [
      {
        name: 'web',
        build: { context: '.', dockerfile: 'Dockerfile' },
        internalPort: 3000,
        healthPath: '/health',
        environment: [],
        projectDatabaseAccess: false,
      },
    ],
    routes: [{ path: '/', service: 'web' }],
  },
  createdAt: '2026-08-03T10:00:00Z',
  updatedAt: '2026-08-03T10:00:00Z',
};

function renderForm(currentProject: Project = project) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <ProjectSettingsForm project={currentProject} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('ProjectSettingsForm', () => {
  beforeEach(() => {
    vi.mocked(saveProjectSettings).mockResolvedValue(project);
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('keeps the internal port empty while the user replaces it', async () => {
    const user = userEvent.setup();
    renderForm();

    const port = screen.getByRole('spinbutton', { name: '내부 포트' });
    await user.clear(port);

    expect(port).toHaveValue(null);
  });

  it('updates routes that reference a renamed service before saving', async () => {
    const user = userEvent.setup();
    renderForm();

    const serviceName = screen.getByRole('textbox', { name: '서비스 이름' });
    await user.clear(serviceName);
    await user.type(serviceName, 'frontend');
    await user.click(screen.getByRole('button', { name: '설정 저장' }));

    expect(saveProjectSettings).toHaveBeenCalledWith(project.id, {
      expectedVersion: 3,
      services: [
        {
          name: 'frontend',
          build: { context: '.', dockerfile: 'Dockerfile' },
          internalPort: 3000,
          healthPath: '/health',
          environment: [],
          projectDatabaseAccess: false,
        },
      ],
      routes: [{ path: '/', service: 'frontend' }],
    });
  });

  it('does not capture another service route while typing through its name', async () => {
    const user = userEvent.setup();
    const projectWithApi: Project = {
      ...project,
      deploymentConfig: {
        services: [
          ...project.deploymentConfig!.services,
          {
            name: 'api',
            build: { context: 'backend', dockerfile: 'Dockerfile' },
            internalPort: 8000,
            healthPath: '/health',
            environment: [],
            projectDatabaseAccess: false,
          },
        ],
        routes: [
          { path: '/', service: 'web' },
          { path: '/api', service: 'api' },
        ],
      },
    };
    renderForm(projectWithApi);

    const [serviceName] = screen.getAllByRole('textbox', { name: '서비스 이름' });
    await user.clear(serviceName);
    await user.type(serviceName, 'api-v2');
    await user.click(screen.getByRole('button', { name: '설정 저장' }));

    expect(saveProjectSettings).toHaveBeenCalledWith(
      project.id,
      expect.objectContaining({
        routes: [
          { path: '/', service: 'api-v2' },
          { path: '/api', service: 'api' },
        ],
      }),
    );
  });
});
