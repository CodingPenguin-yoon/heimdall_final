import { queryOptions } from '@tanstack/react-query';

import { getProject, listCommits, listProjects } from './api';

export const projectKeys = {
  all: ['projects'] as const,
  detail: (projectId: string) => ['projects', projectId] as const,
  commits: (projectId: string) => ['projects', projectId, 'commits'] as const,
};

export const projectsQuery = () =>
  queryOptions({ queryKey: projectKeys.all, queryFn: listProjects });

export const projectQuery = (projectId: string) =>
  queryOptions({ queryKey: projectKeys.detail(projectId), queryFn: () => getProject(projectId) });

export const commitsQuery = (projectId: string) =>
  queryOptions({
    queryKey: projectKeys.commits(projectId),
    queryFn: () => listCommits(projectId),
    enabled: Boolean(projectId),
  });
