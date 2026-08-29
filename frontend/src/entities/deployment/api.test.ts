import { afterEach, expect, it, vi } from 'vitest';

import { registerUnauthorizedHandler, setCsrfToken } from '@/shared/api/client';

import { subscribeDeploymentEvents, subscribeServiceLogs } from './api';

class FakeEventSource {
  static instances: FakeEventSource[] = [];

  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  readonly close = vi.fn();
  readonly addEventListener = vi.fn();

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }

  emitConnectionError(): void {
    this.onerror?.(new Event('error'));
  }
}

let unregisterUnauthorized: () => void = () => undefined;

afterEach(() => {
  unregisterUnauthorized();
  unregisterUnauthorized = () => undefined;
  FakeEventSource.instances = [];
  setCsrfToken(null);
  vi.unstubAllGlobals();
});

it('revalidates and deduplicates authentication after either EventSource connection error', async () => {
  let resolveSession!: (response: Response) => void;
  const fetchMock = vi.fn(
    () =>
      new Promise<Response>((resolve) => {
        resolveSession = resolve;
      }),
  );
  vi.stubGlobal('fetch', fetchMock);
  vi.stubGlobal('EventSource', FakeEventSource);
  const onUnauthorized = vi.fn();
  unregisterUnauthorized = registerUnauthorizedHandler(onUnauthorized);
  setCsrfToken('session-csrf');
  const deploymentConnectionError = vi.fn();
  const serviceConnectionError = vi.fn();

  subscribeDeploymentEvents('deployment-id', 3, {
    onOpen: vi.fn(),
    onReady: vi.fn(),
    onEvent: vi.fn(),
    onEnd: vi.fn(),
    onStreamError: vi.fn(),
    onConnectionError: deploymentConnectionError,
  });
  subscribeServiceLogs('deployment-id', 'api', {
    onOpen: vi.fn(),
    onReady: vi.fn(),
    onLine: vi.fn(),
    onEnd: vi.fn(),
    onStreamError: vi.fn(),
    onConnectionError: serviceConnectionError,
  });

  FakeEventSource.instances[0]?.emitConnectionError();
  FakeEventSource.instances[1]?.emitConnectionError();

  expect(deploymentConnectionError).toHaveBeenCalledOnce();
  expect(serviceConnectionError).toHaveBeenCalledOnce();
  expect(fetchMock).toHaveBeenCalledOnce();
  expect(fetchMock).toHaveBeenCalledWith(
    '/api/auth/session',
    expect.objectContaining({ credentials: 'same-origin' }),
  );

  resolveSession(
    new Response(
      JSON.stringify({ code: 'AUTHENTICATION_REQUIRED', message: 'Authentication required' }),
      { status: 401, headers: { 'Content-Type': 'application/json' } },
    ),
  );

  await vi.waitFor(() => expect(onUnauthorized).toHaveBeenCalledOnce());
});
