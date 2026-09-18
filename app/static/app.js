
const $=s=>document.querySelector(s);
const esc=v=>String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const api=async(path,opts={})=>{const r=await fetch(path,{headers:{'Content-Type':'application/json'},...opts});if(r.status===204)return null;const raw=await r.text();let d;try{d=raw?JSON.parse(raw):null}catch{d=null}if(!r.ok)throw new Error(d?.detail||raw||`Erro HTTP ${r.status}`);return d};
const table=(headers,rows)=>`<div class="table-wrap"><table><thead><tr>${headers.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(cell=>`<td>${cell}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
let currentProjectId=null,currentRunId=null,pollTimer=null;

api('/health').then(d=>$('#api-status').textContent=`API conectada · ${d.version}`).catch(()=>$('#api-status').textContent='API indisponível');

function qaBadge(qa){const status=qa?.status||'PENDING';const cls=status==='APPROVED'?'ok':status==='REJECTED'?'bad':'warn';return `<span class="badge ${cls}">${esc(status)}</span>`}
function factStatus(v){const map={CONFIRMED:'Confirmado',PARTIALLY_CONFIRMED:'Confirmação parcial',SOURCE_CONFLICT:'Conflito entre fontes',NOT_FOUND_IN_SAMPLE:'Não localizado na amostra'};return map[v]||v||'N/D'}
function scopeLabel(v){return v===true?'núcleo principal':v===false?'caso relacionado':'escopo pendente'}
function render(result){
  const d=result.report,p=result.project,m=result.metrics,qa=result.qa||{};
  const facts=result.fact_events||[];
  const factRows=facts.map(x=>[
    esc(x.subject_name||'Não localizado'),
    esc([x.institution,x.rank_or_role,x.unit].filter(Boolean).join(' / ')||'Não localizado'),
    esc(x.death_date||x.event_date||'Não localizada'),
    esc(x.cause||x.circumstance||'Não localizado'),
    esc([x.address,x.neighborhood,x.city,x.state].filter(Boolean).join(', ')||'Não localizado'),
    esc([x.death_place_name,x.death_address,x.death_neighborhood,x.death_city,x.death_state].filter(Boolean).join(', ')||'Não localizado'),
    `<span class="${x.resolution_status==='SOURCE_CONFLICT'?'fact-conflict':x.resolution_status==='CONFIRMED'?'fact-confirmed':''}">${esc(factStatus(x.resolution_status))}</span><br><small>${esc(scopeLabel(x.primary_scope))}${x.conflict_fields?.length?` · conflito: ${esc(x.conflict_fields.join(', '))}`:''}</small>`
  ]);
  const axes=(d.thematic_axes||[]).map(x=>[esc(x.axis),esc(x.anchor_data),esc(x.coverage)]);
  const risks=(d.risk_assessment||[]).map(x=>[esc(x.dimension),esc(x.assessment),esc(x.evidence)]);
  const kit=(d.press_kit||[]).map(x=>[esc(x.product),esc(x.purpose)]);
  const corpus=(result.corpus||[]).map((x,i)=>[String(i+1),esc(x.published_at||x.published_year||'N/D'),esc(x.source||x.domain||'Fonte aberta'),esc(x.title),`<a href="${esc(x.url)}" target="_blank" rel="noreferrer">${esc(x.url)}</a>`]);
  const windowLabel=(a,b,empty='Não delimitado')=>a&&b?`${esc(a)} a ${esc(b)}`:a?esc(a):b?esc(b):empty;
  const factLayerEnabled=!!p.execution_flags?.enable_fact_layer;
  const factSection=factLayerEnabled?`<h2>Camada de Fatos Verificados</h2><p>${esc(d.fact_layer_intro||'')}</p>${facts.length?table(['Pessoa','Vínculo','Data','Fato / causa','Local do fato','Local da morte','Situação'],factRows):'<p>Nenhum fato individual foi suficientemente estruturado na amostra factual.</p>'}`:'';
  const contextMeta=p.project_type==='INSTITUTIONAL_PRODUCT'?`<div><b>Lançamento</b><br>${esc(p.launch_date||'Não confirmado')}</div>`:`<div><b>Janela dos fatos</b><br>${windowLabel(p.event_start,p.event_end)}</div>`;
  $('#report-layout').classList.remove('hidden');
  $('#report').innerHTML=`
    <div class="kicker">Relatório de repercussão midiática</div>
    <h1>${esc(d.title)}</h1><p class="interpretive">${esc(d.interpretive_title)}</p><p class="subtitle">${esc(d.subtitle)}</p>
    <div class="report-meta"><div><b>Instituição</b><br>${esc(p.institution)}</div>${contextMeta}<div><b>Janela de repercussão</b><br>${windowLabel(p.collection_start,p.collection_end,'Busca temática')}</div><div><b>QA</b><br>${qaBadge(qa)}</div></div>
    <h2>Resumo Executivo</h2><div class="summary"><p>${esc(d.executive_summary)}</p></div>
    ${factSection}
    <h2>Abertura</h2><p>${esc(d.opening)}</p>
    <h2>I. Panorama da Repercussão</h2><p>${esc(d.panorama)}</p>
    <div class="table-wrap"><table><tbody><tr><th>Itens validados</th><td>${esc(m.valid_items)}</td><th>Veículos</th><td>${esc(m.unique_vehicles)}</td><th>Eventos factuais</th><td>${esc(m.facts?.events||0)}</td></tr></tbody></table></div>
    <h2>II. Enquadramento Dominante</h2><p>${esc(d.dominant_framing)}</p>
    <h2>III. Um Estudo, Muitas Pautas</h2>${table(['Eixo temático','Dado-âncora','Cobertura'],axes)}
    <h2>IV. Recorte de Maior Rendimento Jornalístico</h2><p>${esc(d.highest_yield)}</p>
    <h2>V. Camada Institucional e Disputa de Narrativa</h2><p>${esc(d.institutional_narrative)}</p>
    <h2>VI. Avaliação: Alcance, Profundidade e Riscos</h2>${table(['Dimensão','Avaliação','Evidência'],risks)}
    <h2>VII. Recomendações e Kit de Imprensa</h2><ul>${(d.recommendations||[]).map(x=>`<li>${esc(x)}</li>`).join('')}</ul>${table(['Produto','Finalidade'],kit)}
    <h2>VIII. Síntese</h2><p>${esc(d.synthesis)}</p>
    <h2>Anexo A - Nota Metodológica</h2><p>${esc(d.methodological_note)}</p>
    <h2>Corpus Auditável</h2>${corpus.length?table(['#','Data','Fonte','Título','URL'],corpus):'<p>Nenhum item validado na amostra para a janela de repercussão.</p>'}
    ${qa.findings?.length?`<h2>Achados de QA</h2>${table(['Severidade','Código','Mensagem'],qa.findings.map(x=>[esc(x.severity),esc(x.code),esc(x.message)]))}`:''}
    <div class="footer-note">Fato, fonte factual e item de repercussão são tratados como objetos distintos. Fontes posteriores podem confirmar um fato sem aumentar a repercussão do mês.</div>`;
  buildToc();
}

