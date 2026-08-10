type IconName =
  | 'activity'
  | 'arrow'
  | 'branch'
  | 'check'
  | 'git'
  | 'grid'
  | 'plus'
  | 'refresh'
  | 'settings'
  | 'shield';

const paths: Record<IconName, string> = {
  activity: 'M3 12h4l2-7 4 14 2-7h6',
  arrow: 'M5 12h14M13 6l6 6-6 6',
  branch: 'M6 3v12a3 3 0 0 0 3 3h6M18 6v12M15 6h6M15 18h6',
  check: 'm5 12 4 4L19 6',
  git: 'm4 4 16 16M14 6l4-2 2 4M4 16l4 4 2-4',
  grid: 'M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z',
  plus: 'M12 5v14M5 12h14',
  refresh: 'M20 6v5h-5M4 18v-5h5M18.5 10a7 7 0 0 0-12-3L4 11M5.5 14a7 7 0 0 0 12 3l2.5-4',
  settings: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8M4 12h2m12 0h2M12 4v2m0 12v2',
  shield: 'M12 3 5 6v5c0 4.6 2.9 7.8 7 10 4.1-2.2 7-5.4 7-10V6zM9 12l2 2 4-5',
};

export function Icon({ name }: { name: IconName }) {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" aria-hidden="true">
      <path d={paths[name]} stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}
