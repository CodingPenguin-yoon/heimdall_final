export interface ProjectRuntime {
  status: 'NOT_ACTIVE' | 'ACTIVE';
  previewPort: number | null;
  activeDeploymentId: string | null;
  updatedAt: string | null;
}
