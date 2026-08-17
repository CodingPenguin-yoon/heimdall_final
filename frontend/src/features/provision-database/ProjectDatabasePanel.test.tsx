import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { getProjectDatabase, provisionProjectDatabase } from '@/entities/database/api';
import type { ProjectDatabase } from '@/entities/database/types';

import { ProjectDatabasePanel } from './ProjectDatabasePanel';

vi.mock('@/entities/database/api', () => ({
  getProjectDatabase: vi.fn(),
  provisionProjectDatabase: vi.fn(),
}));

const notCreated: ProjectDatabase = {
  required: true,
  status: 'NOT_CREATED',
  id: null,
  phase: null,
  databaseName: null,
  username: null,
  schemaName: null,
  host: null,
  port: null,
  connectedServices: ['api'],
  failureStage: null,
  failureCode: null,
  updatedAt: null,
};

describe('ProjectDatabasePanel', () => {
  beforeEach(() => {
    vi.mocked(getProjectDatabase).mockResolvedValue(notCreated);
    vi.mocked(provisionProjectDatabase).mockResolvedValue({
      ...notCreated,
      status: 'ACTIVE',
      id: 'd7b86499-b5a6-485d-9daa-58a2d3354910',
      phase: 'ACTIVE',
      databaseName: 'hd_db_d7b86499b5a6485d9daa58a2d3354910',
      username: 'hd_role_d7b86499b5a6485d9daa58a2d3354910',
      schemaName: 'app',
      host: 'managed-db.internal',
      port: 5432,
      updatedAt: '2026-08-03T10:00:00Z',
    });
  });

  it('provisions a database and shows non-secret connection metadata', async () => {
    const user = userEvent.setup();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <ProjectDatabasePanel projectId="project-1" />
      </QueryClientProvider>,
    );

    await user.click(await screen.findByRole('button', { name: 'Database 생성' }));

    expect(await screen.findByText(/managed-db\.internal/)).toBeInTheDocument();
    expect(screen.getByText('hd_db_d7b86499b5a6485d9daa58a2d3354910')).toBeInTheDocument();
    expect(screen.getByText(/DATABASE_PASSWORD_FILE/)).toBeInTheDocument();
  });
});
