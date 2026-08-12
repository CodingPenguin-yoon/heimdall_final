import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getRuntimeReconciliation, requestRuntimeReconciliation } from '@/entities/deployment/api';
import type { Deployment, RuntimeReconciliation } from '@/entities/deployment/types';

import { RuntimeReconciliationPanel } from './RuntimeReconciliationPanel';

vi.mock('@/entities/deployment/api', () => ({
  getRuntimeReconciliation: vi.fn(),
  requestRuntimeReconciliation: vi.fn(),
}));

const deployment: Deployment = {
  id: '8a7d1b1a-1df0-4a75-af80-2cbc60b734b9',
  projectId: 'project-1',
  sourceType: 'MAIN_HEAD',
  requestedCommitSha: null,
  resolvedCommitSha: 'a'.repeat(40),
  configVersion: 1,
  status: 'FAILED',
  failureStage: 'RECOVERY',
  failureCode: 'RECOVERY_STATE_UNCERTAIN',
  createdAt: '2026-08-05T06:00:00Z',
  updatedAt: '2026-08-05T06:05:00Z',
  terminalAt: '2026-08-05T06:05:00Z',
};

const retained: RuntimeReconciliation = {
  deploymentId: deployment.id,
  state: 'RETAINED',
  action: 'RECONCILE',
  requestedBy: 'SYSTEM',
  result: null,
  resultCode: null,
  attempts: 0,
  availableAt: '2026-08-08T06:05:00Z',
  updatedAt: '2026-08-05T06:05:00Z',
  completedAt: null,
};

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <RuntimeReconciliationPanel deployment={deployment} projectId="project-1" />
    </QueryClientProvider>,
  );
}

describe('RuntimeReconciliationPanel', () => {
  beforeEach(() => {
    vi.mocked(getRuntimeReconciliation).mockResolvedValue(retained);
    vi.mocked(requestRuntimeReconciliation).mockResolvedValue({
      ...retained,
      state: 'PENDING',
      requestedBy: 'ADMIN',
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('queues an immediate safe reconciliation without cleanup confirmation', async () => {
    const user = userEvent.setup();
    renderPanel();

    await user.click(await screen.findByRole('button', { name: '지금 안전 확인' }));

    expect(requestRuntimeReconciliation).toHaveBeenCalledWith(
      deployment.id,
      'RECONCILE',
      undefined,
    );
  });

  it('enables forced cleanup only after the full deployment ID matches', async () => {
    const user = userEvent.setup();
    renderPanel();
    const button = await screen.findByRole('button', { name: '보존 자원 강제 정리' });

    expect(button).toBeDisabled();
    await user.type(
      screen.getByRole('textbox', { name: '강제 정리 확인 Deployment ID' }),
      deployment.id,
    );
    expect(button).toBeEnabled();
    await user.click(button);

    expect(requestRuntimeReconciliation).toHaveBeenCalledWith(
      deployment.id,
      'FORCE_CLEANUP',
      deployment.id,
    );
  });

  it('hides reconciliation and force cleanup actions after resolution', async () => {
    vi.mocked(getRuntimeReconciliation).mockResolvedValue({
      ...retained,
      state: 'RESOLVED',
      result: 'CLEANED',
      resultCode: 'INACTIVE_CANDIDATE_CLEANED',
      completedAt: '2026-08-08T06:06:00Z',
    });

    renderPanel();

    expect(await screen.findByText('처리 완료')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '지금 안전 확인' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '보존 자원 강제 정리' })).not.toBeInTheDocument();
  });
});