function buildToc(){const nav=$('#toc-links');nav.innerHTML='';document.querySelectorAll('#report h2').forEach((h,i)=>{h.id=`sec-${i}`;const a=document.createElement('a');a.href=`#${h.id}`;a.textContent=h.textContent;nav.appendChild(a)})}
function stageClass(status){return ({PENDING:'pending',RUNNING:'running',DONE:'done',SKIPPED:'skipped',FAILED:'failed',CANCELLED:'cancelled'})[status]||'pending'}
function stageLabel(status){return ({PENDING:'Aguardando',RUNNING:'Executando',DONE:'Concluído',SKIPPED:'Não necessário',FAILED:'Erro',CANCELLED:'Interrompido'})[status]||status}
function formatNumber(value){
  return new Intl.NumberFormat('pt-BR').format(Number(value || 0));
}

function formatUSD(value){
  const number = Number(value || 0);

  if(number === 0){
    return 'US$ 0,00';
  }

  if(number < 0.01){
    return `US$ ${number.toFixed(6).replace('.', ',')}`;
  }

  return `US$ ${number.toFixed(4).replace('.', ',')}`;
}

function durationBetween(start, end){
  if(!start) return '—';

  const a = new Date(start);
  const b = end ? new Date(end) : new Date();

  let seconds = Math.max(
    0,
    Math.floor((b.getTime() - a.getTime()) / 1000)
  );

  if(seconds < 60){
    return `${seconds}s`;
  }

  const minutes = Math.floor(seconds / 60);
  seconds %= 60;

  if(minutes < 60){
    return `${minutes}min ${seconds}s`;
  }

  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;

  return `${hours}h ${remainingMinutes}min`;
}

function runStatusLabel(status){
  return ({
    PENDING: 'Aguardando',
    RUNNING: 'Em execução',
    COMPLETED: 'Concluído',
    FAILED: 'Erro',
    CANCELLED: 'Interrompido'
  })[status] || status;
}

