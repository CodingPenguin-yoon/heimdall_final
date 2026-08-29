import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  registerUnauthorizedHandler,
  requestJson,
  revalidateAuthenticatedSession,
  setCsrfToken,
} from './client';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  setCsrfToken(null);
  vi.unstubAllGlobals();
});

describe('requestJson authentication boundary', () => {
  it('keeps caller Headers while adding same-origin credentials and unsafe-method CSRF', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ saved: true }));
    vi.stubGlobal('fetch', fetchMock);
    setCsrfToken('session-csrf');
    const callerHeaders = new Headers({
      'Content-Type': 'application/problem+json',
      'X-Caller-Header': 'kept',
    });

    await requestJson('/projects', {
      method: 'POST',
      body: '{}',
      headers: callerHeaders,
      credentials: 'omit',
    });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const sentHeaders = new Headers(init.headers);
    expect(url).toBe('/api/projects');
    expect(init.credentials).toBe('same-origin');
    expect(sentHeaders.get('Accept')).toBe('application/json');
    expect(sentHeaders.get('Content-Type')).toBe('application/problem+json');
    expect(sentHeaders.get('X-Caller-Header')).toBe('kept');
    expect(sentHeaders.get('X-CSRF-Token')).toBe('session-csrf');
  });

  it('does not add CSRF to safe methods', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [] }));
    vi.stubGlobal('fetch', fetchMock);
    setCsrfToken('session-csrf');

    await requestJson('/projects');

    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(init.headers).has('X-CSRF-Token')).toBe(false);
  });

  it('notifies once for an authenticated 401 but ignores a login-style unauthenticated 401', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        jsonResponse(
          { code: 'AUTHENTICATION_REQUIRED', message: 'Authentication is required.' },
          401,
        ),
      );
    vi.stubGlobal('fetch', fetchMock);
    const onUnauthorized = vi.fn();
    const unregister = registerUnauthorizedHandler(onUnauthorized);

    await expect(requestJson('/auth/login', { method: 'POST', body: '{}' })).rejects.toMatchObject({
      status: 401,
    });
    expect(onUnauthorized).not.toHaveBeenCalled();

    setCsrfToken('session-csrf');
    await expect(requestJson('/projects')).rejects.toMatchObject({ status: 401 });
    await expect(requestJson('/deployments')).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    unregister();
  });

  it('parses the JSON 200 logout response', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ loggedOut: true })));

    await expect(
      requestJson<{ loggedOut: true }>('/auth/logout', { method: 'POST' }),
    ).resolves.toEqual({ loggedOut: true });
  });

  it.each(['server error', 'network error'])(
    'keeps authentication after a %s revalidation',
    async (kind) => {
      const fetchMock = vi.fn();
      if (kind === 'server error') {
        fetchMock.mockResolvedValueOnce(
          jsonResponse({ code: 'SERVICE_UNAVAILABLE', message: 'Unavailable' }, 503),
        );
      } else {
        fetchMock.mockRejectedValueOnce(new TypeError('network unavailable'));
      }
      fetchMock.mockResolvedValueOnce(jsonResponse({ saved: true }));
      vi.stubGlobal('fetch', fetchMock);
      const onUnauthorized = vi.fn();
      const unregister = registerUnauthorizedHandler(onUnauthorized);
      setCsrfToken('session-csrf');

      await revalidateAuthenticatedSession();
      await requestJson('/projects', { method: 'POST', body: '{}' });

      expect(onUnauthorized).not.toHaveBeenCalled();
      const mutationHeaders = new Headers((fetchMock.mock.calls[1]?.[1] as RequestInit).headers);
      expect(mutationHeaders.get('X-CSRF-Token')).toBe('session-csrf');
      unregister();
    },
  );
});
