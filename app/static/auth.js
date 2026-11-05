// Autenticacao local: tokens emitidos pelo proprio servidor FastAPI.
const AUTH_KEY = 'isp_auth_local_v1';
const authState = () => {
  try {
    const session = JSON.parse(localStorage.getItem(AUTH_KEY) || 'null');
    if (!session?.access_token) return null;
    if (session.expires_at && Date.now() >= session.expires_at) {
      localStorage.removeItem(AUTH_KEY);
      return null;
    }
    return session;
  } catch { return null; }
};
const saveAuth = session => {
  if (session) localStorage.setItem(AUTH_KEY, JSON.stringify(session));
  else localStorage.removeItem(AUTH_KEY);
};
const authHeaders = () => {
  const session = authState();
  return session ? { Authorization: `Bearer ${session.access_token}` } : {};
};
async function bootstrapAuth() {
  return authState();
}
async function refreshSession() {
  // Sessao local: apos 12 horas, solicitar novo login (sem refresh token).
  saveAuth(null);
  return false;
}
