type IconName = 'activity' | 'arrow' | 'branch' | 'check' | 'git' | 'grid' | 'plus' | 'settings';

const paths: Record<IconName, string> = {
  activity: 'M3 12h4l2-7 4 14 2-7h6',
  arrow: 'M5 12h14M13 6l6 6-6 6',
  branch: 'M6 3v12a3 3 0 0 0 3 3h6M18 6v12M15 6h6M15 18h6',
  check: 'm5 12 4 4L19 6',
  git: 'm4 4 16 16M14 6l4-2 2 4M4 16l4 4 2-4',
  grid: 'M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z',
  plus: 'M12 5v14M5 12h14',
  settings: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8M4 12h2m12 0h2M12 4v2m0 12v2',
};

export function Icon({ name }: { name: IconName }) {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" aria-hidden="true">
      <path d={paths[name]} stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}
