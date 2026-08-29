import { expect, test, type Page } from '@playwright/test';

import { adminSession, futureExpiry, mockAuthenticatedSession } from './support/auth';

const validPassword = 'correct horse battery staple';

const authenticationRequired = {
  code: 'AUTHENTICATION_REQUIRED',
  message: 'Authentication is required.',
};

async function mockEmptyManagementLists(page: Page) {
  await page.route('**/api/projects', async (route) => {
    await route.fulfill({ json: { items: [] } });
  });
  await page.route('**/api/deployments', async (route) => {
    await route.fulfill({ json: { items: [] } });
  });
}

async function mockLogout(page: Page, requests?: Array<{ method: string; csrfToken: string }>) {
  await page.route('**/api/auth/logout', async (route) => {
    requests?.push({
      method: route.request().method(),
      csrfToken: route.request().headers()['x-csrf-token'] ?? '',
    });
    await route.fulfill({
      status: 200,
      headers: { 'Cache-Control': 'no-store' },
      json: { loggedOut: true },
    });
  });
}

test('returns an unauthenticated deep link after wrong and correct login attempts', async ({
  page,
}) => {
  const loginRequests: Array<Record<string, unknown>> = [];
  await page.route('**/api/auth/session', async (route) => {
    await route.fulfill({
      status: 401,
      headers: { 'Cache-Control': 'no-store' },
      json: authenticationRequired,
    });
  });
  await page.route('**/api/auth/login', async (route) => {
    const payload = route.request().postDataJSON() as Record<string, unknown>;
    loginRequests.push(payload);
    if (payload.username !== 'admin' || payload.password !== validPassword) {
      await route.fulfill({
        status: 401,
        headers: { 'Cache-Control': 'no-store' },
        json: { code: 'INVALID_CREDENTIALS', message: 'Invalid username or password.' },
      });
      return;
    }
    await route.fulfill({
      status: 200,
      headers: { 'Cache-Control': 'no-store' },
      json: { ...adminSession, expiresAt: futureExpiry() },
    });
  });
  await mockEmptyManagementLists(page);

  await page.goto('/deployments?status=failed');

  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole('heading', { name: '관리자 로그인' })).toBeVisible();
  await page.getByLabel('사용자 이름').fill('admin');
  await page.getByLabel('비밀번호').fill('wrong password');
  await page.getByRole('button', { name: '로그인' }).click();
  await expect(page.getByText('사용자 이름 또는 비밀번호가 올바르지 않습니다.')).toBeVisible();

  await page.getByLabel('비밀번호').fill(validPassword);
  await page.getByRole('button', { name: '로그인' }).click();

  await expect(page).toHaveURL(/\/deployments\?status=failed$/);
  await expect(page.getByRole('heading', { name: '배포 활동', exact: true })).toBeVisible();
  expect(loginRequests).toEqual([
    { username: 'admin', password: 'wrong password' },
    { username: 'admin', password: validPassword },
  ]);
});

test('distinguishes a session API outage and retries the session check', async ({ page }) => {
  let available = false;
  await page.route('**/api/auth/session', async (route) => {
    if (!available) {
      await route.fulfill({
        status: 503,
        json: { code: 'SERVICE_UNAVAILABLE', message: 'Management API unavailable.' },
      });
      return;
    }
    await route.fulfill({
      status: 200,
      headers: { 'Cache-Control': 'no-store' },
      json: { ...adminSession, expiresAt: futureExpiry() },
    });
  });
  await mockEmptyManagementLists(page);

  await page.goto('/projects');

  await expect(page.getByRole('heading', { name: '관리 API에 연결할 수 없습니다.' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '관리자 로그인' })).toHaveCount(0);
  available = true;
  await page.getByRole('button', { name: '다시 확인' }).click();
  await expect(page.getByRole('heading', { name: 'Preview projects' })).toBeVisible();
});

test('logs out with the session CSRF token', async ({ page }) => {
  const logoutRequests: Array<{ method: string; csrfToken: string }> = [];
  await mockAuthenticatedSession(page);
  await mockEmptyManagementLists(page);
  await mockLogout(page, logoutRequests);

  await page.goto('/projects');

  await expect(page.getByRole('heading', { name: 'Preview projects' })).toBeVisible();
  await expect(page.getByText('admin', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '로그아웃' }).click();

  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole('heading', { name: '관리자 로그인' })).toBeVisible();
  await expect
    .poll(() => logoutRequests)
    .toEqual([{ method: 'POST', csrfToken: adminSession.csrfToken }]);
});

test('redirects to login when a protected API rejects the active session', async ({ page }) => {
  await mockAuthenticatedSession(page);
  await page.route('**/api/projects', async (route) => {
    await route.fulfill({ status: 401, json: authenticationRequired });
  });

  await page.goto('/projects');

  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole('heading', { name: '관리자 로그인' })).toBeVisible();
});

test('redirects to login at the absolute session expiry', async ({ page }) => {
  await mockAuthenticatedSession(page, futureExpiry(1_500));
  await mockEmptyManagementLists(page);

  await page.goto('/projects');

  await expect(page.getByRole('heading', { name: 'Preview projects' })).toBeVisible();
  await expect(page).toHaveURL(/\/login$/, { timeout: 5_000 });
  await expect(page.getByRole('heading', { name: '관리자 로그인' })).toBeVisible();
});

test('keeps logout visible and usable at a mobile viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockAuthenticatedSession(page);
  await mockEmptyManagementLists(page);
  await mockLogout(page);

  await page.goto('/projects');

  const logout = page.getByRole('button', { name: '로그아웃' });
  await expect(logout).toBeVisible();
  await logout.click();
  await expect(page.getByRole('heading', { name: '관리자 로그인' })).toBeVisible();
});
