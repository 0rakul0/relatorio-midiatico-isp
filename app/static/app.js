
const $=s=>document.querySelector(s);
const esc=v=>String(v??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const RAW_HTML='__raw__';
const raw=v=>RAW_HTML+String(v??'');
const api=async(path,opts={})=>{const r=await fetch(path,{headers:{'Content-Type':'application/json',...authHeaders()},...opts});if(r.status===401&&await refreshSession()){const r2=await fetch(path,{headers:{'Content-Type':'application/json',...authHeaders()},...opts});return handleApi(r2)}if(r.status===401){window.location.href='/login';throw new Error('Sessão expirada. Entre de novo.')}return handleApi(r)};
async function handleApi(r){if(r.status===204)return null;const raw=await r.text();let d;try{d=raw?JSON.parse(raw):null}catch{d=null}if(!r.ok)throw new Error(d?.detail||raw||`Erro HTTP ${r.status}`);return d}

function showLoggedOut(msg){
  saveAuth(null);
  window.location.href='/login';
}
async function doLogout(){saveAuth(null);window.location.href='/login';}
async function renderAuth(){
  const s=authState();
  if(!s){window.location.href='/login';return;}
  $('#auth-user-email').textContent=s.email||'';
  try{
    const me=await api('/billing/me');
    $('#auth-user-plan').textContent=me.plan||'FREE';
  }catch(e){
    $('#auth-user-plan').textContent='Conta ativa';
  }
}
$('#auth-logout').onclick=doLogout;
if(authState()){renderAuth();}else{window.location.href='/login';}
const table=(headers,rows)=>`<div class="table-wrap"><table><thead><tr>${headers.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(cell=>{const s=String(cell??'');return `<td>${s.startsWith(RAW_HTML)?s.slice(RAW_HTML.length):esc(s)}</td>`}).join('')}</tr>`).join('')}</tbody></table></div>`;
let currentProjectId=null,currentRunId=null,pollTimer=null;

api('/health').then(d=>$('#api-status').textContent=`API conectada · ${d.version}`).catch(()=>$('#api-status').textContent='API indisponível');

function qaBadge(qa){const status=qa?.status||'PENDING';const cls=status==='APPROVED'?'ok':status==='REJECTED'?'bad':'warn';return `<span class="badge ${cls}">${esc(status)}</span>`}
function factStatus(v){const map={CONFIRMED:'Confirmado',PARTIALLY_CONFIRMED:'Confirmação parcial',SOURCE_CONFLICT:'Conflito entre fontes',NOT_FOUND_IN_SAMPLE:'Não localizado na amostra'};return map[v]||v||'N/D'}
function scopeLabel(v){return v===true?'núcleo principal':v===false?'caso relacionado':'escopo pendente'}
function relationLabel(v){const map={DIRECT_PRODUCT:'Direto ao produto',ATTRIBUTED_FINDING:'Achado atribuído',DERIVED_COVERAGE:'Cobertura derivada',DIRECT_EVENT:'Direto ao evento',THEMATIC_CONTEXT:'Contexto temático',THEMATIC_ONLY:'Apenas semelhante',UNRELATED:'Não relacionado'};return map[v]||v||'Relacionado ao tema'}
function originLabel(v){return String(v||'SEARCH').toUpperCase()==='REUSED'?'Corpus reutilizado':'Nova coleta'}
function originClass(v){return String(v||'SEARCH').toUpperCase()==='REUSED'?'reused':'new'}
function viewLabel(v){return Number.isInteger(v)?v.toLocaleString('pt-BR'):'N/D'}
const SOCIAL_HOSTS=['facebook.com','instagram.com','x.com','twitter.com','tiktok.com','threads.net','threads.com','linkedin.com','pinterest.com','pin.it','reddit.com','t.me','telegram.me','whatsapp.com','wa.me','kwai.com','bsky.app','mastodon.social'];
function bucketByOrigin(items){
  const b={portal_noticias:[],redes_sociais:[],youtube:[]};
  for(const x of (items||[])){
    let o=String(x.media_origin||'').toUpperCase();
    if(o!=='YOUTUBE'&&o!=='REDE_SOCIAL'&&o!=='PORTAL_NOTICIAS'){
      const h=String(x.domain||'').toLowerCase();
      o=(h==='youtube.com'||h.endsWith('.youtube.com')||h==='youtu.be')?'YOUTUBE':SOCIAL_HOSTS.some(s=>h===s||h.endsWith('.'+s))?'REDE_SOCIAL':'PORTAL_NOTICIAS';
    }
    b[o==='YOUTUBE'?'youtube':o==='REDE_SOCIAL'?'redes_sociais':'portal_noticias'].push(x);
  }
  return b;
}
const ANNEX_TITLE_STOPWORDS=new Set(['a','as','com','da','das','de','do','dos','e','em','na','nas','no','nos','o','os','para','por','um','uma']);
const ANNEX_TRACKING_KEYS=new Set(['fbclid','gclid','dclid','msclkid','mc_cid','mc_eid','igshid','mkt_tok','vero_id']);

function normalizeAnnexText(value){
  const normalized=String(value||'').normalize('NFD').replace(/[\u0300-\u036f]/g,'').toLowerCase();
  return (normalized.match(/[a-z0-9]+/g)||[]).join(' ');
}

function annexTitleTokens(value){
  return new Set(normalizeAnnexText(value).split(' ').filter(token=>token&&!ANNEX_TITLE_STOPWORDS.has(token)));
}

function annexDomain(item){
  let domain=String(item?.domain||'').toLowerCase().trim();
  if(!domain&&item?.url){
    try{domain=new URL(item.url,window.location.origin).hostname.toLowerCase()}catch(_error){}
  }
  return domain.startsWith('www.')?domain.slice(4):domain;
}

function annexYear(item){
  const published=String(item?.published_at||'');
  if(/^\d{4}/.test(published))return published.slice(0,4);
  const year=String(item?.published_year||'');
  return /^\d{4}$/.test(year)?year:'';
}

function annexUrlKey(value){
  const raw=String(value||'').trim();
  if(!raw)return '';
  try{
    const url=new URL(raw,window.location.origin);
    let host=url.hostname.toLowerCase();
    if(host.startsWith('www.'))host=host.slice(4);
    let path=url.pathname.replace(/\/{2,}/g,'/');
    if(path.length>1)path=path.replace(/\/+$/,'');
    const params=[...url.searchParams.entries()]
      .filter(([key])=>{
        const lowered=key.toLowerCase();
        return !lowered.startsWith('utm_')&&!ANNEX_TRACKING_KEYS.has(lowered);
      })
      .sort((a,b)=>a[0].localeCompare(b[0])||a[1].localeCompare(b[1]));
    const query=new URLSearchParams(params).toString();
    return host+path+(query?'?'+query:'');
  }catch(_error){
    return raw.toLowerCase();
  }
}

function sameAnnexItem(left,right){
  const leftUrl=annexUrlKey(left?.canonical_url||left?.url);
  const rightUrl=annexUrlKey(right?.canonical_url||right?.url);
  if(leftUrl&&rightUrl&&leftUrl===rightUrl)return true;
  if(annexDomain(left)!==annexDomain(right))return false;
  const leftOrigin=String(left?.media_origin||'');
  const rightOrigin=String(right?.media_origin||'');
  if(leftOrigin&&rightOrigin&&leftOrigin!==rightOrigin)return false;
  const leftYear=annexYear(left),rightYear=annexYear(right);
  if(leftYear&&rightYear&&leftYear!==rightYear)return false;

  const leftTitle=normalizeAnnexText(left?.title);
  const rightTitle=normalizeAnnexText(right?.title);
  if(!leftTitle||!rightTitle)return false;
  if(leftTitle===rightTitle)return true;

  const leftTokens=annexTitleTokens(left?.title);
  const rightTokens=annexTitleTokens(right?.title);
  if(Math.min(leftTokens.size,rightTokens.size)<3)return false;
  const intersection=[...leftTokens].filter(token=>rightTokens.has(token)).length;
  const containment=intersection/Math.min(leftTokens.size,rightTokens.size);
  const union=new Set([...leftTokens,...rightTokens]).size;
  const jaccard=union?intersection/union:0;
  const shorter=leftTitle.length<=rightTitle.length?leftTitle:rightTitle;
  const titleContainment=shorter.length>=12&&(leftTitle.includes(rightTitle)||rightTitle.includes(leftTitle));
  return containment>=.90&&(jaccard>=.72||titleContainment);
}

function annexItemQuality(item){
  return [
    item?.published_at?1:0,
    (item?.evidence||item?.relation_evidence)?1:0,
    String(item?.title||'').length,
    item?.url?1:0
  ];
}

function betterAnnexItem(candidate,existing){
  const left=annexItemQuality(candidate),right=annexItemQuality(existing);
  for(let i=0;i<left.length;i++){
    if(left[i]!==right[i])return left[i]>right[i];
  }
  return false;
}

function deduplicateAnnexItems(rows){
  const representatives=[];
  for(const row of (rows||[])){
    const candidate={...row,duplicate_count:Number(row?.duplicate_count||0),duplicate_urls:[...(row?.duplicate_urls||[])]};
    const index=representatives.findIndex(existing=>sameAnnexItem(existing,candidate));
    if(index<0){representatives.push(candidate);continue}

    const existing=representatives[index];
    const duplicateCount=Number(existing.duplicate_count||0)+Number(candidate.duplicate_count||0)+1;
    const urls=[existing.url,...(existing.duplicate_urls||[]),candidate.url,...(candidate.duplicate_urls||[])]
      .filter(Boolean)
      .filter((url,pos,array)=>array.indexOf(url)===pos);
    const representative=betterAnnexItem(candidate,existing)?{...candidate}:{...existing};
    representative.duplicate_count=duplicateCount;
    representative.duplicate_urls=urls.filter(url=>url!==representative.url);
    representatives[index]=representative;
  }
  return representatives;
}

function annexCollapsedCount(rows){
  return (rows||[]).reduce((total,item)=>total+Number(item?.duplicate_count||0),0);
}
function renderWordCloud(cloud){
  const words=(cloud?.words||[]).slice(0,50);
  if(!words.length){
    return '<div class="word-cloud-panel empty"><span>Nenhuma palavra relevante disponível no corpus jornalístico validado.</span></div>';
  }

  // Os spans funcionam como fallback caso D3/CDN esteja indisponível.
  const fallback=words.slice(0,30).map((item,index)=>{
    const weight=Math.max(0,Math.min(1,Number(item.weight||0)));
    const size=14+Math.round(weight*22);
    const emphasis=index<5?' word-cloud-fallback-top':'';
    return `<span class="word-cloud-fallback-term${emphasis}" data-rank="${index+1}" style="font-size:${size}px">${esc(item.word)}</span>`;
  }).join('');

  const mergedVariants=Math.max(0,Number(cloud?.merged_variants||0));
  const normalizationNote=mergedVariants
    ? ` · <span class="word-cloud-normalization">${esc(mergedVariants)} variante(s) linguística(s) consolidada(s)</span>`
    : '';

  return `<div class="word-cloud-panel">
    <div id="word-cloud-chart" class="word-cloud-chart" aria-label="Nuvem de palavras do corpus validado">
      <div class="word-cloud-fallback">${fallback}</div>
    </div>
    <p class="word-cloud-note">
      ${esc(cloud.documents||0)} notícia(s) validada(s) de portais · stopwords e nomes de sites removidos${normalizationNote}.
    </p>
  </div>`;
}

function wordCloudSeed(value){
  let hash=2166136261;
  const text=String(value||'word-cloud');
  for(let i=0;i<text.length;i++){
    hash^=text.charCodeAt(i);
    hash=Math.imul(hash,16777619);
  }
  return hash>>>0;
}

function seededRandom(seed){
  let state=seed>>>0;
  return ()=>{
    state=(Math.imul(1664525,state)+1013904223)>>>0;
    return state/4294967296;
  };
}

function drawPackedWordCloud(containerSelector,cloud){
  const container=document.querySelector(containerSelector);
  const words=(cloud?.words||[]).slice(0,50);
  if(!container||!words.length)return;

  if(typeof window.d3==='undefined'||!window.d3.layout||typeof window.d3.layout.cloud!=='function'){
    container.classList.add('word-cloud-fallback-active');
    return;
  }

  const width=Math.max(320,Math.floor(container.getBoundingClientRect().width||900));
  const height=width<560?330:420;
  const counts=words.map(item=>Math.max(1,Number(item.count||1)));
  const minCount=Math.min(...counts);
  const maxCount=Math.max(...counts);

  const fontScale=maxCount===minCount
    ? ()=>34
    : d3.scaleSqrt().domain([minCount,maxCount]).range([16,width<560?62:88]);

  // Paleta institucional fixa: azul, roxo, verde, teal e amarelo.
  // Top 5 usa tons fortes; os demais ciclam por versoes mais suaves.
  const strongPalette=['#075B9A','#6536A5','#247A3C','#007C73','#A87400'];
  const softPalette=['#6F9FC2','#9A83BC','#79A783','#6FAAA5','#B9A36A'];
  const seed=wordCloudSeed(words.map(item=>`${item.word}:${item.count}`).join('|'));
  const random=seededRandom(seed);

  const prepared=words.map((item,index)=>({
    text:String(item.word||'').trim(),
    count:Math.max(1,Number(item.count||1)),
    size:fontScale(Math.max(1,Number(item.count||1))),
    rotate:index%9===0?90:index%13===0?-90:0,
    rank:index+1,
    isTop:index<5,
    color:index<5
      ? strongPalette[index]
      : softPalette[(index-5)%softPalette.length],
  })).filter(item=>item.text);

  container.innerHTML='';

  d3.layout.cloud()
    .size([width,height])
    .words(prepared)
    .padding(3)
    .rotate(item=>item.rotate)
    .font('Inter, Segoe UI, Arial, sans-serif')
    .fontWeight(item=>item.size>=48?800:700)
    .fontSize(item=>item.size)
    .spiral('archimedean')
    .random(random)
    .on('end',layoutWords=>{
      const svg=d3.select(container)
        .append('svg')
        .attr('class','word-cloud-svg')
        .attr('viewBox',`0 0 ${width} ${height}`)
        .attr('width','100%')
        .attr('height',height)
        .attr('role','img')
        .attr('aria-label','Nuvem de palavras do corpus jornalístico validado');

      const group=svg.append('g')
        .attr('transform',`translate(${width/2},${height/2})`);

      const nodes=group.selectAll('text')
        .data(layoutWords)
        .enter()
        .append('text')
        .attr('class',item=>`word-cloud-svg-term${item.isTop?' word-cloud-svg-top':''}`)
        .attr('data-rank',item=>item.rank)
        .style('font-family','Inter, Segoe UI, Arial, sans-serif')
        .style('font-size',item=>`${item.size}px`)
        .style('font-weight',item=>item.isTop?900:(item.size>=48?800:650))
        .style('fill',item=>item.color)
        .style('opacity',item=>item.isTop?1:.78)
        .attr('text-anchor','middle')
        .attr('transform',item=>`translate(${item.x},${item.y}) rotate(${item.rotate})`)
        .text(item=>item.text);

      nodes.append('title')
        .text(item=>`${item.text}: ${item.count} ocorrência(s)`);
    })
    .start();
}

function render(result){
  const d=result.report,p=result.project,m=result.metrics,qa=result.qa||{};
  const facts=result.fact_events||[];
  const factRows=facts.map(x=>[
    x.subject_name||'Não localizado',
    [x.institution,x.rank_or_role,x.unit].filter(Boolean).join(' / ')||'Não localizado',
    x.death_date||x.event_date||'Não localizada',
    x.cause||x.circumstance||'Não localizado',
    [x.address,x.neighborhood,x.city,x.state].filter(Boolean).join(', ')||'Não localizado',
    [x.death_place_name,x.death_address,x.death_neighborhood,x.death_city,x.death_state].filter(Boolean).join(', ')||'Não localizado',
    raw(`<span class="${x.resolution_status==='SOURCE_CONFLICT'?'fact-conflict':x.resolution_status==='CONFIRMED'?'fact-confirmed':''}">${esc(factStatus(x.resolution_status))}</span><br><small>${esc(scopeLabel(x.primary_scope))}${x.conflict_fields?.length?` · conflito: ${esc(x.conflict_fields.join(', '))}`:''}</small>`)
  ]);
  const axes=(d.thematic_axes||[]).map(x=>[x.axis,x.anchor_data,x.coverage]);
  const risks=(d.risk_assessment||[]).map(x=>[x.dimension,x.assessment,x.evidence]);
  const kit=(d.press_kit||[]).map(x=>[x.product,x.purpose]);
  const rawCorpus=result.corpus||[];
  const wordCloud=result.word_cloud||{};
  const academicPapers=result.academic_papers||[];
  const academicSection=academicPapers.length?`<h2>Literatura científica relacionada</h2>
    <p class="related-intro">Estes trabalhos foram recuperados no arXiv para contextualização científica. Eles não são contados como repercussão midiática e não confirmam automaticamente fatos noticiados.</p>
    ${table(['Ano','Autores','Artigo','Relação com o tema','Fonte'],academicPapers.map(x=>[
      x.published_at?String(x.published_at).slice(0,4):'N/D',
      (x.authors||[]).slice(0,4).join(', ')+((x.authors||[]).length>4?' et al.':''),
      raw(`<strong>${esc(x.title||'Sem título')}</strong>${x.title_original&&x.title_original!==x.title? `<br><small>Original: ${esc(x.title_original)}</small>`:''}${x.abstract_ptbr? `<details class="academic-abstract"><summary>Resumo em pt-BR</summary><small>${esc(x.abstract_ptbr)}</small></details>`:''}`),
      x.relation_to_topic||'Contexto científico relacionado',
      x.url?raw(`<a href="${esc(x.url)}" target="_blank" rel="noreferrer">arXiv</a>`):'N/D'
    ]))}`:'';
  const sourceBuckets=rawCorpus.length
    ? bucketByOrigin(rawCorpus)
    : (result.corpus_by_origin||{portal_noticias:[],redes_sociais:[],youtube:[]});
  const socialItems=deduplicateAnnexItems(sourceBuckets.redes_sociais||[]);
  const youtubeItems=deduplicateAnnexItems(sourceBuckets.youtube||[]);
  const portalItems=deduplicateAnnexItems(sourceBuckets.portal_noticias||[]);
  const displayCorpusCount=socialItems.length+youtubeItems.length+portalItems.length;
  const collapsedDuplicates=annexCollapsedCount(socialItems)+annexCollapsedCount(youtubeItems)+annexCollapsedCount(portalItems);
  const displayValidItems=(rawCorpus.length||result.corpus_by_origin)?displayCorpusCount:(m.valid_items||0);

  const corpusRow=(x,i)=>{
    const duplicateCount=Number(x.duplicate_count||0);
    const titleCell=raw(`<span>${esc(x.title||'Sem título')}</span>${duplicateCount?`<br><span class="annex-duplicate-badge">+${esc(duplicateCount)} duplicata(s) consolidada(s)</span>`:''}`);
    const corpusOrigin=x.corpus_origin||x.origin||'SEARCH';
    return [
      String(i+1),
      x.published_at||x.published_year||'N/D',
      x.source||x.domain||'Fonte aberta',
      titleCell,
      raw(`<span class="origin-badge ${originClass(corpusOrigin)}">${esc(originLabel(corpusOrigin))}</span>${x.search_source?`<br><small>via ${esc(x.search_source)}</small>`:''}`),
      x.url?raw(`<a href="${esc(x.url)}" target="_blank" rel="noreferrer">Abrir</a>`):'N/D'
    ];
  };
  const annexTable=(rows)=>{
    if(!rows.length)return '<p>Nenhum item validado nesta categoria para a janela observada.</p>';
    const collapsed=annexCollapsedCount(rows);
    const note=collapsed
      ? `<p class="annex-dedup-note">${esc(collapsed)} entrada(s) duplicada(s) foram consolidadas neste anexo.</p>`
      : '';
    return note+`<div class="annex-table">${table(['#','Data','Fonte','Título','Origem','URL'],rows.map(corpusRow))}</div>`;
  };
  const duplicateNote=collapsedDuplicates
    ? ` ${collapsedDuplicates} entrada(s) repetida(s) foram consolidadas para evitar dupla contagem.`
    : '';
  const relatedSummary=`<p class="related-intro">${displayCorpusCount} item(ns) único(s) validado(s) como materialmente relacionados ao tema: ${socialItems.length} em mídias sociais, ${youtubeItems.length} no YouTube e ${portalItems.length} em portais de notícias.${duplicateNote} O detalhamento item a item está nos anexos.</p><p class="note">Origem: <span class="origin-badge new">Nova coleta</span> = coletado desta vez; <span class="origin-badge reused">Corpus reutilizado</span> = reaproveitado de coleta anterior.</p>`;
  const linkCell=u=>u?raw(`<a href="${esc(u)}" target="_blank" rel="noreferrer">Abrir</a>`):'N/D';
  const coveredPortals=(m.portal_checks||[]).filter(x=>x.result==='com cobertura auditável');
  const portalSection=coveredPortals.length?`<h2>Checagem de portais prioritários</h2>${table(['Portal','Resultado','Evidência'],coveredPortals.map(x=>[x.portal,x.result,x.evidence]))}`:'';
  const coveredChannels=(m.youtube_priority_channel_checks||[]).filter(x=>x.result==='com cobertura auditável');
  const channelSection=coveredChannels.length?`<h2>Checagem de canais prioritários no YouTube</h2>${table(['Canal','Resultado','Vídeos','Visualizações','Link do vídeo de maior alcance'],coveredChannels.map(x=>[x.channel,x.result,x.videos??0,viewLabel(x.views),linkCell(x.lead_url)]))}`:'';
  const topChannels=(m.top_youtube_channels||[]);
  const topChannelsSection=topChannels.length?`<h2>Canais no YouTube</h2>${table(['#','Canal','Vídeos validados','Visualizações','Link do vídeo de maior alcance'],topChannels.map((x,i)=>[String(i+1),x.channel,x.videos??0,viewLabel(x.views),linkCell(x.lead_url)]))}`:'';
  const topReach=(m.top_reach_contents||[]);
  const topReachSection=topReach.length?`<h2>Conteúdos por alcance disponível</h2><p class="related-intro">Ranking considera apenas itens validados com métrica numérica de alcance disponível no corpus.</p>${table(['#','Plataforma','Fonte','Título','Alcance','URL'],topReach.map((x,i)=>[String(i+1),x.platform,x.source,x.title,viewLabel(x.reach),linkCell(x.url)]))}`:'';
  const annexBase=!!p.execution_flags?.enable_fact_layer?2:1;
  const annexLetter=i=>String.fromCharCode(65+annexBase+i);
  const windowLabel=(a,b,empty='Não delimitado')=>a&&b?`${esc(a)} a ${esc(b)}`:a?esc(a):b?esc(b):empty;
  const factLayerEnabled=!!p.execution_flags?.enable_fact_layer;
  const factSection=factLayerEnabled?`<h2>Camada de Fatos Verificados</h2><p>${esc(d.fact_layer_intro||'')}</p>${facts.length?table(['Pessoa','Vínculo','Data','Fato / causa','Local do fato','Local da morte','Situação'],factRows):'<p>Nenhum fato individual foi suficientemente estruturado na amostra factual.</p>'}`:'';
  const contextMeta=p.project_type==='INSTITUTIONAL_PRODUCT'?`<div><b>Lançamento</b><br>${esc(p.launch_date||'Não confirmado')}</div>`:`<div><b>Janela dos fatos</b><br>${windowLabel(p.event_start,p.event_end)}</div>`;
  $('#report-layout').classList.remove('hidden');
  $('#report').innerHTML=`
    <div class="kicker">Relatório de repercussão midiática</div>
    <h1>${esc(d.title)}</h1><p class="interpretive">${esc(d.interpretive_title)}</p><p class="subtitle">${esc(d.subtitle)}</p>
    <div class="report-meta"><div><b>Instituição</b><br>${esc(p.institution)}</div>${contextMeta}<div><b>Janela de repercussão</b><br>${windowLabel(p.collection_start,p.collection_end,'Busca temática')}</div><div><b>QA</b><br>${qaBadge(qa)}</div></div>
    <h2>Nuvem de palavras</h2>
    ${renderWordCloud(wordCloud)}
    <h2>Resumo Executivo</h2><div class="summary"><p>${esc(d.executive_summary)}</p></div>
    ${academicSection}
    <h2>Itens relacionados encontrados</h2>
    ${relatedSummary}
    ${factSection}
    <h2>Abertura</h2><p>${esc(d.opening)}</p>
    <h2>I. Panorama da Repercussão</h2><p>${esc(d.panorama)}</p>
    <div class="table-wrap"><table><tbody><tr><th>Itens validados</th><td>${esc(displayValidItems)}</td><th>Veículos</th><td>${esc(m.unique_vehicles)}</td><th>Eventos factuais</th><td>${esc(m.facts?.events||0)}</td></tr></tbody></table></div>
    ${portalSection}
    ${channelSection}
    ${topChannelsSection}
    ${topReachSection}
    <h2>II. Enquadramento Dominante</h2><p>${esc(d.dominant_framing)}</p>
    <h2>III. Um Estudo, Muitas Pautas</h2>${table(['Eixo temático','Dado-âncora','Cobertura'],axes)}
    <h2>IV. Recorte de Maior Rendimento Jornalístico</h2><p>${esc(d.highest_yield)}</p>
    <h2>V. Camada Institucional e Disputa de Narrativa</h2><p>${esc(d.institutional_narrative)}</p>
    <h2>VI. Avaliação: Alcance, Profundidade e Riscos</h2>${table(['Dimensão','Avaliação','Evidência'],risks)}
    <h2>VII. Recomendações e Kit de Imprensa</h2><ul>${(d.recommendations||[]).map(x=>`<li>${esc(x)}</li>`).join('')}</ul>${table(['Produto','Finalidade'],kit)}
    <h2>VIII. Síntese</h2><p>${esc(d.synthesis)}</p>
    <h2>Anexo A - Nota Metodológica</h2><p>${esc(d.methodological_note)}</p>
    <h2>Anexo ${annexLetter(0)} - Mídias Sociais</h2><p class="related-intro">Itens validados na janela de repercussão.</p>${annexTable(socialItems)}
    <h2>Anexo ${annexLetter(1)} - YouTube</h2><p class="related-intro">Itens validados na janela de repercussão.</p>${annexTable(youtubeItems)}
    <h2>Anexo ${annexLetter(2)} - Portais de Notícias</h2><p class="related-intro">Itens validados na janela de repercussão.</p>${annexTable(portalItems)}
    ${qa.findings?.length?`<h2>Achados de QA</h2>${table(['Severidade','Código','Mensagem'],qa.findings.map(x=>[x.severity,x.code,x.message]))}`:''}
    <div class="footer-note">Fato, fonte factual e item de repercussão são tratados como objetos distintos. Fontes posteriores podem confirmar um fato sem aumentar a repercussão do mês.</div>`;
  requestAnimationFrame(()=>drawPackedWordCloud('#word-cloud-chart',wordCloud));
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



const STAGE_GROUPS = [
  { number: 1, label: 'Perfil do tema', keys: ['profile'] },
  { number: 2, label: 'Planejamento do relatório', keys: ['search_plan'] },
  {
    number: 3,
    label: 'Coleta',
    keys: ['collection', 'youtube'],
    vertical: true
  },
  { number: 4, label: 'Literatura científica', keys: ['academic_research'] },
  { number: 5, label: 'Validação das notícias', keys: ['validation'] },
  { number: 6, label: 'Extração factual', keys: ['facts_pass_1'] },
  { number: 7, label: 'Consolidação factual', keys: ['fact_resolution_1'] },
  { number: 8, label: 'Planejamento nominal', keys: ['nominal_plan'] },
  { number: 9, label: 'Coleta nominal', keys: ['nominal_collection'] },
  { number: 10, label: 'Extração complementar', keys: ['facts_pass_2'] },
  { number: 11, label: 'Consolidação final', keys: ['fact_resolution_2'] },
  { number: 12, label: 'Análise e classificação', keys: ['classification'] },
  { number: 13, label: 'Cobertura complementar', keys: ['gap_fill'] },
  { number: 14, label: 'Redação do relatório', keys: ['report'] },
  { number: 15, label: 'Auditoria QA final', keys: ['qa'] }
];

function normalizeGroupStatus(children){
  if(!children.length) return 'PENDING';
  const statuses = children.map(x => x.status);
  if(statuses.includes('FAILED')) return 'FAILED';
  if(statuses.includes('CANCELLED')) return 'CANCELLED';
  if(statuses.includes('RUNNING')) return 'RUNNING';
  if(statuses.every(s => s === 'SKIPPED')) return 'SKIPPED';
  if(statuses.every(s => ['DONE','SKIPPED'].includes(s))) return 'DONE';
  return 'PENDING';
}

function buildGroupedStages(stages){
  const stageMap = new Map((stages || []).map(stage => [stage.key, stage]));
  return STAGE_GROUPS.map(group => {
    const children = group.keys.map(key => stageMap.get(key)).filter(Boolean);
    const status = normalizeGroupStatus(children);
    const currentChild =
      children.find(x => x.status === 'RUNNING') ||
      children.find(x => x.status === 'FAILED') ||
      children.find(x => x.status === 'CANCELLED') ||
      children.find(x => x.status === 'PENDING') ||
      children[0] || null;
    const startedAt = children.map(x => x.started_at).filter(Boolean).sort()[0] || null;
    const finishedAt = children.length && children.every(x => !x.started_at || x.finished_at || x.status === 'SKIPPED')
      ? (children.map(x => x.finished_at).filter(Boolean).sort().slice(-1)[0] || null)
      : null;
    return {
      ...group,
      children,
      status,
      currentChild,
      detail: currentChild?.detail || null,
      started_at: startedAt,
      finished_at: finishedAt
    };
  });
}

function renderGroupDetail(group){
  const detailBox = $('#run-stage-detail');
  const detailClass = stageClass(group?.status || 'PENDING');
  detailBox.className = `run-stage-detail ${detailClass}`;

  if(!group){
    detailBox.innerHTML = '<strong>Execução</strong><div>Nenhuma etapa disponível.</div>';
    return;
  }

  if(group.vertical){
    detailBox.innerHTML = `
      <strong>Etapa ${group.displayNumber ?? group.number} · ${esc(group.label)}</strong>
      <div class="parallel-stage-list">
        ${group.children.map(child => `
          <div class="parallel-stage ${stageClass(child.status)}">
            <div class="parallel-stage-badge">${group.displayNumber ?? group.number}</div>
            <div class="parallel-stage-content">
              <div class="parallel-stage-title">${esc(child.label)}</div>
              <div class="parallel-stage-meta">
                ${esc(stageLabel(child.status))}${child.detail ? ` · ${esc(child.detail)}` : ''}
              </div>
              <small>${child.started_at ? `Tempo: ${esc(durationBetween(child.started_at, child.finished_at))}` : 'Aguardando início'}</small>
            </div>
          </div>
        `).join('')}
      </div>
    `;
    return;
  }

  detailBox.innerHTML = `
    <strong>Etapa ${group.displayNumber ?? group.number} · ${esc(group.label)}</strong>
    <div>${esc(stageLabel(group.status))}${group.detail ? ` · ${esc(group.detail)}` : ''}</div>
    <small>${group.started_at ? `Tempo: ${esc(durationBetween(group.started_at, group.finished_at))}` : 'Aguardando início'}</small>
  `;
}

function renderRun(run){
  $('#run-tracker').classList.remove('hidden');

  const rawStages = run.stages || [];
  const allGroups = buildGroupedStages(rawStages);
  const planningFinished = rawStages.find(stage => stage.key === 'search_plan')?.status === 'DONE';
  const groups = (planningFinished
    ? allGroups.filter(group => group.status !== 'SKIPPED')
    : allGroups
  ).map((group, index) => ({...group, displayNumber: index + 1}));
  const costs = run.costs || {};

  const completedGroups = groups.filter(group => group.status === 'DONE').length;
  const processedGroups = groups.filter(group => group.status === 'DONE').length;
  const totalGroups = groups.length;
  const percent = totalGroups ? Math.round((processedGroups / totalGroups) * 100) : 0;

  const currentGroup =
    groups.find(group => group.status === 'RUNNING') ||
    groups.find(group => group.status === 'FAILED') ||
    groups.find(group => group.status === 'CANCELLED') ||
    (run.status === 'COMPLETED' ? groups[groups.length - 1] : groups.find(group => group.status === 'PENDING')) ||
    null;

  $('#run-summary').textContent = currentGroup?.status === 'RUNNING'
    ? `Executando: Etapa ${currentGroup.number} · ${currentGroup.label}`
    : (run.message || `Status: ${run.status}`);

  $('#run-status-badge').innerHTML = `
    <span class="run-badge ${runStatusClass(run.status)}">
      ${esc(runStatusLabel(run.status))}
    </span>
  `;

  $('#run-progress-label').textContent =
    `${completedGroups} concluída(s) · ${processedGroups}/${totalGroups} processadas`;
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
      <strong>${esc(currentGroup ? `Etapa ${currentGroup.displayNumber ?? currentGroup.number} · ${currentGroup.label}` : runStatusLabel(run.status))}</strong>
      <small>${currentGroup?.detail ? esc(currentGroup.detail) : 'Nenhuma operação em andamento'}</small>
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

  $('#run-steps').innerHTML = groups.map(group => {
    const statusClass = stageClass(group.status);
    const circleContent = group.status === 'DONE' ? '✓' : String(group.displayNumber ?? group.number);
    return `
      <div class="run-step ${statusClass}" title="${esc(group.detail || stageLabel(group.status))}">
        <div class="run-step-circle">${circleContent}</div>
        <div class="run-step-label">${esc(group.label)}</div>
        <div class="run-step-status">${esc(stageLabel(group.status))}</div>
      </div>
    `;
  }).join('');

  renderGroupDetail(currentGroup);

  if(currentGroup?.status === 'RUNNING'){
    requestAnimationFrame(() => {
      document.querySelector('.run-step.running')?.scrollIntoView({behavior:'smooth', inline:'center', block:'nearest'});
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
let pollFailures=0,pollDelay=1000;
function schedulePoll(ms){clearInterval(pollTimer);pollTimer=setInterval(pollRun,ms)}
async function pollRun(){
  if(!currentRunId)return;
  try{
    const run=await api(`/runs/${currentRunId}`);
    pollFailures=0;pollDelay=1000;
    renderRun(run);
    if(['COMPLETED','CANCELLED','FAILED'].includes(run.status))await finishRun(run);
    else schedulePoll(pollDelay);
  }catch(err){
    if(/sess/i.test(err.message)||err.status===401){clearInterval(pollTimer);pollTimer=null;return}
    pollFailures+=1;
    if(pollFailures>=10){clearInterval(pollTimer);pollTimer=null;$('#submit').disabled=false;$('#progress').textContent=`Erro ao acompanhar execução: ${err.message}`;return}
    pollDelay=Math.min(pollDelay*2,15000);
    schedulePoll(pollDelay);
    $('#progress').textContent=`Conexão instável… tentando novamente em ${Math.round(pollDelay/1000)}s (tentativa ${pollFailures}).`;
  }
}

async function loadHistory(){try{const rows=await api('/reports/history');const box=$('#history-list');if(!rows.length){box.innerHTML='<span class="note">Nenhum relatório salvo.</span>';return}box.innerHTML='';rows.forEach(row=>{const wrap=document.createElement('div');wrap.className='history-entry';const open=document.createElement('button');open.className='history-item';open.innerHTML=`<strong>${esc(row.topic)}</strong><span>${esc(row.generated_at||'')} · QA ${esc(row.qa_status||'PENDING')}</span>`;open.onclick=async()=>{const d=await api(`/reports/history/${row.id}`);currentProjectId=row.id;render(d.report)};const del=document.createElement('button');del.className='danger history-delete';del.textContent='×';del.onclick=async()=>{if(!confirm('Excluir esta versão e seus dados associados?'))return;await api(`/reports/history/${row.id}`,{method:'DELETE'});loadHistory()};wrap.append(open,del);box.appendChild(wrap)})}catch(e){$('#history-list').textContent=e.message}}

$('#report-form').addEventListener('submit',async e=>{e.preventDefault();const btn=$('#submit'),progress=$('#progress');btn.disabled=true;progress.classList.remove('hidden');$('#run-tracker').classList.add('hidden');try{
  progress.textContent='Preparando projeto…';
  const payload={topic:$('#topic').value.trim(),execution_profile:$('#execution-profile').value||'AUTO'};
  for(const [id,key] of [['collection-start','collection_start'],['collection-end','collection_end'],['event-start','event_start'],['event-end','event_end']]){if($(`#${id}`).value)payload[key]=$(`#${id}`).value}
  const created=await api('/projects',{method:'POST',body:JSON.stringify(payload)});currentProjectId=created.id;
  const started=await api(`/projects/${created.id}/run-async`,{method:'POST'});currentRunId=started.run.run_id;renderRun(started.run);progress.textContent='Relatório em processamento.';
  if(pollTimer)clearInterval(pollTimer);pollFailures=0;pollDelay=1000;schedulePoll(1000);await pollRun();
}catch(err){progress.textContent=`Erro: ${err.message}`;btn.disabled=false;$('#stop-report').classList.add('hidden')}});

$('#stop-report').onclick=async()=>{if(!currentRunId)return;try{const response=await api(`/runs/${currentRunId}/cancel`,{method:'POST'});renderRun(response.run);$('#progress').textContent='Cancelamento solicitado. A execução será encerrada no próximo ponto seguro.'}catch(e){alert(e.message)}};
$('#refresh-report').onclick=async()=>{if(!currentProjectId)return alert('Abra ou gere um relatório primeiro.');try{const d=await api(`/reports/history/${currentProjectId}`);render(d.report)}catch(e){alert(e.message)}};
$('#save-pdf').onclick=()=>{if(!currentProjectId)return;window.open(`/projects/${currentProjectId}/export.pdf`,'_blank')};
$('#save-draft').onclick=()=>{if(!currentProjectId)return;window.open(`/projects/${currentProjectId}/export-draft.pdf`,'_blank')};
loadHistory();
