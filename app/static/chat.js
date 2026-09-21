// Chat com a base: conversa grounded no corpus validado do projeto ativo.
// O historico vive na sessao (memoria do navegador); o servidor e stateless.

const esc = v => String(v ?? '').replace(/[&<>'"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]));

function guardAuth() {
  if (!authState()) { window.location.href = '/login'; return false; }
  return true;
}

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { 'Content-Type': 'application/json', ...authHeaders() }, ...opts });
  if (r.status === 401 && await refreshSession()) {
    const r2 = await fetch(path, { headers: { 'Content-Type': 'application/json', ...authHeaders() }, ...opts });
    return handleApi(r2);
  }
  if (r.status === 401) { window.location.href = '/login'; throw new Error('Sessão expirada. Entre de novo.'); }
  return handleApi(r);
}

async function handleApi(r) {
  if (r.status === 204) return null;
  const raw = await r.text();
  let d = null;
  try { d = raw ? JSON.parse(raw) : null; } catch { d = null; }
  if (!r.ok) throw new Error(d?.detail || raw || `Erro HTTP ${r.status}`);
  return d;
}

const ORIGIN_LABEL = { YOUTUBE: 'YouTube', REDE_SOCIAL: 'Rede social', PORTAL_NOTICIAS: 'Portal de notícias' };
const originLabel = v => ORIGIN_LABEL[String(v || '').toUpperCase()] || 'Fonte';

let currentProject = null;
let conversation = [];
const log = document.getElementById('chat-log');
const input = document.getElementById('chat-input');
const sendBtn = document.getElementById('chat-send');
const progress = document.getElementById('chat-progress');
const selector = document.getElementById('chat-project');

function appendBubble(role, html) {
  const div = document.createElement('div');
  div.className = `chat-bubble ${role}`;
  div.innerHTML = html;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function renderAnswer(state) {
  const paragraphs = String(state.answer || '').split(/\n{2,}|\r\n{2,}/);
  const body = paragraphs.map(p => `<p>${esc(p).replace(/\n/g, '<br>')}</p>`).join('');
  let sources = '';
  if (state.sources?.length) {
    const items = state.sources.map(s => {
      const link = s.url ? `<a href="${esc(s.url)}" target="_blank" rel="noreferrer">Abrir fonte</a>` : '';
      const when = s.published_at ? ` · ${esc(s.published_at)}` : '';
      return `<li><strong>${esc(s.title)}</strong><br><span class="note">${esc(s.domain || s.source_name || '')}${when} · ${esc(originLabel(s.media_origin))} · via ${esc(s.search_source || 'unknown')}${link ? ' · ' + link : ''}</span></li>`;
    }).join('');
    sources = `<details class="chat-sources"><summary>Fontes usadas (${state.sources.length})</summary><ul>${items}</ul></details>`;
  }
  const corpusNote = state.corpus_size ? `<p class="note">Contexto: ${state.corpus_size} itens validados.</p>` : '';
  appendBubble('assistant', `<div class="chat-answer">${body}</div>${sources}${corpusNote}`);
}

function renderUser(text) {
  appendBubble('user', `<p>${esc(text)}</p>`);
}

async function sendQuestion() {
  const text = input.value.trim();
  if (!text || !currentProject) return;
  input.value = '';
  renderUser(text);
  conversation.push({ role: 'user', content: text });
  sendBtn.disabled = true;
  progress.classList.remove('hidden');
  try {
    const state = await api(`/chat/${currentProject}/ask`, { method: 'POST', body: JSON.stringify({ messages: conversation }) });
    conversation.push({ role: 'assistant', content: state.answer });
    renderAnswer(state);
  } catch (err) {
    appendBubble('assistant', `<p>${esc(err.message)}</p>`);
  } finally {
    sendBtn.disabled = false;
    progress.classList.add('hidden');
    input.focus();
  }
}

function resetConversation() {
  conversation = [];
  log.innerHTML = '';
}

async function loadProjects() {
  const data = await api('/chat/projects');
  selector.innerHTML = '';
  const available = data.projects.filter(p => p.valid_items > 0);
  if (!available.length) {
    selector.innerHTML = '<option value="">Nenhum projeto com corpus coletado</option>';
    currentProject = null;
    appendBubble('assistant', '<p>Nenhum projeto tem itens validados ainda. Gere um relatório no <a href="/">painel</a> e volte aqui.</p>');
    return;
  }
  for (const p of data.projects) {
    const opt = document.createElement('option');
    opt.value = p.id;
    opt.textContent = `${p.topic} — ${p.valid_items} itens${p.generated_at ? ' · relatório gerado' : ''}`;
    if (p.valid_items > 0) opt.disabled = false;
    selector.appendChild(opt);
  }
  const preselect = data.last_active_project_id && available.some(p => p.id === data.last_active_project_id)
    ? data.last_active_project_id
    : available[0].id;
  selector.value = preselect;
  selectProject();
}

function selectProject() {
  const value = Number(selector.value);
  currentProject = Number.isFinite(value) && value > 0 ? value : null;
  resetConversation();
  if (currentProject) {
    const opt = selector.selectedOptions[0];
    document.getElementById('chat-scope').textContent = `Converse sobre “${opt?.textContent?.split(' — ')[0] || ''}”. As respostas usam somente os itens validados — sem novas buscas na web.`;
  }
  input.disabled = !currentProject;
  sendBtn.disabled = !currentProject;
}

if (!guardAuth()) {
  // guardAuth redireciona.
} else {
  const s = authState();
  document.getElementById('chat-auth').textContent = `${s.email || ''} · `;
  const logout = document.createElement('button');
  logout.type = 'button';
  logout.className = 'secondary';
  logout.textContent = 'Sair';
  logout.onclick = () => { saveAuth(null); window.location.href = '/login'; };
  document.getElementById('chat-auth').appendChild(logout);

  document.getElementById('chat-form').addEventListener('submit', e => { e.preventDefault(); sendQuestion(); });
  selector.addEventListener('change', selectProject);
  loadProjects().catch(err => appendBubble('assistant', `<p>${esc(err.message)}</p>`));
}