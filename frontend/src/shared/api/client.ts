export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

const csrfProtectedMethods = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

let csrfToken: string | null = null;
let unauthorizedHandler: (() => void) | null = null;
let sessionRevalidation: Promise<void> | null = null;

export function setCsrfToken(token: string | null): void {
  if (token !== csrfToken) sessionRevalidation = null;
  csrfToken = token;
}

export function registerUnauthorizedHandler(handler: () => void): () => void {
  unauthorizedHandler = handler;
  return () => {
    if (unauthorizedHandler === handler) unauthorizedHandler = null;
  };
}

export async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  const method = (init?.method ?? 'GET').toUpperCase();
  const requestCsrfToken = csrfToken;

  if (!headers.has('Accept')) headers.set('Accept', 'application/json');
  if (init?.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  if (requestCsrfToken && csrfProtectedMethods.has(method)) {
    headers.set('X-CSRF-Token', requestCsrfToken);
  }

  const response = await fetch(`/api${path}`, {
    ...init,
    credentials: 'same-origin',
    headers,
  });

  if (!response.ok) {
    if (response.status === 401 && requestCsrfToken !== null && csrfToken === requestCsrfToken) {
      csrfToken = null;
      unauthorizedHandler?.();
    }
    const problem = (await response.json().catch(() => null)) as {
      code?: string;
      message?: string;
    } | null;
    throw new ApiError(
      response.status,
      problem?.code ?? 'REQUEST_FAILED',
      problem?.message ?? '요청을 처리하지 못했습니다.',
    );
  }

  return (await response.json()) as T;
}

export function revalidateAuthenticatedSession(): Promise<void> {
  if (csrfToken === null) return Promise.resolve();
  if (sessionRevalidation) return sessionRevalidation;

  const revalidation = requestJson<unknown>('/auth/session')
    .then(() => undefined)
    .catch(() => undefined)
    .finally(() => {
      if (sessionRevalidation === revalidation) sessionRevalidation = null;
    });
  sessionRevalidation = revalidation;
  return revalidation;
}
