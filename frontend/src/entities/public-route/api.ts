import { ApiError, requestJson } from '@/shared/api/client';

import type { PublicRoute } from './types';

export async function getPublicRoute(projectId: string): Promise<PublicRoute | null> {
  try {
    return await requestJson(`/projects/${projectId}/public-route`);
  } catch (error) {
    if (
      error instanceof ApiError &&
      error.status === 404 &&
      error.code === 'PUBLIC_ROUTE_NOT_FOUND'
    ) {
      return null;
    }
    throw error;
  }
}

export function savePublicRoute(projectId: string, subdomain: string): Promise<PublicRoute> {
  return requestJson(`/projects/${projectId}/public-route`, {
    method: 'PUT',
    body: JSON.stringify({ subdomain }),
  });
}

export function disablePublicRoute(projectId: string): Promise<PublicRoute> {
  return requestJson(`/projects/${projectId}/public-route`, { method: 'DELETE' });
}
