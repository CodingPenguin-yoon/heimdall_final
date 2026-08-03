import { Navigate, createBrowserRouter } from 'react-router';

import { AppShell } from '@/app/shell/AppShell';
import { ProjectCreatePage } from '@/pages/project-create/ProjectCreatePage';
import { ProjectDetailPage } from '@/pages/project-detail/ProjectDetailPage';
import { ProjectListPage } from '@/pages/project-list/ProjectListPage';
import { ProjectSettingsPage } from '@/pages/project-settings/ProjectSettingsPage';

export const router = createBrowserRouter([
  {
    path: '/',
    Component: AppShell,
    children: [
      { index: true, element: <Navigate to="/projects" replace /> },
      { path: 'projects', Component: ProjectListPage },
      { path: 'projects/new', Component: ProjectCreatePage },
      { path: 'projects/:projectId', Component: ProjectDetailPage },
      { path: 'projects/:projectId/settings', Component: ProjectSettingsPage },
    ],
  },
  { path: '*', element: <Navigate to="/projects" replace /> },
]);
