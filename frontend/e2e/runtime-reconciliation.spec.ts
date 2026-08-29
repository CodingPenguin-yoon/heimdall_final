import { expect, test, type Page } from '@playwright/test';

import { mockAuthenticatedSession } from './support/auth';

const projectId = 'b5fd8229-4b3f-4c41-8a85-42908bfa7db1';
const deploymentId = '8a7d1b1a-1df0-4a75-af80-2cbc60b734b9';

const project = {
  id: projectId,
  name: 'Recovery Console',
  repositoryUrl: 'https://github.com/example/recovery-console',
  branch: 'main',
  status: 'READY',
  configVersion: 1,
  deploymentConfig: {
    services: [
      {
        name: 'web',
        build: { context: '.', dockerfile: 'Dockerfile' },
        internalPort: 8080,
        healthPath: '/health',
        environment: [],
        projectDatabaseAccess: false,
      },
    ],
    routes: [{ path: '/', service: 'web' }],
  },
  createdAt: '2026-08-05T06:00:00Z',
  updatedAt: '2026-08-05T06:05:00Z',
};

const deployment = {
  id: deploymentId,
  projectId,
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

function reconciliation(state: 'RETAINED' | 'BLOCKED' | 'PENDING') {
  return {
    deploymentId,
    state,
    action: 'RECONCILE',
    requestedBy: state === 'PENDING' ? 'ADMIN' : 'SYSTEM',
    result: state === 'BLOCKED' ? 'UNCERTAIN' : null,
    resultCode: state === 'BLOCKED' ? 'RECOVERY_STATE_UNCERTAIN' : null,
    attempts: state === 'RETAINED' ? 0 : 1,
    availableAt: '2026-08-08T06:05:00Z',
    updatedAt: '2026-08-05T06:05:00Z',
    completedAt: state === 'BLOCKED' ? '2026-08-05T06:06:00Z' : null,
  };
}

async function mockProjectApi(
  page: Page,
  initialState: 'RETAINED' | 'BLOCKED',
  requests: Array<Record<string, unknown>>,
) {
  let current = reconciliation(initialState);
  await page.route(/\/api\/(?:projects|deployments)\//, async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === `/api/projects/${projectId}`) {
      await route.fulfill({ json: project });
      return;
    }
    if (path === `/api/projects/${projectId}/deployments`) {
      await route.fulfill({ json: { items: [deployment] } });
      return;
    }
    if (path === `/api/projects/${projectId}/runtime`) {
      await route.fulfill({
        json: {
          status: 'NOT_ACTIVE',
          previewPort: 48100,
          activeDeploymentId: null,
          updatedAt: '2026-08-05T06:05:00Z',
        },
      });
      return;
    }
    if (path === `/api/projects/${projectId}/public-route`) {
      await route.fulfill({
        status: 404,
        json: { code: 'PUBLIC_ROUTE_NOT_FOUND', message: 'Public route was not found' },
      });
      return;
    }
    if (path === `/api/projects/${projectId}/commits`) {
      await route.fulfill({ json: { items: [] } });
      return;
    }
    if (path === `/api/deployments/${deploymentId}/events`) {
      await route.fulfill({ json: { items: [] } });
      return;
    }
    if (path === `/api/deployments/${deploymentId}/runtime-reconciliation`) {
      if (request.method() === 'POST') {
        const payload = request.postDataJSON() as Record<string, unknown>;
        requests.push(payload);
        current = {
          ...reconciliation('PENDING'),
          action: payload.action as string,
        };
      }
      await route.fulfill({ json: current });
      return;
    }
    await route.fulfill({ status: 404, json: { code: 'NOT_FOUND', message: path } });
  });
}

test('queues immediate safe reconciliation from the retained runtime panel', async ({ page }) => {
  const requests: Array<Record<string, unknown>> = [];
  await mockAuthenticatedSession(page);
  await mockProjectApi(page, 'RETAINED', requests);

  await page.goto(`/projects/${projectId}`);

  await expect(page.getByRole('heading', { name: 'Recovery Console' })).toBeVisible();
  await expect(page.getByText('안전 보존 중', { exact: true }).first()).toBeVisible();
  await page.getByRole('button', { name: '지금 안전 확인' }).click();
  await expect.poll(() => requests).toEqual([{ action: 'RECONCILE' }]);
  await expect(page.getByText('Worker 대기 중', { exact: true }).first()).toBeVisible();
});

test('requires the full deployment ID before forced cleanup', async ({ page }) => {
  const requests: Array<Record<string, unknown>> = [];
  await mockAuthenticatedSession(page);
  await mockProjectApi(page, 'BLOCKED', requests);
  await page.goto(`/projects/${projectId}`);
  const forceButton = page.getByRole('button', { name: '보존 자원 강제 정리' });

  await expect(forceButton).toBeDisabled();
  await page.getByRole('textbox', { name: '강제 정리 확인 Deployment ID' }).fill(deploymentId);
  await expect(forceButton).toBeEnabled();
  await forceButton.click();

  await expect
    .poll(() => requests)
    .toEqual([{ action: 'FORCE_CLEANUP', confirmation: deploymentId }]);
});
