import { useState } from 'react';
import { NavLink, Outlet } from 'react-router';

import { useAdminAuth } from '@/features/admin-auth/AdminAuthProvider';
import { Icon } from '@/shared/ui/Icon';

import styles from './app-shell.module.css';

export function AppShell() {
  const auth = useAdminAuth();
  const [loggingOut, setLoggingOut] = useState(false);
  const [logoutError, setLogoutError] = useState(false);

  async function handleLogout() {
    setLoggingOut(true);
    setLogoutError(false);
    try {
      await auth.logout();
    } catch {
      setLogoutError(true);
      setLoggingOut(false);
    }
  }

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
          <NavLink to="/deployments" className={({ isActive }) => (isActive ? styles.active : '')}>
            <Icon name="activity" />
            배포 활동
          </NavLink>
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
            <span className={styles.avatar}>
              {auth.session?.username.slice(0, 1).toUpperCase()}
            </span>
            <div>
              <strong>{auth.session?.username}</strong>
              <span>Administrator</span>
            </div>
            <button
              className={styles.logoutButton}
              type="button"
              disabled={loggingOut}
              onClick={() => void handleLogout()}
            >
              {loggingOut ? '로그아웃 중' : '로그아웃'}
            </button>
            {logoutError ? (
              <span className={styles.logoutError} role="alert">
                로그아웃하지 못했습니다.
              </span>
            ) : null}
          </div>
        </header>
        <main className={styles.content}>
          <Outlet />
        </main>
      </section>
    </div>
  );
}
