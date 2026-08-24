import { useQueryClient } from '@tanstack/react-query';
import { Navigate, Outlet, useLocation, useNavigate } from 'react-router';
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';

import { ApiError, registerUnauthorizedHandler, setCsrfToken } from '@/shared/api/client';

import { getAdminSession, loginAdmin, logoutAdmin, type AdminSession } from './api';

export type AdminAuthStatus = 'CHECKING' | 'AUTHENTICATED' | 'UNAUTHENTICATED' | 'UNAVAILABLE';

interface AdminAuthContextValue {
  status: AdminAuthStatus;
  session: AdminSession | null;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  retrySession: () => void;
}

interface AdminAuthState {
  status: AdminAuthStatus;
  session: AdminSession | null;
}

const AdminAuthContext = createContext<AdminAuthContextValue | null>(null);

function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const requestSequence = useRef(0);
  const [state, setState] = useState<AdminAuthState>({
    status: 'CHECKING',
    session: null,
  });

  const acceptSession = useCallback((session: AdminSession) => {
    setCsrfToken(session.csrfToken);
    setState({ status: 'AUTHENTICATED', session });
  }, []);

  const clearSession = useCallback(() => {
    setCsrfToken(null);
    queryClient.clear();
    setState({ status: 'UNAUTHENTICATED', session: null });
  }, [queryClient]);

  const handleUnauthorized = useCallback(() => {
    clearSession();
    if (location.pathname === '/login') return;
    navigate('/login', { replace: true, state: { from: location } });
  }, [clearSession, location, navigate]);

  const loadSession = useCallback(async () => {
    const sequence = ++requestSequence.current;
    setCsrfToken(null);

    try {
      const session = await getAdminSession();
      if (sequence !== requestSequence.current) return;
      acceptSession(session);
    } catch (error) {
      if (sequence !== requestSequence.current) return;
      if (error instanceof ApiError && error.status === 401) {
        clearSession();
        return;
      }
      setState({ status: 'UNAVAILABLE', session: null });
    }
  }, [acceptSession, clearSession]);

  useEffect(() => registerUnauthorizedHandler(handleUnauthorized), [handleUnauthorized]);

  useEffect(() => {
    let active = true;
    queueMicrotask(() => {
      if (active) void loadSession();
    });
    return () => {
      active = false;
      requestSequence.current += 1;
    };
  }, [loadSession]);

  useEffect(() => {
    if (state.status !== 'AUTHENTICATED' || !state.session) return;
    const delay = Math.max(0, Date.parse(state.session.expiresAt) - Date.now());
    const timeout = window.setTimeout(handleUnauthorized, delay);
    return () => window.clearTimeout(timeout);
  }, [handleUnauthorized, state]);

  const login = useCallback(
    async (username: string, password: string) => {
      const session = await loginAdmin(username, password);
      acceptSession(session);
    },
    [acceptSession],
  );

  const logout = useCallback(async () => {
    await logoutAdmin();
    clearSession();
    navigate('/login', { replace: true });
  }, [clearSession, navigate]);

  const retrySession = useCallback(() => {
    setState({ status: 'CHECKING', session: null });
    void loadSession();
  }, [loadSession]);

  const value = useMemo<AdminAuthContextValue>(
    () => ({
      status: state.status,
      session: state.session,
      login,
      logout,
      retrySession,
    }),
    [login, logout, retrySession, state],
  );

  return <AdminAuthContext.Provider value={value}>{children}</AdminAuthContext.Provider>;
}

export function AdminAuthProvider() {
  return (
    <AuthProvider>
      <Outlet />
    </AuthProvider>
  );
}

export function RequireAdmin() {
  const auth = useAdminAuth();
  const location = useLocation();

  if (auth.status === 'CHECKING') {
    return <div className="loading-page">관리자 세션을 확인하는 중입니다.</div>;
  }
  if (auth.status === 'UNAVAILABLE') {
    return (
      <section className="auth-status-page">
        <h1>관리 API에 연결할 수 없습니다.</h1>
        <p>FastAPI 연결 상태를 확인한 뒤 다시 시도해주세요.</p>
        <button className="button primary" type="button" onClick={auth.retrySession}>
          다시 확인
        </button>
      </section>
    );
  }
  if (auth.status === 'UNAUTHENTICATED') {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }
  return <Outlet />;
}

export function useAdminAuth(): AdminAuthContextValue {
  const value = useContext(AdminAuthContext);
  if (!value) throw new Error('useAdminAuth must be used within AdminAuthProvider');
  return value;
}
