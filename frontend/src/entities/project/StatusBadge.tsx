import type { ProjectStatus } from './types';

export function StatusBadge({ status }: { status: ProjectStatus }) {
  return (
    <span className={`status-badge status-${status.toLowerCase()}`}>
      <span />
      {status === 'READY' ? 'Ready' : 'Setup required'}
    </span>
  );
}
