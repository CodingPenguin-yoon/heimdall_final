import type { Page } from '@playwright/test';

export const adminSession = {
  username: 'admin',
  csrfToken: 'e2e-session-csrf-token',
};

export function futureExpiry(milliseconds = 60 * 60 * 1000) {
  return new Date(Date.now() + milliseconds).toISOString();
}

export async function mockAuthenticatedSession(page: Page, expiresAt = futureExpiry()) {
  await page.route('**/api/auth/session', async (route) => {
    await route.fulfill({
      status: 200,
      headers: { 'Cache-Control': 'no-store' },
      json: { ...adminSession, expiresAt },
    });
  });
}
