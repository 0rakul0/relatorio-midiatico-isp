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
  PORTAL_NOTICIAS: 'Portal de notícias',
  WEB: 'Web',
  ACADEMIC: 'Artigo científico'
};
const originLabel = v => ORIGIN_LABEL[String(v || '').toUpperCase()] || 'Fonte';

let currentProject = null;
let currentTopic = null;
let projects = [];
let conversation = [];
let currentConversationId = null;
let conversationHistory = [];

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
const historyPanel = document.getElementById('chat-history-panel');
const historyList = document.getElementById('chat-history-list');

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
     <p>O acervo disponível tem ${total} notícias validadas em ${scopeText}. Eu consulto primeiro essa base e, quando necessário, o agente pode complementar com Web, vídeos ou arXiv.</p>`
  );
}

function renderInlineMarkdown(value) {
  return esc(value)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/__([^_]+)__/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\[F(\d+)\]/g, '<span class="chat-inline-ref">[F$1]</span>');
}

function renderMarkdown(value) {
  const normalized = String(value || '')
    .replace(/\\([*_#\[\]])/g, '$1')
    .replace(/\r\n/g, '\n');
  const lines = normalized.split('\n');
  const html = [];
  let listType = null;

  const closeList = () => {
    if (listType) {
      html.push(`</${listType}>`);
      listType = null;
    }
  };

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) {
      closeList();
      continue;
    }

    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      closeList();
      const level = Math.min(4, heading[1].length + 2);
      html.push(`<h${level}>${renderInlineMarkdown(heading[2])}</h${level}>`);
      continue;
    }

    const unordered = line.match(/^[-*]\s+(.+)$/);
    if (unordered) {
      if (listType !== 'ul') {
        closeList();
        listType = 'ul';
        html.push('<ul>');
      }
      html.push(`<li>${renderInlineMarkdown(unordered[1])}</li>`);
      continue;
    }

    const ordered = line.match(/^\d+[.)]\s+(.+)$/);
    if (ordered) {
      if (listType !== 'ol') {
        closeList();
        listType = 'ol';
        html.push('<ol>');
      }
      html.push(`<li>${renderInlineMarkdown(ordered[1])}</li>`);
      continue;
    }

    closeList();
    html.push(`<p>${renderInlineMarkdown(line)}</p>`);
  }

  closeList();
  return html.join('');
}

function renderAnswer(state) {
  const body = renderMarkdown(state.answer || '');
  let sources = '';

  const allSources = state.sources || [];
  if (allSources.length) {
    const corpusSources = allSources.filter(s => (s.source_scope || 'CORPUS') !== 'EXTERNAL');
    const externalSources = allSources.filter(s => s.source_scope === 'EXTERNAL');

    const renderSourceItems = rows => rows.map((s, index) => {
      const isExternal = s.source_scope === 'EXTERNAL';
      const reference = String(s.reference || `${isExternal ? 'W' : 'F'}${index + 1}`);
      const rawTitle = String(s.title || s.source_name || s.domain || s.url || 'Fonte do acervo').trim() || 'Fonte do acervo';
      const title = s.url
        ? `<a href="${esc(s.url)}" target="_blank" rel="noreferrer"><strong>${esc(rawTitle)}</strong></a>`
        : `<strong>${esc(rawTitle)}</strong>`;
      const when = s.published_at ? ` · ${esc(s.published_at)}` : '';
      const via = s.search_source ? ` · via ${esc(s.search_source)}` : '';
      const tool = s.tool ? ` · ${esc({
        pesquisar_internet: 'busca web',
        pesquisar_videos: 'busca de vídeos',
        pesquisar_artigos_arxiv: 'arXiv'
      }[s.tool] || s.tool)}` : '';
      const origin = esc(originLabel(s.media_origin));
      const source = esc(s.domain || s.source_name || (isExternal ? 'Fonte externa' : 'Acervo validado'));
      return `<li class="chat-source-item"><span class="chat-source-ref">${esc(reference)}</span><div>${title}<br><span class="note">${source}${when}${origin ? ' · ' + origin : ''}${via}${tool}</span></div></li>`;
    }).join('');

    const groups = [];
    if (corpusSources.length) {
      groups.push(`<div class="chat-source-group"><span class="chat-source-label">Acervo validado</span><ul>${renderSourceItems(corpusSources)}</ul></div>`);
    }
    if (externalSources.length) {
      const label = state.external_source_mode === 'consulted'
        ? 'Pesquisa externa consultada'
        : 'Pesquisa externa usada';
      groups.push(`<div class="chat-source-group"><span class="chat-source-label">${esc(label)}</span><ul>${renderSourceItems(externalSources)}</ul></div>`);
    }

    sources = `<details class="chat-sources" open><summary>Fontes e evidências (${allSources.length})</summary>${groups.join('')}</details>`;
  }

  const contextNote = state.corpus_size
    ? `<p class="note">Acervo: ${state.corpus_size} notícias validadas · contexto local recuperado: ${state.context_size || 0}.</p>`
    : '';

  const toolNote = state.tools_used?.length
    ? `<p class="note chat-tool-note">Ferramentas acionadas: ${esc(state.tools_used.map(t => ({
        pesquisar_internet: 'Web',
        pesquisar_videos: 'YouTube/Vídeos',
        pesquisar_artigos_arxiv: 'arXiv'
      }[t] || t)).join(', '))}.</p>`
    : '';

  appendBubble('assistant', `<div class="chat-answer">${body}</div>${sources}${contextNote}${toolNote}`);
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
      body: JSON.stringify({
        messages: conversation.slice(-50),
        conversation_id: currentConversationId
      })
    });
    currentConversationId = state.conversation_id || currentConversationId;
    conversation.push({ role: 'assistant', content: state.answer });
    renderAnswer(state);
    await loadConversationHistory(false);
  } catch (err) {
    appendBubble('assistant', `<p>${esc(err.message)}</p>`);
  } finally {
    sendBtn.disabled = false;
    progress.classList.add('hidden');
    input.focus();
  }
}

function resetConversation(showWelcome = true, clearId = true) {
  conversation = [];
  if (clearId) currentConversationId = null;
  log.innerHTML = '';
  if (showWelcome) renderWelcome();
  renderConversationHistory();
}

function conversationTime(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return new Intl.DateTimeFormat('pt-BR', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  }).format(date);
}

function renderConversationHistory() {
  if (!historyPanel || !historyList) return;
  historyPanel.classList.toggle('hidden', !currentProject);

  if (!currentProject) {
    historyList.innerHTML = '';
    return;
  }

  if (!conversationHistory.length) {
    historyList.innerHTML = '<div class="chat-history-empty">Nenhuma conversa salva neste tema ainda.</div>';
    return;
  }

  historyList.innerHTML = conversationHistory.map(item => {
    const current = String(item.id) === String(currentConversationId);
    const count = Number(item.message_count || 0);
    return `
      <div class="chat-history-entry${current ? ' current' : ''}" data-conversation-id="${item.id}">
        <button type="button" class="chat-history-open" data-open-conversation="${item.id}">
          <strong>${esc(item.title || 'Conversa')}</strong>
          <span>${esc(conversationTime(item.updated_at))} · ${count} mensagem(ns)</span>
        </button>
        <button type="button" class="chat-history-delete" data-delete-conversation="${item.id}" title="Excluir conversa" aria-label="Excluir conversa">×</button>
      </div>
    `;
  }).join('');

  historyList.querySelectorAll('[data-open-conversation]').forEach(button => {
    button.addEventListener('click', () => loadConversation(button.dataset.openConversation));
  });

  historyList.querySelectorAll('[data-delete-conversation]').forEach(button => {
    button.addEventListener('click', async event => {
      event.stopPropagation();
      const id = button.dataset.deleteConversation;
      if (!confirm('Excluir esta conversa do histórico?')) return;
      try {
        await api(`/chat/conversations/${id}`, { method: 'DELETE' });
        const wasCurrent = String(currentConversationId) === String(id);
        if (wasCurrent) resetConversation(true, true);
        await loadConversationHistory(wasCurrent);
      } catch (err) {
        appendBubble('assistant', `<p>${esc(err.message)}</p>`);
      }
    });
  });
}

async function loadConversation(conversationId) {
  if (!conversationId) return;
  const selectedProject = currentProject;
  try {
    const state = await api(`/chat/conversations/${conversationId}`);
    if (selectedProject !== currentProject) return;

    currentConversationId = state.id;
    conversation = [];
    log.innerHTML = '';
    renderWelcome();

    for (const message of (state.messages || [])) {
      conversation.push({ role: message.role, content: message.content });
      if (message.role === 'user') {
        renderUser(message.content);
      } else {
        renderAnswer({
          answer: message.content,
          sources: message.sources || [],
          ...(message.metadata || {})
        });
      }
    }
    renderConversationHistory();
    input.focus();
  } catch (err) {
    appendBubble('assistant', `<p>${esc(err.message)}</p>`);
  }
}

async function loadConversationHistory(openLatest = false) {
  if (!currentProject) {
    conversationHistory = [];
    renderConversationHistory();
    return;
  }

  const selectedProject = currentProject;
  try {
    const state = await api(`/chat/conversations?project_id=${encodeURIComponent(selectedProject)}`);
    if (selectedProject !== currentProject) return;
    conversationHistory = state.conversations || [];
    renderConversationHistory();

    if (openLatest) {
      if (conversationHistory.length) {
        await loadConversation(conversationHistory[0].id);
      } else {
        resetConversation(true, true);
      }
    }
  } catch (err) {
    conversationHistory = [];
    renderConversationHistory();
    appendBubble('assistant', `<p>Não foi possível carregar o histórico: ${esc(err.message)}</p>`);
  }
}

function startNewConversation() {
  resetConversation(true, true);
  input.focus();
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
              class="topic-option${p.scope === 'ALL' ? ' all-corpus' : ''}${selected ? ' selected' : ''}"
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
    ? 'Converse com todo o acervo validado disponível para sua conta. O agente usa o acervo primeiro e pode pesquisar externamente quando a pergunta exigir.'
    : `Converse sobre “${currentTopic.topic}”. A base reúne as notícias validadas das execuções desse tema; o agente pode complementar com Web, vídeos ou arXiv quando necessário.`;
}

async function selectProject(projectId) {
  const selected = projects.find(
    p => String(p.id) === String(projectId) && p.valid_items > 0
  );
  currentTopic = selected || null;
  currentProject = selected ? String(selected.id) : null;
  currentConversationId = null;
  conversationHistory = [];

  updatePickerSummary();
  log.innerHTML = '';
  conversation = [];

  input.disabled = !currentProject;
  sendBtn.disabled = !currentProject;
  newBtn.disabled = !currentProject;

  renderConversationHistory();
  if (currentProject) {
    await loadConversationHistory(true);
    input.focus();
  }
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

  await selectProject(preselect);
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

  newBtn.addEventListener('click', startNewConversation);

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
