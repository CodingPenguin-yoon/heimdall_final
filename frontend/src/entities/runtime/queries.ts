import { queryOptions } from '@tanstack/react-query';

import { getProjectRuntime } from './api';

export const runtimeKeys = {
  project: (projectId: string) => ['projects', projectId, 'runtime'] as const,
};

export const projectRuntimeQuery = (projectId: string) =>
  queryOptions({
    queryKey: runtimeKeys.project(projectId),
    queryFn: () => getProjectRuntime(projectId),
    refetchInterval: 3_000,
  });
