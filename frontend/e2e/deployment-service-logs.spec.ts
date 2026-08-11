import { expect, test } from '@playwright/test';

const deploymentId = '77777777-7777-4777-8777-777777777777';
const projectId = '88888888-8888-4888-8888-888888888888';

const deployment = {
  id: deploymentId,
  projectId,
  sourceType: 'MAIN_HEAD',
  requestedCommitSha: null,
  resolvedCommitSha: 'c'.repeat(40),
  configVersion: 2,
  status: 'SUCCEEDED',
  failureStage: null,
  failureCode: null,
  createdAt: '2026-08-10T06:00:00Z',
  updatedAt: '2026-08-10T06:02:00Z',
  terminalAt: '2026-08-10T06:02:00Z',
};

test('follows live service logs and keeps manual snapshot refresh', async ({ page }) => {
  const streamedServices: Array<string | null> = [];
  const snapshotServices: Array<string | null> = [];
  await page.route(new RegExp(`/api/deployments/${deploymentId}(?:/.*)?`), async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === `/api/deployments/${deploymentId}`) {
      await route.fulfill({ json: deployment });
      return;
    }
    if (url.pathname === `/api/deployments/${deploymentId}/events`) {
      await route.fulfill({ json: { items: [] } });
      return;
    }
    if (url.pathname === `/api/deployments/${deploymentId}/service-logs/stream`) {
      const serviceName = url.searchParams.get('service');
      streamedServices.push(serviceName);
      const selected = serviceName ?? 'web';
      const ready = {
        deploymentId,
        services: ['web', 'api'],
        serviceName: selected,
        connectedAt: '2026-08-10T06:03:00Z',
      };
      const line = {
        timestamp: '2026-08-10T06:02:59.000000000Z',
        stream: selected === 'api' ? 'STDERR' : 'STDOUT',
        message: `${selected} live ${streamedServices.length}`,
        truncated: false,
      };
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        headers: { 'Cache-Control': 'no-store' },
        body: [
          `event: ready\ndata: ${JSON.stringify(ready)}\n\n`,
          `event: log\ndata: ${JSON.stringify(line)}\n\n`,
          'event: end\ndata: {"reason":"CONTAINER_LOG_ENDED"}\n\n',
        ].join(''),
      });
      return;
    }
    if (url.pathname === `/api/deployments/${deploymentId}/service-logs`) {
      const serviceName = url.searchParams.get('service');
      snapshotServices.push(serviceName);
      await route.fulfill({
        json: {
          deploymentId,
          services: ['web', 'api'],
          serviceName: serviceName ?? 'web',
          retrievedAt: '2026-08-10T06:03:00Z',
          lines: [
            {
              timestamp: '2026-08-10T06:02:59.000000000Z',
              stream: serviceName === 'api' ? 'STDERR' : 'STDOUT',
              message: `${serviceName ?? 'web'} snapshot ${snapshotServices.length}`,
            },
          ],
          truncated: false,
        },
      });
      return;
    }
    await route.fulfill({ status: 404, json: { code: 'NOT_FOUND' } });
  });

  await page.goto(`/deployments/${deploymentId}`);

  await expect(page.getByRole('heading', { name: '서비스 로그' })).toBeVisible();
  await expect(page.getByText('web live 1')).toBeVisible();
  await page.getByRole('combobox', { name: '로그 서비스 선택' }).selectOption('api');
  await expect(page.getByText('api live 2')).toBeVisible();
  await page.getByRole('button', { name: '새로고침' }).click();
  await expect(page.getByText('api snapshot 1')).toBeVisible();
  expect(streamedServices).toEqual([null, 'api']);
  expect(snapshotServices).toEqual(['api']);
});

test('receives structured deployment events over SSE from the last stored event', async ({
  page,
}) => {
  const activeDeploymentId = '99999999-9999-4999-8999-999999999999';
  const streamCursors: string[] = [];
  let currentStatus = 'BUILDING';
  await page.route(new RegExp(`/api/deployments/${activeDeploymentId}(?:/.*)?`), async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === `/api/deployments/${activeDeploymentId}`) {
      await route.fulfill({
        json: {
          ...deployment,
          id: activeDeploymentId,
          status: currentStatus,
          terminalAt: currentStatus === 'SUCCEEDED' ? deployment.terminalAt : null,
        },
      });
      return;
    }
    if (url.pathname === `/api/deployments/${activeDeploymentId}/events`) {
      await route.fulfill({
        json: {
          items: [
            {
              id: 11,
              deploymentId: activeDeploymentId,
              stage: 'BUILDING',
              code: 'IMAGES_BUILDING',
              message: 'Building service images',
              createdAt: '2026-08-10T06:01:00Z',
            },
          ],
        },
      });
      return;
    }
    if (url.pathname === `/api/deployments/${activeDeploymentId}/events/stream`) {
      streamCursors.push(url.searchParams.get('after') ?? '');
      currentStatus = 'SUCCEEDED';
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: [
          `event: ready\ndata: {"deploymentId":"${activeDeploymentId}","after":11}\n\n`,
          `id: 12\nevent: deployment-event\ndata: ${JSON.stringify({
            id: 12,
            deploymentId: activeDeploymentId,
            stage: 'SUCCEEDED',
            code: 'DEPLOYMENT_SUCCEEDED',
            message: 'The preview deployment is active',
            createdAt: '2026-08-10T06:02:00Z',
          })}\n\n`,
          'event: end\ndata: {"reason":"DEPLOYMENT_TERMINAL"}\n\n',
        ].join(''),
      });
      return;
    }
    if (url.pathname === `/api/deployments/${activeDeploymentId}/service-logs/stream`) {
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: [
          `event: ready\ndata: ${JSON.stringify({
            deploymentId: activeDeploymentId,
            services: ['web'],
            serviceName: 'web',
            connectedAt: '2026-08-10T06:02:00Z',
          })}\n\n`,
          'event: end\ndata: {"reason":"CONTAINER_LOG_ENDED"}\n\n',
        ].join(''),
      });
      return;
    }
    await route.fulfill({ status: 404, json: { code: 'NOT_FOUND' } });
  });

  await page.goto(`/deployments/${activeDeploymentId}`);

  await expect(page.getByText('DEPLOYMENT_SUCCEEDED')).toBeVisible();
  await expect(page.getByText('The preview deployment is active')).toBeVisible();
  await expect.poll(() => streamCursors).toEqual(['11']);
});
