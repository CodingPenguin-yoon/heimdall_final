import { queryOptions } from '@tanstack/react-query';

import { getProjectDatabase } from './api';

export const databaseKeys = {
  detail: (projectId: string) => ['projects', projectId, 'database'] as const,
};

export const projectDatabaseQuery = (projectId: string, enabled: boolean) =>
  queryOptions({
    queryKey: databaseKeys.detail(projectId),
    queryFn: () => getProjectDatabase(projectId),
    enabled: Boolean(projectId) && enabled,
  });
