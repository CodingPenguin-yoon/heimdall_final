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

test('selects and manually refreshes a service log snapshot', async ({ page }) => {
  const requestedServices: Array<string | null> = [];
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
    if (url.pathname === `/api/deployments/${deploymentId}/service-logs`) {
      const serviceName = url.searchParams.get('service');
      requestedServices.push(serviceName);
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
              message: `${serviceName ?? 'web'} snapshot ${requestedServices.length}`,
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
  await expect(page.getByText('web snapshot 1')).toBeVisible();
  await page.getByRole('combobox', { name: '로그 서비스 선택' }).selectOption('api');
  await expect(page.getByText('api snapshot 2')).toBeVisible();
  await page.getByRole('button', { name: '새로고침' }).click();
  await expect(page.getByText('api snapshot 3')).toBeVisible();
  expect(requestedServices).toEqual([null, 'api', 'api']);
});
