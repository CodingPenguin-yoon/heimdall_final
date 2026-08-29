import { queryOptions } from '@tanstack/react-query';

import { getPublicRoute } from './api';

export const publicRouteKeys = {
  detail: (projectId: string) => ['projects', projectId, 'public-route'] as const,
};

export const publicRouteQuery = (projectId: string) =>
  queryOptions({
    queryKey: publicRouteKeys.detail(projectId),
    queryFn: () => getPublicRoute(projectId),
    enabled: Boolean(projectId),
    refetchInterval: (query) =>
      ['PENDING', 'APPLYING'].includes(query.state.data?.status ?? '') ? 1_000 : false,
  });
