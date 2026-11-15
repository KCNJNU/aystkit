import { useQuery, useQueryClient } from '@tanstack/react-query';
import { createContext, useCallback, useContext, useEffect, useMemo, type ReactNode } from 'react';
import { api, ApiError, setCsrfToken, type AuthUser, type UserRole } from '../services/api';

type AuthValue = { user?: AuthUser; loading: boolean; authenticated: boolean; login: (email: string, password: string) => Promise<AuthUser>; logout: () => Promise<void>; refresh: () => Promise<void>; hasRole: (...roles: UserRole[]) => boolean };
const AuthContext = createContext<AuthValue | null>(null);
const sessionKey = ['auth', 'session'] as const;

export function AuthProvider({ children }: { children: ReactNode }) {
  const client = useQueryClient();
  const session = useQuery({ queryKey: sessionKey, queryFn: async ({ signal }) => { try { const result = await api.session(signal); setCsrfToken(result.csrfToken); return result.user; } catch (error) { if (error instanceof ApiError && [401, 403].includes(error.status)) { setCsrfToken(); return null; } throw error; } }, retry: false, staleTime: 5 * 60_000 });
  const clearSession = useCallback(() => {
    setCsrfToken();
    void client.cancelQueries();
    // Keep the observed session query alive so route guards receive the change.
    // Clearing the whole cache leaves mounted observers attached to a removed query.
    client.removeQueries({ predicate: query => !(query.queryKey.length === 2 && query.queryKey[0] === 'auth' && query.queryKey[1] === 'session') });
    client.getMutationCache().clear();
    client.setQueryData(sessionKey, null);
  }, [client]);
  const login = useCallback(async (email: string, password: string) => { await client.cancelQueries({ queryKey: sessionKey, exact: true }); const result = await api.login(email, password); setCsrfToken(result.csrfToken); client.setQueryData(sessionKey, result.user); return result.user; }, [client]);
  const logout = useCallback(async () => { try { await api.logout(); } finally { clearSession(); } }, [clearSession]);
  const refresh = useCallback(async () => { await client.invalidateQueries({ queryKey: sessionKey }); }, [client]);
  useEffect(() => { window.addEventListener('auth-expired', clearSession); return () => window.removeEventListener('auth-expired', clearSession); }, [clearSession]);
  const user = session.data ?? undefined;
  const value = useMemo<AuthValue>(() => ({ user, loading: session.isLoading, authenticated: Boolean(user), login, logout, refresh, hasRole: (...roles) => Boolean(user && roles.includes(user.role)) }), [user, session.isLoading, login, logout, refresh]);
  if (session.error instanceof ApiError) console.warn('Session check failed', session.error.message);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components
export function useAuth() { const value = useContext(AuthContext); if (!value) throw new Error('AuthProvider is missing'); return value; }

// Components are also rendered in isolated unit tests. Permission-aware controls
// can use this hook without requiring every small component test to construct a
// session provider; routed application pages always receive the real context.
// eslint-disable-next-line react-refresh/only-export-components
export function useOptionalAuth() { return useContext(AuthContext); }
