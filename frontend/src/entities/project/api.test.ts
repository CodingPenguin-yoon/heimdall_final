import { afterEach, describe, expect, it, vi } from 'vitest';

import { setCsrfToken } from '@/shared/api/client';

import { deleteProject, getProjectDeletion, retryProjectDeletion } from './api';
import type { ProjectDeletion, ProjectDeletionRequest } from './types';

const request: ProjectDeletionRequest = {
  confirmation: 'f3efae34-9544-48f1-b635-c84ca09d95e7',
  deleteManagedDatabase: true,
  managedDatabaseConfirmation: 'DELETE f3efae34-9544-48f1-b635-c84ca09d95e7 APPLICATION DATA',
};

const deletion: ProjectDeletion = {
  projectId: request.confirmation,
  state: 'PENDING',
  phase: 'REQUESTED',
  attempts: 0,
  availableAt: '2026-08-29T00:00:00Z',
  lastErrorCode: null,
  lastErrorRetryable: null,
  deleteManagedDatabase: true,
  createdAt: '2026-08-29T00:00:00Z',
  updatedAt: '2026-08-29T00:00:00Z',
};

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  setCsrfToken(null);
  vi.unstubAllGlobals();
});

describe('project deletion API', () => {
  it('sends full destructive confirmation with CSRF on delete', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(deletion));
    vi.stubGlobal('fetch', fetchMock);
    setCsrfToken('session-csrf');

    await expect(deleteProject(request.confirmation, request)).resolves.toEqual(deletion);

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`/api/projects/${request.confirmation}`);
    expect(init.method).toBe('DELETE');
    expect(init.body).toBe(JSON.stringify(request));
    expect(new Headers(init.headers).get('X-CSRF-Token')).toBe('session-csrf');
  });

  it('reads deletion progress and retries through dedicated endpoints', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(deletion))
      .mockResolvedValueOnce(jsonResponse({ ...deletion, state: 'PENDING', attempts: 2 }));
    vi.stubGlobal('fetch', fetchMock);

    await getProjectDeletion(request.confirmation);
    await retryProjectDeletion(request.confirmation, request);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      `/api/projects/${request.confirmation}/deletion`,
      expect.not.objectContaining({ method: expect.anything() }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      `/api/projects/${request.confirmation}/deletion/retry`,
      expect.objectContaining({ method: 'POST', body: JSON.stringify(request) }),
    );
  });
});
