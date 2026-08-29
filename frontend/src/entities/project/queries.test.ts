import { describe, expect, it } from 'vitest';

import { projectDeletionQuery } from './queries';
import type { ProjectDeletion, ProjectDeletionState } from './types';

const deletion: ProjectDeletion = {
  projectId: 'project-1',
  state: 'PENDING',
  phase: 'REQUESTED',
  attempts: 0,
  availableAt: '2026-08-29T00:00:00Z',
  lastErrorCode: null,
  lastErrorRetryable: null,
  deleteManagedDatabase: false,
  createdAt: '2026-08-29T00:00:00Z',
  updatedAt: '2026-08-29T00:00:00Z',
};

describe('projectDeletionQuery', () => {
  it('polls active deletion jobs and stops after a durable failure', () => {
    const interval = projectDeletionQuery('project-1', true).refetchInterval as unknown as (query: {
      state: { data: ProjectDeletion | undefined };
    }) => number | false;
    const value = (state: ProjectDeletionState) =>
      interval({ state: { data: { ...deletion, state } } });

    expect(value('PENDING')).toBe(1_000);
    expect(value('CLAIMED')).toBe(1_000);
    expect(value('FAILED')).toBe(false);
    expect(interval({ state: { data: undefined } })).toBe(false);
  });
});
