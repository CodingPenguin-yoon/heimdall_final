import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { disablePublicRoute, getPublicRoute, savePublicRoute } from '@/entities/public-route/api';
import type { PublicRoute, PublicRouteStatus } from '@/entities/public-route/types';
import { ApiError } from '@/shared/api/client';

import { PublicRoutePanel } from './PublicRoutePanel';

vi.mock('@/entities/public-route/api', () => ({
  getPublicRoute: vi.fn(),
  savePublicRoute: vi.fn(),
  disablePublicRoute: vi.fn(),
}));

const route: PublicRoute = {
  projectId: 'project-1',
  subdomain: 'student-a',
  hostname: 'student-a.deploy.example',
  desiredState: 'ENABLED',
  status: 'ACTIVE',
  desiredRevision: 1,
  appliedRevision: 1,
  appliedHostname: 'student-a.deploy.example',
  lastErrorCode: null,
  createdAt: '2026-08-21T00:00:00Z',
  updatedAt: '2026-08-21T00:00:00Z',
};

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <PublicRoutePanel projectId="project-1" />
    </QueryClientProvider>,
  );
}

describe('PublicRoutePanel', () => {
  beforeEach(() => {
    vi.mocked(getPublicRoute).mockResolvedValue(null);
    vi.mocked(savePublicRoute).mockResolvedValue({
      ...route,
      status: 'PENDING',
      appliedRevision: null,
      appliedHostname: null,
    });
    vi.mocked(disablePublicRoute).mockResolvedValue({
      ...route,
      desiredState: 'DISABLED',
      status: 'PENDING',
      desiredRevision: 2,
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('reserves a subdomain without requiring an active runtime', async () => {
    const user = userEvent.setup();
    renderPanel();

    await user.type(
      await screen.findByRole('textbox', { name: 'Public hostname subdomain' }),
      'student-a',
    );
    await user.click(screen.getByRole('button', { name: 'Hostname 예약' }));

    expect(savePublicRoute).toHaveBeenCalledWith('project-1', 'student-a');
    expect(await screen.findByText('http://student-a.deploy.example')).toBeInTheDocument();
    expect(
      screen.queryByRole('link', { name: 'http://student-a.deploy.example' }),
    ).not.toBeInTheDocument();
    expect(screen.getByText('적용 대기')).toBeInTheDocument();
  });

  it('keeps the applied URL reachable while disable is pending', async () => {
    const user = userEvent.setup();
    vi.mocked(getPublicRoute).mockResolvedValue(route);
    renderPanel();

    await user.click(await screen.findByRole('button', { name: '비활성화' }));

    expect(disablePublicRoute).toHaveBeenCalledWith('project-1');
    expect(await screen.findByText('DISABLED')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'http://student-a.deploy.example' })).toHaveAttribute(
      'href',
      'http://student-a.deploy.example',
    );
  });

  it('shows API conflict messages without deriving a hostname in the browser', async () => {
    const user = userEvent.setup();
    vi.mocked(savePublicRoute).mockRejectedValue(
      new ApiError(409, 'PUBLIC_ROUTE_HOSTNAME_CONFLICT', '이미 사용 중인 subdomain입니다.'),
    );
    renderPanel();

    await user.type(
      await screen.findByRole('textbox', { name: 'Public hostname subdomain' }),
      'taken',
    );
    await user.click(screen.getByRole('button', { name: 'Hostname 예약' }));

    expect(await screen.findByText('이미 사용 중인 subdomain입니다.')).toBeInTheDocument();
    expect(screen.queryByText(/taken\.deploy/)).not.toBeInTheDocument();
  });

  it.each(['FAILED', 'UNCERTAIN'] as PublicRouteStatus[])(
    'retries %s with the same subdomain PUT',
    async (status) => {
      const user = userEvent.setup();
      vi.mocked(getPublicRoute).mockResolvedValue({
        ...route,
        status,
        lastErrorCode: `ROUTE_${status}`,
      });
      renderPanel();

      await user.click(await screen.findByRole('button', { name: '다시 시도' }));

      expect(savePublicRoute).toHaveBeenCalledWith('project-1', 'student-a');
    },
  );

  it.each(['FAILED', 'UNCERTAIN'] as PublicRouteStatus[])(
    'retries a disabled %s route with DELETE',
    async (status) => {
      const user = userEvent.setup();
      vi.mocked(getPublicRoute).mockResolvedValue({
        ...route,
        desiredState: 'DISABLED',
        status,
        lastErrorCode: `ROUTE_${status}`,
      });
      renderPanel();

      await user.click(await screen.findByRole('button', { name: '다시 시도' }));

      expect(disablePublicRoute).toHaveBeenCalledWith('project-1');
      expect(savePublicRoute).not.toHaveBeenCalled();
    },
  );

  it('distinguishes the last applied URL from a failed rename request', async () => {
    vi.mocked(getPublicRoute).mockResolvedValue({
      ...route,
      subdomain: 'student-b',
      hostname: 'student-b.deploy.example',
      status: 'FAILED',
      desiredRevision: 2,
      lastErrorCode: 'EDGE_RELOAD_FAILED',
    });
    renderPanel();

    expect(
      await screen.findByRole('link', { name: 'http://student-a.deploy.example' }),
    ).toHaveAttribute('href', 'http://student-a.deploy.example');
    expect(screen.getByText('http://student-b.deploy.example')).toBeInTheDocument();
  });
});
