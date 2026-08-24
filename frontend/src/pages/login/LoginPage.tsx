import { useState, type FormEvent } from 'react';
import { Navigate, createPath, useLocation } from 'react-router';

import { useAdminAuth } from '@/features/admin-auth/AdminAuthProvider';
import { ApiError } from '@/shared/api/client';

import styles from './login-page.module.css';

function safeReturnPath(state: unknown): string {
  if (!state || typeof state !== 'object' || !('from' in state)) return '/projects';
  const from = state.from;
  if (!from || typeof from !== 'object') return '/projects';
  if (!('pathname' in from) || typeof from.pathname !== 'string') return '/projects';
  if (!from.pathname.startsWith('/') || from.pathname.startsWith('//')) return '/projects';
  if (
    from.pathname.includes('\\') ||
    [...from.pathname].some((character) => {
      const codePoint = character.codePointAt(0) ?? 0;
      return codePoint <= 0x1f || codePoint === 0x7f;
    })
  ) {
    return '/projects';
  }
  if (from.pathname === '/login') return '/projects';
  if (!('search' in from) || typeof from.search !== 'string') return '/projects';
  if (!('hash' in from) || typeof from.hash !== 'string') return '/projects';
  const candidate = createPath({ pathname: from.pathname, search: from.search, hash: from.hash });
  try {
    const resolved = new URL(candidate, window.location.origin);
    if (resolved.origin !== window.location.origin) return '/projects';
    return createPath({
      pathname: resolved.pathname,
      search: resolved.search,
      hash: resolved.hash,
    });
  } catch {
    return '/projects';
  }
}

export function LoginPage() {
  const auth = useAdminAuth();
  const location = useLocation();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const returnTo = safeReturnPath(location.state);

  if (auth.status === 'AUTHENTICATED') return <Navigate to={returnTo} replace />;

  if (auth.status === 'CHECKING') {
    return <div className={styles.status}>관리자 세션을 확인하는 중입니다.</div>;
  }

  if (auth.status === 'UNAVAILABLE') {
    return (
      <main className={styles.page}>
        <section className={styles.card}>
          <span className={styles.mark}>H</span>
          <h1>관리 API에 연결할 수 없습니다.</h1>
          <p>FastAPI 연결 상태를 확인한 뒤 다시 시도해주세요.</p>
          <button className="button primary wide" type="button" onClick={auth.retrySession}>
            다시 확인
          </button>
        </section>
      </main>
    );
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await auth.login(username, password);
    } catch (loginError) {
      setPassword('');
      setError(
        loginError instanceof ApiError && loginError.status === 401
          ? '사용자 이름 또는 비밀번호가 올바르지 않습니다.'
          : '로그인 API에 연결할 수 없습니다. 잠시 뒤 다시 시도해주세요.',
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className={styles.page}>
      <section className={styles.card}>
        <div className={styles.heading}>
          <span className={styles.mark}>H</span>
          <div>
            <span className="eyebrow">Heimdall control plane</span>
            <h1>관리자 로그인</h1>
          </div>
        </div>
        <p className={styles.intro}>관리 화면을 계속하려면 단일 관리자 계정으로 로그인하세요.</p>
        <form className={styles.form} onSubmit={handleSubmit}>
          <label>
            사용자 이름
            <input
              name="username"
              autoComplete="username"
              maxLength={64}
              required
              value={username}
              onChange={(event) => setUsername(event.target.value)}
            />
          </label>
          <label>
            비밀번호
            <input
              name="password"
              type="password"
              autoComplete="current-password"
              maxLength={1024}
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </label>
          {error ? (
            <div className="inline-error" role="alert">
              {error}
            </div>
          ) : null}
          <button className="button primary wide" type="submit" disabled={submitting}>
            {submitting ? '로그인 중' : '로그인'}
          </button>
        </form>
      </section>
    </main>
  );
}
