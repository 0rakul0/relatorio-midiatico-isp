// Helpers de autenticação compartilhados (login.html e index).
const AUTH_KEY = 'isp_auth_v1';
let supaCfg = null;
const authState = () => { try { return JSON.parse(localStorage.getItem(AUTH_KEY) || 'null'); } catch { return null; } };
const saveAuth = s => { if (s) localStorage.setItem(AUTH_KEY, JSON.stringify(s)); else localStorage.removeItem(AUTH_KEY); };
const authHeaders = () => { const s = authState(); return s?.access_token ? { Authorization: `Bearer ${s.access_token}` } : {}; };
async function supaConfig() { if (supaCfg) return supaCfg; supaCfg = await (await fetch('/auth/config')).json(); return supaCfg; }
async function bootstrapAuth() {
  const cfg = await supaConfig();
  if (cfg.local_auth_bypass) {
    const localSession = { access_token: 'local-development', email: 'local@development.invalid', local: true };
    saveAuth(localSession);
    return localSession;
  }
  return authState();
}
async function goTrue(path, body) {
  const cfg = await supaConfig();
  if (cfg.local_auth_bypass) throw new Error('O login local está ativo.');
  const r = await fetch(`${cfg.supabase_url}/auth/v1/${path}`, { method: 'POST', headers: { 'Content-Type': 'application/json', apikey: cfg.supabase_key }, body: JSON.stringify(body) });
  const d = await r.json().catch(() => null);
  if (!r.ok) throw new Error(d?.msg || d?.message || d?.error_description || `Auth HTTP ${r.status}`);
  return d;
}
async function refreshSession() {
  const s = authState();
  if (s?.local) return true;
  if (!s?.refresh_token) return false;
  try {
    const d = await goTrue('token?grant_type=refresh_token', { refresh_token: s.refresh_token });
    saveAuth({ access_token: d.access_token, refresh_token: d.refresh_token ?? s.refresh_token, email: s.email, expires_at: Date.now() + (d.expires_in || 3600) * 1000 });
    return true;
  } catch { saveAuth(null); return false; }
}
