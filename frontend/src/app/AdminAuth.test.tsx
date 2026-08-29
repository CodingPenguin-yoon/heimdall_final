import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  MemoryRouter,
  Route,
  Routes,
  createPath,
  useLocation,
  type MemoryRouterProps,
} from 'react-router';

import { LoginPage } from '@/pages/login/LoginPage';
import { requestJson, setCsrfToken } from '@/shared/api/client';

import { AdminAuthProvider, RequireAdmin } from '@/features/admin-auth/AdminAuthProvider';

const session = {
  username: 'admin',
  csrfToken: 'unit-session-csrf',
  expiresAt: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
};

type InitialEntry = NonNullable<MemoryRouterProps['initialEntries']>[number];

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function RouteProbe() {
  const location = useLocation();
  return (
    <div>
      <output data-testid="current-location">{createPath(location)}</output>
      <button type="button" onClick={() => void requestJson('/protected').catch(() => undefined)}>
        보호 요청
      </button>
    </div>
  );
}

function renderAuth(initialEntry: InitialEntry) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route Component={AdminAuthProvider}>
            <Route path="/login" Component={LoginPage} />
            <Route Component={RequireAdmin}>
              <Route path="/projects" element={<RouteProbe />} />
              <Route path="/deployments" element={<RouteProbe />} />
            </Route>
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return queryClient;
}

afterEach(() => {
  cleanup();
  setCsrfToken(null);
  vi.unstubAllGlobals();
});

describe('AdminAuthProvider', () => {
  it('returns a protected deep link after generic wrong-credential handling and login', async () => {
    const loginBodies: unknown[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const path = String(input);
        if (path === '/api/auth/session') {
          return jsonResponse(
            { code: 'AUTHENTICATION_REQUIRED', message: 'Authentication required' },
            401,
          );
        }
        if (path === '/api/auth/login') {
          const body = JSON.parse(String(init?.body)) as { username: string; password: string };
          loginBodies.push(body);
          return body.password === 'correct-password'
            ? jsonResponse(session)
            : jsonResponse({ code: 'INVALID_CREDENTIALS', message: 'Invalid credentials' }, 401);
        }
        throw new Error(`Unexpected request: ${path}`);
      }),
    );
    const user = userEvent.setup();

    renderAuth('/deployments?status=failed#activity');

    await screen.findByRole('heading', { name: '관리자 로그인' });
    await user.type(screen.getByLabelText('사용자 이름'), 'admin');
    await user.type(screen.getByLabelText('비밀번호'), 'wrong-password');
    await user.click(screen.getByRole('button', { name: '로그인' }));
    expect(await screen.findByText('사용자 이름 또는 비밀번호가 올바르지 않습니다.')).toBeVisible();

    await user.type(screen.getByLabelText('비밀번호'), 'correct-password');
    await user.click(screen.getByRole('button', { name: '로그인' }));

    expect(await screen.findByTestId('current-location')).toHaveTextContent(
      '/deployments?status=failed#activity',
    );
    expect(loginBodies).toEqual([
      { username: 'admin', password: 'wrong-password' },
      { username: 'admin', password: 'correct-password' },
    ]);
  });

  it('keeps an API outage distinct from unauthenticated login and retries', async () => {
    let available = false;
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        available
          ? jsonResponse(session)
          : jsonResponse({ code: 'SERVICE_UNAVAILABLE', message: 'Unavailable' }, 503),
      ),
    );
    const user = userEvent.setup();

    renderAuth('/projects');

    expect(
      await screen.findByRole('heading', { name: '관리 API에 연결할 수 없습니다.' }),
    ).toBeVisible();
    expect(screen.queryByRole('heading', { name: '관리자 로그인' })).not.toBeInTheDocument();
    available = true;
    await user.click(screen.getByRole('button', { name: '다시 확인' }));
    expect(await screen.findByTestId('current-location')).toHaveTextContent('/projects');
  });

  it.each([
    ['a protocol-relative pathname', { pathname: '//attacker.example/path', search: '', hash: '' }],
    ['multiple leading slashes', { pathname: '///attacker.example/path', search: '', hash: '' }],
    ['a backslash-normalized host', { pathname: '/\\attacker.example/path', search: '', hash: '' }],
    ['missing search and hash values', { pathname: '/deployments' }],
  ])('rejects crafted login return state with %s', async (_case, from) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse(session)));

    renderAuth({ pathname: '/login', state: { from } });

    expect(await screen.findByTestId('current-location')).toHaveTextContent('/projects');
  });

  it('accepts the complete backend password input bound', async () => {
    vi.stubGlobal(
      'fetch',
      vi
        .fn()
        .mockResolvedValue(
          jsonResponse(
            { code: 'AUTHENTICATION_REQUIRED', message: 'Authentication required' },
            401,
          ),
        ),
    );

    renderAuth('/login');

    expect(await screen.findByLabelText('비밀번호')).toHaveAttribute('maxlength', '1024');
  });

  it('clears protected query cache and navigates when an authenticated request returns 401', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request) =>
        String(input) === '/api/auth/session'
          ? jsonResponse(session)
          : jsonResponse(
              { code: 'AUTHENTICATION_REQUIRED', message: 'Authentication required' },
              401,
            ),
      ),
    );
    const user = userEvent.setup();
    const queryClient = renderAuth('/projects');
    queryClient.setQueryData(['projects'], { items: [{ id: 'cached-project' }] });

    await user.click(await screen.findByRole('button', { name: '보호 요청' }));

    await screen.findByRole('heading', { name: '관리자 로그인' });
    await waitFor(() => expect(queryClient.getQueryCache().findAll()).toHaveLength(0));
  });
});
