import { requestJson } from '@/shared/api/client';

export interface AdminSession {
  username: string;
  csrfToken: string;
  expiresAt: string;
}

export function getAdminSession(): Promise<AdminSession> {
  return requestJson('/auth/session');
}

export function loginAdmin(username: string, password: string): Promise<AdminSession> {
  return requestJson('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  });
}

export function logoutAdmin(): Promise<{ loggedOut: true }> {
  return requestJson('/auth/logout', { method: 'POST' });
}
