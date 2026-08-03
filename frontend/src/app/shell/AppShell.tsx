import { NavLink, Outlet } from 'react-router';

import { Icon } from '@/shared/ui/Icon';

import styles from './app-shell.module.css';

export function AppShell() {
  return (
    <div className={styles.shell}>
      <aside className={styles.sidebar}>
        <div className={styles.brand}>
          <span className={styles.mark}>H</span>
          <div>
            <strong>Heimdall</strong>
            <span>Preview control</span>
          </div>
        </div>

        <nav className={styles.navigation} aria-label="주 메뉴">
          <NavLink to="/projects" className={({ isActive }) => (isActive ? styles.active : '')}>
            <Icon name="grid" />
            프로젝트
          </NavLink>
          <span className={styles.disabledLink} aria-disabled="true">
            <Icon name="activity" />
            배포 활동
            <small>곧 제공</small>
          </span>
        </nav>

        <div className={styles.sidebarFooter}>
          <span className={styles.liveDot} />
          <div>
            <strong>Control plane</strong>
            <span>Local host</span>
          </div>
        </div>
      </aside>

      <section className={styles.workspace}>
        <header className={styles.topbar}>
          <div className={styles.breadcrumb}>Workspace / Preview environments</div>
          <div className={styles.admin}>
            <span className={styles.avatar}>A</span>
            <div>
              <strong>Administrator</strong>
              <span>Single host</span>
            </div>
          </div>
        </header>
        <main className={styles.content}>
          <Outlet />
        </main>
      </section>
    </div>
  );
}
