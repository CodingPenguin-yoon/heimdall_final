import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '@/shared/api/client';

import { disablePublicRoute, getPublicRoute, savePublicRoute } from './api';
import type { PublicRoute } from './types';

const route: PublicRoute = {
  projectId: 'project-1',
  subdomain: 'student-a',
  hostname: 'student-a.deploy.example',
  desiredState: 'ENABLED',
  status: 'PENDING',
  desiredRevision: 1,
  appliedRevision: null,
  appliedHostname: null,
  lastErrorCode: null,
  createdAt: '2026-08-21T00:00:00Z',
  updatedAt: '2026-08-21T00:00:00Z',
};

function response(options: {
  ok: boolean;
  status: number;
  body: PublicRoute | { code: string; message: string };
}): Response {
  return {
    ok: options.ok,
    status: options.status,
    json: vi.fn().mockResolvedValue(options.body),
  } as unknown as Response;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('public route API', () => {
  it('maps only the stable not-found response to null', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        response({
          ok: false,
          status: 404,
          body: { code: 'PUBLIC_ROUTE_NOT_FOUND', message: 'Public route was not found' },
        }),
      )
      .mockResolvedValueOnce(
        response({
          ok: false,
          status: 404,
          body: { code: 'PROJECT_NOT_FOUND', message: 'Project was not found' },
        }),
      );
    vi.stubGlobal('fetch', fetchMock);

    await expect(getPublicRoute('project-1')).resolves.toBeNull();
    await expect(getPublicRoute('project-1')).rejects.toEqual(
      new ApiError(404, 'PROJECT_NOT_FOUND', 'Project was not found'),
    );
  });

  it('sends only the subdomain on PUT and expects JSON from DELETE', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(response({ ok: true, status: 200, body: route }))
      .mockResolvedValueOnce(
        response({
          ok: true,
          status: 200,
          body: { ...route, desiredState: 'DISABLED', status: 'PENDING' },
        }),
      );
    vi.stubGlobal('fetch', fetchMock);

    await savePublicRoute('project-1', 'student-a');
    await disablePublicRoute('project-1');

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      '/api/projects/project-1/public-route',
      expect.objectContaining({ method: 'PUT', body: JSON.stringify({ subdomain: 'student-a' }) }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      '/api/projects/project-1/public-route',
      expect.objectContaining({ method: 'DELETE' }),
    );
  });
});
