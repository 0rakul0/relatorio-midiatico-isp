// Tela de login: autentica e vai para o index.
const $ = s => document.querySelector(s);

async function doLogin() {
  const email = $('#login-email').value.trim(), password = $('#login-pass').value;
  if (!email || !password) { $('#login-msg').textContent = 'Preencha e-mail e senha.'; return; }
  $('#login-msg').textContent = 'Entrando…';
  try {
    const d = await goTrue('token?grant_type=password', { email, password });
    saveAuth({ access_token: d.access_token, refresh_token: d.refresh_token, email: d.user?.email || email, expires_at: Date.now() + (d.expires_in || 3600) * 1000 });
    window.location.href = '/';
  } catch (e) { $('#login-msg').textContent = `Erro: ${e.message}`; }
}

async function doSignup() {
  const email = $('#login-email').value.trim(), password = $('#login-pass').value;
  if (!email || !password) { $('#login-msg').textContent = 'Preencha e-mail e senha.'; return; }
  if (password.length < 6) { $('#login-msg').textContent = 'A senha precisa de ao menos 6 caracteres.'; return; }
  $('#login-msg').textContent = 'Criando conta…';
  try {
    const d = await goTrue('signup', { email, password });
    if (d?.session?.access_token) {
      saveAuth({ access_token: d.session.access_token, refresh_token: d.session.refresh_token, email, expires_at: Date.now() + (d.session.expires_in || 3600) * 1000 });
      window.location.href = '/';
    } else {
      $('#login-msg').textContent = 'Conta criada. Se pedirem confirmação, verifique seu e-mail e entre.';
    }
  } catch (e) { $('#login-msg').textContent = `Erro: ${e.message}`; }
}

// Já logado? Vai direto.
if (authState()) window.location.href = '/';
$('#login-go').onclick = doLogin;
$('#login-signup').onclick = doSignup;
$('#login-pass').addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });
