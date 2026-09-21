// Chat com a base: conversa grounded no corpus validado do tema selecionado.
// Projetos repetidos do mesmo tema chegam agregados pelo backend.

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
  if (r.status === 401) {
    window.location.href = '/login';
    throw new Error('Sessão expirada. Entre de novo.');
  }
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

const ORIGIN_LABEL = {
  YOUTUBE: 'YouTube',
  REDE_SOCIAL: 'Rede social',
  PORTAL_NOTICIAS: 'Portal de notícias'
};
const originLabel = v => ORIGIN_LABEL[String(v || '').toUpperCase()] || 'Fonte';

let currentProject = null;
let currentTopic = null;
let projects = [];
let conversation = [];

const log = document.getElementById('chat-log');
const input = document.getElementById('chat-input');
const sendBtn = document.getElementById('chat-send');
const progress = document.getElementById('chat-progress');
const newBtn = document.getElementById('chat-new');

const picker = document.getElementById('topic-picker');
const trigger = document.getElementById('topic-trigger');
const menu = document.getElementById('topic-menu');
const search = document.getElementById('topic-search');
const list = document.getElementById('topic-list');
const triggerTitle = document.getElementById('topic-trigger-title');
const triggerMeta = document.getElementById('topic-trigger-meta');

const baseSummary = document.getElementById('chat-base-summary');
const baseItems = document.getElementById('base-items');
const baseRuns = document.getElementById('base-runs');

function plural(value, singular, pluralText) {
  return `${value} ${value === 1 ? singular : pluralText}`;
}

function appendBubble(role, html) {
  const div = document.createElement('div');
  div.className = `chat-bubble ${role}`;
  div.innerHTML = html;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function renderWelcome() {
  if (!currentTopic) return;
  const total = currentTopic.valid_items || 0;
  const groups = currentTopic.project_count || 1;
  const scopeText = currentTopic.scope === 'ALL'
    ? `${groups} projeto(s) com notícias validadas`
    : `${groups} coleta(s) reunida(s)`;
  appendBubble(
    'assistant',
    `<p><strong>Base “${esc(currentTopic.topic)}” pronta para consulta.</strong></p>
     <p>O acervo disponível tem ${total} notícias validadas em ${scopeText}. A cada pergunta eu recupero primeiro os documentos mais aderentes e só então envio esse contexto à LLM.</p>`
  );
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

  const contextNote = state.corpus_size
    ? `<p class="note">Acervo: ${state.corpus_size} notícias validadas · contexto recuperado nesta pergunta: ${state.context_size || 0}.</p>`
    : '';

  appendBubble('assistant', `<div class="chat-answer">${body}</div>${sources}${contextNote}`);
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
    const endpoint = currentProject === 'all'
      ? '/chat/all/ask'
      : `/chat/${currentProject}/ask`;
    const state = await api(endpoint, {
      method: 'POST',
      body: JSON.stringify({ messages: conversation })
    });
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

function resetConversation(showWelcome = true) {
  conversation = [];
  log.innerHTML = '';
  if (showWelcome) renderWelcome();
}

function closePicker() {
  menu.classList.add('hidden');
  trigger.setAttribute('aria-expanded', 'false');
}

function openPicker() {
  menu.classList.remove('hidden');
  trigger.setAttribute('aria-expanded', 'true');
  search.value = '';
  renderTopicList('');
  setTimeout(() => search.focus(), 0);
}

function renderTopicList(filterText = '') {
  const q = filterText.trim().toLocaleLowerCase('pt-BR');
  const filtered = projects.filter(p => !q || p.topic.toLocaleLowerCase('pt-BR').includes(q));

  if (!filtered.length) {
    list.innerHTML = '<div class="topic-empty">Nenhum tema encontrado.</div>';
    return;
  }

  list.innerHTML = filtered.map(p => {
    const selected = String(p.id) === String(currentProject);
    const status = p.scope === 'ALL'
      ? 'Acervo completo'
      : (p.generated_at ? 'Relatório gerado' : 'Base coletada');
    const runs = p.project_count || 1;
    const groupLabel = p.scope === 'ALL'
      ? plural(runs, 'projeto com itens', 'projetos com itens')
      : plural(runs, 'coleta reunida', 'coletas reunidas');
    return `
      <button type="button"
              class="topic-option${selected ? ' selected' : ''}"
              data-project-id="${p.id}"
              role="option"
              aria-selected="${selected}">
        <span class="topic-option-main">
          <strong>${esc(p.topic)}</strong>
          <span>${plural(p.valid_items, 'notícia validada', 'notícias validadas')} · ${groupLabel}</span>
        </span>
        <span class="topic-option-status">${esc(status)}</span>
      </button>
    `;
  }).join('');

  list.querySelectorAll('.topic-option').forEach(button => {
    button.addEventListener('click', () => {
      selectProject(button.dataset.projectId);
      closePicker();
    });
  });
}

function updatePickerSummary() {
  if (!currentTopic) {
    triggerTitle.textContent = 'Nenhuma base disponível';
    triggerMeta.textContent = 'Gere um relatório para começar.';
    baseSummary.classList.add('hidden');
    return;
  }

  const runs = currentTopic.project_count || 1;
  const allScope = currentTopic.scope === 'ALL';
  triggerTitle.textContent = currentTopic.topic;
  triggerMeta.textContent = allScope
    ? `${plural(currentTopic.valid_items, 'notícia validada', 'notícias validadas')} · ${plural(runs, 'projeto com itens', 'projetos com itens')}`
    : `${plural(currentTopic.valid_items, 'notícia validada', 'notícias validadas')} · ${plural(runs, 'coleta reunida', 'coletas reunidas')}`;
  baseItems.textContent = currentTopic.valid_items;
  baseRuns.textContent = runs;
  const runsLabel = document.getElementById('base-runs-label');
  if (runsLabel) runsLabel.textContent = allScope ? 'projetos com itens' : 'coletas reunidas';
  baseSummary.classList.remove('hidden');

  document.getElementById('chat-scope').textContent = allScope
    ? 'Converse com todo o acervo validado disponível para sua conta. Nenhuma nova busca na web é feita.'
    : `Converse sobre “${currentTopic.topic}”. A base reúne todas as notícias validadas encontradas nas execuções desse mesmo tema, sem novas buscas na web.`;
}

function selectProject(projectId) {
  const selected = projects.find(
    p => String(p.id) === String(projectId) && p.valid_items > 0
  );
  currentTopic = selected || null;
  currentProject = selected ? String(selected.id) : null;

  updatePickerSummary();
  resetConversation(Boolean(currentProject));

  input.disabled = !currentProject;
  sendBtn.disabled = !currentProject;
  newBtn.disabled = !currentProject;

  if (currentProject) input.focus();
}

async function loadProjects() {
  const data = await api('/chat/projects');
  const topicProjects = (data.projects || []).filter(p => p.valid_items > 0);
  projects = data.all_corpus
    ? [data.all_corpus, ...topicProjects]
    : topicProjects;

  if (!projects.length) {
    currentProject = null;
    currentTopic = null;
    updatePickerSummary();
    appendBubble('assistant', '<p>Nenhum tema tem notícias validadas ainda. Gere um relatório no <a href="/">painel</a> e volte aqui.</p>');
    input.disabled = true;
    sendBtn.disabled = true;
    newBtn.disabled = true;
    return;
  }

  const preselect = data.last_active_project_id && projects.some(
    p => String(p.id) === String(data.last_active_project_id)
  )
    ? String(data.last_active_project_id)
    : String(projects[0].id);

  selectProject(preselect);
  renderTopicList('');
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

  document.getElementById('chat-form').addEventListener('submit', e => {
    e.preventDefault();
    sendQuestion();
  });

  newBtn.addEventListener('click', () => resetConversation(true));

  trigger.addEventListener('click', () => {
    menu.classList.contains('hidden') ? openPicker() : closePicker();
  });

  search.addEventListener('input', () => renderTopicList(search.value));

  document.addEventListener('click', event => {
    if (!picker.contains(event.target)) closePicker();
  });

  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') closePicker();
  });

  loadProjects().catch(err => appendBubble('assistant', `<p>${esc(err.message)}</p>`));
}
