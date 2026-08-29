import { Navigate, createBrowserRouter } from 'react-router';

import { AppShell } from '@/app/shell/AppShell';
import { AdminAuthProvider, RequireAdmin } from '@/features/admin-auth/AdminAuthProvider';
import { DeploymentActivityPage } from '@/pages/deployment-activity/DeploymentActivityPage';
import { DeploymentDetailPage } from '@/pages/deployment-detail/DeploymentDetailPage';
import { LoginPage } from '@/pages/login/LoginPage';
import { ProjectCreatePage } from '@/pages/project-create/ProjectCreatePage';
import { ProjectDetailPage } from '@/pages/project-detail/ProjectDetailPage';
import { ProjectListPage } from '@/pages/project-list/ProjectListPage';
import { ProjectSettingsPage } from '@/pages/project-settings/ProjectSettingsPage';

export const router = createBrowserRouter([
  {
    Component: AdminAuthProvider,
    children: [
      { path: '/login', Component: LoginPage },
      {
        Component: RequireAdmin,
        children: [
          {
            path: '/',
            Component: AppShell,
            children: [
              { index: true, element: <Navigate to="/projects" replace /> },
              { path: 'projects', Component: ProjectListPage },
              { path: 'projects/new', Component: ProjectCreatePage },
              { path: 'projects/:projectId', Component: ProjectDetailPage },
              { path: 'projects/:projectId/settings', Component: ProjectSettingsPage },
              { path: 'deployments', Component: DeploymentActivityPage },
              { path: 'deployments/:deploymentId', Component: DeploymentDetailPage },
            ],
          },
          { path: '*', element: <Navigate to="/projects" replace /> },
        ],
      },
    ],
  },
]);