function runStatusClass(status){
  return ({
    PENDING: 'pending',
    RUNNING: 'running',
    COMPLETED: 'done',
    FAILED: 'failed',
    CANCELLED: 'cancelled'
  })[status] || 'pending';
}


function renderRun(run){
  $('#run-tracker').classList.remove('hidden');

  const stages = run.stages || [];
  const costs = run.costs || {};

  const completedStages = stages.filter(stage => stage.status === 'DONE').length;
  const processedStages = stages.filter(stage => ['DONE','SKIPPED'].includes(stage.status)).length;
  const totalStages = stages.length;
  const percent = totalStages ? Math.round((processedStages / totalStages) * 100) : 0;

  const currentStage =
    stages.find(stage => stage.status === 'RUNNING') ||
    stages.find(stage => stage.status === 'FAILED') ||
    stages.find(stage => stage.status === 'CANCELLED') ||
    (run.status === 'COMPLETED' ? stages[stages.length - 1] : null);

  $('#run-summary').textContent = currentStage?.status === 'RUNNING'
    ? `Executando: ${currentStage.label}`
    : (run.message || `Status: ${run.status}`);

  $('#run-status-badge').innerHTML = `
    <span class="run-badge ${runStatusClass(run.status)}">
      ${esc(runStatusLabel(run.status))}
    </span>
  `;

  $('#run-progress-label').textContent =
    `${completedStages} concluída(s) · ${processedStages}/${totalStages} processadas`;
  $('#run-progress-percent').textContent = `${percent}%`;
  $('#run-progress-bar').style.width = `${percent}%`;

  const inputTokens = Number(costs.total_input_tokens || 0);
  const outputTokens = Number(costs.total_output_tokens || 0);
  const cachedTokens = Number(costs.total_cached_input_tokens || 0);
  const totalTokens = inputTokens + outputTokens;

  const modelEntries = Object.values(costs.by_model || {});
  const models = modelEntries.length
    ? modelEntries.map(x => x.model).filter(Boolean).join(', ')
    : '—';

  $('#run-metrics').innerHTML = `
    <div class="run-metric">
      <span class="run-metric-label">Etapa atual</span>
      <strong>${esc(currentStage?.label || runStatusLabel(run.status))}</strong>
      <small>${currentStage?.detail ? esc(currentStage.detail) : 'Nenhuma operação em andamento'}</small>
    </div>

    <div class="run-metric cost">
      <span class="run-metric-label">Custo LLM</span>
      <strong>${formatUSD(costs.total_cost_usd)}</strong>
      <small>${formatNumber(costs.calls)} chamada(s) à LLM</small>
    </div>

    <div class="run-metric">
      <span class="run-metric-label">Tokens</span>
      <strong>${formatNumber(totalTokens)}</strong>
      <small>${formatNumber(inputTokens)} entrada · ${formatNumber(outputTokens)} saída${cachedTokens ? ` · ${formatNumber(cachedTokens)} cache` : ''}</small>
    </div>

    <div class="run-metric">
      <span class="run-metric-label">Tempo</span>
      <strong>${durationBetween(run.started_at, run.finished_at)}</strong>
      <small>${esc(models)}</small>
    </div>
  `;

  $('#run-steps').innerHTML = stages.map((stage, index) => {
    const statusClass = stageClass(stage.status);
    const circleContent = stage.status === 'DONE' ? '✓' : String(index + 1);

    return `
      <div class="run-step ${statusClass}" title="${esc(stage.detail || stageLabel(stage.status))}">
        <div class="run-step-circle">${circleContent}</div>
        <div class="run-step-label">${esc(stage.label)}</div>
        <div class="run-step-status">${esc(stageLabel(stage.status))}</div>
      </div>
    `;
  }).join('');

  const detailStage = currentStage || stages.find(stage => stage.status === 'PENDING') || null;
  const detailClass = detailStage ? stageClass(detailStage.status) : 'pending';
  const detailBox = $('#run-stage-detail');
  detailBox.className = `run-stage-detail ${detailClass}`;
  detailBox.innerHTML = detailStage
    ? `
        <strong>${esc(detailStage.label)}</strong>
        <div>${esc(stageLabel(detailStage.status))}${detailStage.detail ? ` · ${esc(detailStage.detail)}` : ''}</div>
        <small>${detailStage.started_at ? `Tempo: ${esc(durationBetween(detailStage.started_at, detailStage.finished_at))}` : 'Aguardando início'}</small>
      `
    : `
        <strong>Execução</strong>
        <div>Nenhuma etapa disponível.</div>
      `;

  if(currentStage?.status === 'RUNNING'){
    requestAnimationFrame(() => {
      const active = document.querySelector('.run-step.running');
      active?.scrollIntoView({behavior:'smooth', inline:'center', block:'nearest'});
    });
  }

  const running = ['PENDING','RUNNING'].includes(run.status);
  $('#submit').disabled = running;
  $('#stop-report').classList.toggle('hidden', !running);
  $('#stop-report').disabled = !!run.cancel_requested;
  $('#stop-report').textContent = run.cancel_requested ? 'Parando…' : 'Parar relatório';
}

