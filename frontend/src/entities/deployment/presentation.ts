import type { DeploymentStatus } from './types';

export const deploymentStatusLabels: Record<DeploymentStatus, string> = {
  QUEUED: '대기 중',
  PREPARING: '소스 준비 중',
  BUILDING: '이미지 빌드 중',
  STARTING: '서비스 시작 중',
  HEALTH_CHECKING: '서비스 상태 확인 중',
  ACTIVATING: 'Preview 전환 중',
  SUCCEEDED: '성공',
  FAILED: '실패',
};

export function isDeploymentTerminal(status: DeploymentStatus): boolean {
  return status === 'SUCCEEDED' || status === 'FAILED';
}

export function deploymentStatusTone(status: DeploymentStatus): 'active' | 'succeeded' | 'failed' {
  if (status === 'SUCCEEDED') return 'succeeded';
  if (status === 'FAILED') return 'failed';
  return 'active';
}
