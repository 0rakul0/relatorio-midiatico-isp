// Login local; cadastro de usuarios e feito pelo administrador no terminal.
const $ = selector => document.querySelector(selector);

async function doLogin() {
  const email = $('#login-email').value.trim();
  const password = $('#login-pass').value;
  if (!email || !password) {
    $('#login-msg').textContent = 'Preencha e-mail e senha.';
    return;
  }
  $('#login-go').disabled = true;
  $('#login-msg').textContent = 'Entrando…';
  try {
    const response = await fetch('/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    });
    const data = await response.json().catch(() => null);
    if (!response.ok) throw new Error(data?.detail || `Login HTTP ${response.status}`);
    saveAuth({
      access_token: data.access_token,
      email: data.email || email,
      expires_at: Date.now() + (data.expires_in || 43200) * 1000,
    });
    window.location.href = '/';
  } catch (error) {
    $('#login-msg').textContent = `Erro: ${error.message}`;
  } finally {
    $('#login-go').disabled = false;
  }
}

bootstrapAuth().then(session => { if (session) window.location.href = '/'; });
$('#login-go').onclick = doLogin;
$('#login-pass').addEventListener('keydown', event => {
  if (event.key === 'Enter') doLogin();
});
