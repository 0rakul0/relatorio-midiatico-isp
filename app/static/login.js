// Autenticacao local: login e autocadastro no SQLite.
const $ = selector => document.querySelector(selector);
const loginGo = $('#login-go');
const signupGo = $('#signup-go');

function setMessage(target, message, error = false) {
  const element = $(target);
  element.textContent = message;
  element.classList.toggle('error', Boolean(error));
  element.classList.toggle('success', Boolean(message) && !error);
}

function setMode(mode) {
  const signup = mode === 'signup' && !$('#mode-signup').classList.contains('hidden');
  $('#login-view').classList.toggle('hidden', signup);
  $('#signup-view').classList.toggle('hidden', !signup);
  $('#mode-login').setAttribute('aria-pressed', String(!signup));
  $('#mode-signup').setAttribute('aria-pressed', String(signup));
  setMessage('#login-msg', '');
  setMessage('#signup-msg', '');
  (signup ? $('#signup-email') : $('#login-email')).focus();
}

function saveSession(data, email) {
  saveAuth({
    access_token: data.access_token,
    email: data.email || email,
    expires_at: Date.now() + (data.expires_in || 43200) * 1000,
  });
  window.location.assign('/');
}

async function authRequest(path, body) {
  const response = await fetch(path, {
    method: 'POST',
    cache: 'no-store',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    let message = data?.detail || `Erro HTTP ${response.status}`;
    if (Array.isArray(message)) message = 'Confira os dados preenchidos.';
    throw new Error(message);
  }
  return data;
}

async function doLogin(event) {
  event.preventDefault();
  const email = $('#login-email').value.trim();
  const password = $('#login-pass').value;
  if (!email || !password) return setMessage('#login-msg', 'Preencha e-mail e senha.', true);
  loginGo.disabled = true;
  setMessage('#login-msg', 'Entrando...');
  try {
    const result = await authRequest('/auth/login', {email, password});
    saveSession(result, email);
  } catch (error) {
    setMessage('#login-msg', `Erro: ${error.message}`, true);
  } finally {
    loginGo.disabled = false;
  }
}

async function doSignup(event) {
  event.preventDefault();
  const email = $('#signup-email').value.trim();
  const password = $('#signup-pass').value;
  const confirmed = $('#signup-confirm').value;
  if (!email || !password || !confirmed) {
    return setMessage('#signup-msg', 'Preencha todos os campos.', true);
  }
  if (password.length < 12) {
    return setMessage('#signup-msg', 'Use uma senha de pelo menos 12 caracteres.', true);
  }
  if (password !== confirmed) {
    return setMessage('#signup-msg', 'As senhas informadas não coincidem.', true);
  }
  signupGo.disabled = true;
  setMessage('#signup-msg', 'Criando sua conta...');
  try {
    const result = await authRequest('/auth/signup', {email, password});
    setMessage('#signup-msg', 'Conta criada! Abrindo sua área de relatórios...');
    $('#signup-pass').value = '';
    $('#signup-confirm').value = '';
    saveSession(result, email);
  } catch (error) {
    setMessage('#signup-msg', `Erro: ${error.message}`, true);
  } finally {
    signupGo.disabled = false;
  }
}

async function loadSignupConfig() {
  try {
    const response = await fetch('/auth/config', {cache: 'no-store'});
    if (!response.ok) return;
    const data = await response.json();
    $('#mode-signup').classList.toggle('hidden', !data.signup_enabled);
    if (!data.signup_enabled && !$('#signup-view').classList.contains('hidden')) {
      setMode('login');
    }
  } catch {
    // Login continua funcionando quando a configuracao publica esta indisponivel.
  }
}

$('#login-form').addEventListener('submit', doLogin);
$('#signup-form').addEventListener('submit', doSignup);
$('#mode-login').addEventListener('click', () => setMode('login'));
$('#mode-signup').addEventListener('click', () => setMode('signup'));
bootstrapAuth().then(session => {
  if (session) window.location.assign('/');
  else loadSignupConfig();
});