async function finishRun(run){
  clearInterval(pollTimer);pollTimer=null;
  $('#submit').disabled=false;$('#stop-report').classList.add('hidden');
  if(run.status==='COMPLETED'){
    $('#progress').textContent=run.message||'Relatório concluído.';
    const d=await api(`/reports/history/${run.project_id}`);currentProjectId=run.project_id;render(d.report);loadHistory();
  }else if(run.status==='CANCELLED'){
    $('#progress').textContent='Relatório interrompido. Os dados já coletados permanecem salvos para auditoria.';
  }else if(run.status==='FAILED'){
    $('#progress').textContent=`Erro: ${run.error||run.message||'falha não identificada'}`;
  }
}
async function pollRun(){if(!currentRunId)return;try{const run=await api(`/runs/${currentRunId}`);renderRun(run);if(['COMPLETED','CANCELLED','FAILED'].includes(run.status))await finishRun(run)}catch(err){clearInterval(pollTimer);pollTimer=null;$('#progress').textContent=`Erro ao acompanhar execução: ${err.message}`;$('#submit').disabled=false}}

async function loadHistory(){try{const rows=await api('/reports/history');const box=$('#history-list');if(!rows.length){box.innerHTML='<span class="note">Nenhum relatório salvo.</span>';return}box.innerHTML='';rows.forEach(row=>{const wrap=document.createElement('div');wrap.className='history-entry';const open=document.createElement('button');open.className='history-item';open.innerHTML=`<strong>${esc(row.topic)}</strong><span>${esc(row.generated_at||'')} · QA ${esc(row.qa_status||'PENDING')}</span>`;open.onclick=async()=>{const d=await api(`/reports/history/${row.id}`);currentProjectId=row.id;render(d.report)};const del=document.createElement('button');del.className='danger history-delete';del.textContent='×';del.onclick=async()=>{if(!confirm('Excluir esta versão e seus dados associados?'))return;await api(`/reports/history/${row.id}`,{method:'DELETE'});loadHistory()};wrap.append(open,del);box.appendChild(wrap)})}catch(e){$('#history-list').textContent=e.message}}

$('#report-form').addEventListener('submit',async e=>{e.preventDefault();const btn=$('#submit'),progress=$('#progress');btn.disabled=true;progress.classList.remove('hidden');$('#run-tracker').classList.add('hidden');try{
  progress.textContent='Preparando projeto…';
  const payload={topic:$('#topic').value.trim()};
  for(const [id,key] of [['collection-start','collection_start'],['collection-end','collection_end'],['event-start','event_start'],['event-end','event_end']]){if($(`#${id}`).value)payload[key]=$(`#${id}`).value}
  const created=await api('/projects',{method:'POST',body:JSON.stringify(payload)});currentProjectId=created.id;
  const started=await api(`/projects/${created.id}/run-async`,{method:'POST'});currentRunId=started.run.run_id;renderRun(started.run);progress.textContent='Relatório em processamento.';
  if(pollTimer)clearInterval(pollTimer);pollTimer=setInterval(pollRun,1000);await pollRun();
}catch(err){progress.textContent=`Erro: ${err.message}`;btn.disabled=false;$('#stop-report').classList.add('hidden')}});

$('#stop-report').onclick=async()=>{if(!currentRunId)return;try{const response=await api(`/runs/${currentRunId}/cancel`,{method:'POST'});renderRun(response.run);$('#progress').textContent='Cancelamento solicitado. A execução será encerrada no próximo ponto seguro.'}catch(e){alert(e.message)}};
$('#refresh-report').onclick=async()=>{if(!currentProjectId)return alert('Abra ou gere um relatório primeiro.');try{const d=await api(`/reports/history/${currentProjectId}`);render(d.report)}catch(e){alert(e.message)}};
$('#save-pdf').onclick=()=>{if(!currentProjectId)return;window.open(`/projects/${currentProjectId}/export.pdf`,'_blank')};
$('#save-draft').onclick=()=>{if(!currentProjectId)return;window.open(`/projects/${currentProjectId}/export-draft.pdf`,'_blank')};
loadHistory();
