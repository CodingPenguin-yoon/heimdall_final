import type { ProjectStatus } from './types';

export function StatusBadge({ status }: { status: ProjectStatus }) {
  const label =
    status === 'READY' ? 'Ready' : status === 'DELETING' ? 'Deleting' : 'Setup required';
  return (
    <span className={`status-badge status-${status.toLowerCase()}`}>
      <span />
      {label}
    </span>
  );
}
