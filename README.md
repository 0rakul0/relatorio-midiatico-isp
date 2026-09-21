# Relatório de Repercussão Midiática — ISP

MVP auditável para coleta, validação, classificação e análise de repercussão midiática relacionada ao Instituto de Segurança Pública do Estado do Rio de Janeiro.

A aplicação foi projetada para manter separadas três camadas que não devem ser confundidas:

1. **o fato ocorrido**;
2. **a fonte que sustenta ou comprova o fato**;
3. **o item de repercussão midiática**.

O sistema utiliza um único agente LLM, ferramentas de pesquisa controladas, regras determinísticas de validação e persistência de proveniência para produzir relatórios rastreáveis e passíveis de auditoria.

---

## Visão geral

O fluxo principal é:

```text
Tema
  ↓
Perfil do tema
  ↓
Planejamento de consultas
  ↓
ReportAgent
  ↓
Tools de pesquisa
   ↓
DuckDuckGo (provedor único)
   ↓
Persistência e auditoria
  ↓
Validação do corpus
  ↓
Camada factual opcional
  ↓
Classificação
   ↓
Cobertura complementar (fase 2, se houver lacuna)
   ↓
Redação do relatório
   ↓
QA (+ revisão automática)
   ↓
PDF / relatório aprovado
```

A aplicação diferencia temas do tipo:

```text
INSTITUTIONAL_PRODUCT
EVENT_TOPIC
GENERAL_TOPIC
```

Exemplos:

```text
Dossiê Mulher 2026
→ INSTITUTIONAL_PRODUCT

policiais mortos em agosto de 2026 no Rio de Janeiro
→ EVENT_TOPIC

segurança pública no Rio de Janeiro
→ GENERAL_TOPIC
```

---

# Arquitetura

A arquitetura segue uma regra central:

> Toda pesquisa externa deve ser solicitada pelo `ReportAgent` através de uma tool.

Services não acessam diretamente o DuckDuckGo.

O fluxo permitido é:

```text
SERVICE
   ↓
REPORT AGENT
   ↓
TOOL
   ↓
PROVIDER
```

Não deve existir:

```text
SERVICE → DuckDuckGo
SERVICE → provider
SERVICE → tool.invoke()
```

A execução das tools acontece dentro do agente (`app/agent.py:354`).

## Visão em camadas

```mermaid
flowchart TB
    subgraph UI[Interface]
        Static["app/static<br/>index.html / app.js"]
    end

    subgraph API[API - app/main.py]
        FastAPI["FastAPI<br/>/projects /run-async /runs/{id}<br/>/facts /reports /costs /export.pdf"]
    end

    subgraph ORCH[Orquestração - app/orchestration/]
        Executor["executor.py<br/>start_run() + Thread worker"]
        State["state.py<br/>stages + cancel cooperativo"]
        ReportRunDB[("ReportRun<br/>status persistido")]
    end

    subgraph PIPE[Pipeline - app/services/pipeline.py]
        Pipeline["run_full_methodology()<br/>13 stages"]
    end

    subgraph SVC[Services determinísticos]
        Profile["project_profile.py<br/>topic_profile.py"]
        Plan["search_planning.py<br/>execution_profile.py"]
        Collect["collection/<br/>web.py youtube.py<br/>orchestrator.py guards.py<br/>persist.py common.py"]
        Validate["news_validation.py<br/>validation.py<br/>article_hydration.py<br/>academic_research.py"]
        Facts["fact_layer.py"]
        Classify["classification.py<br/>metrics.py"]
        Write["reporting.py<br/>report_qa.py<br/>pdf_report.py"]
        Cache["cache.py<br/>corpus_reuse.py"]
    end

    subgraph AGENT[Um único agente - app/agent.py]
        ReportAgent["ReportAgent<br/>13 tasks: topic_profile,<br/>documentalist, collector,<br/>classification, report_writer, qa..."]
    end

    subgraph TOOLS[Tools - app/tools/]
        Registry["registry.py<br/>build_agent_tools()"]
        SearchTools["search.py / hydration.py / academic.py<br/>pesquisar_internet<br/>pesquisar_videos<br/>pesquisar_artigos_arxiv<br/>executar_buscas_web/videos<br/>hidratar_artigos"]
    end

    subgraph PROV[Providers - app/tools/providers/]
        DDG["duckduckgo.py<br/>web + videos"]
        ARXIV["arxiv.py<br/>literatura científica"]
    end

    subgraph DATA[Persistência - app/models.py]
        DB[("PostgreSQL 16 / SQLite<br/>Project SearchQuery SearchCall<br/>SearchHit MediaItem FactEvent<br/>FactAssertion Classification<br/>GeneratedReport LLMCall<br/>CorpusDocument")]
    end

    subgraph LLM[LLM - app/llm.py]
        OpenAI["OpenAI gpt-4.1-mini<br/>+ cost_tracker.py"]
    end

    Static --> FastAPI
    FastAPI --> Executor
    Executor --> State
    State --> ReportRunDB
    Executor --> Pipeline
    Pipeline --> SVC
    SVC --> ReportAgent
    ReportAgent --> Registry
    Registry --> SearchTools
    SearchTools --> DDG
    SearchTools --> ARXIV
    SVC --> DB
    ReportAgent --> OpenAI
    ReportAgent --> DB
```

Em texto:

```text
app/static (dashboard)
   ↓ HTTP
app/main.py (FastAPI)
   ↓ start_run(project_id)
app/orchestration/executor.py (Thread) + state.py (stages/cancel)
   ↓ run_full_methodology()
app/services/pipeline.py (13 stages, perfil → QA)
   ↓ prepara contexto / sinks / observers
app/agent.py (ReportAgent.run task + payload + schema Pydantic)
   ↓ llm.bind_tools() — a LLM decide se chama
app/tools/registry.py → search.py / hydration.py
   ↓
app/tools/providers/duckduckgo.py (provedor único)
   ↓ SearchHit / MediaItem / SearchCall / LLMCall
PostgreSQL (app/models.py) + PDF/QA
```

## Ciclo de vida de uma execução assíncrona

```mermaid
sequenceDiagram
    participant UI as Dashboard
    participant API as main.py
    participant EXE as orchestration/executor
    participant PIPE as services/pipeline
    participant AG as ReportAgent
    participant TOOL as Tool + Provider
    participant DB as Postgres

    UI->>API: POST /projects (tema + janelas)
    API->>DB: Project(DRAFT/CUSTOM_DATES)
    UI->>API: POST /projects/{id}/run-async
    API->>EXE: start_run(project_id)
    EXE->>DB: ReportRun(PENDING→RUNNING)
    EXE->>PIPE: run_full_methodology(progress_callback, cancel_check)
    PIPE->>AG: topic_profile / documentalist / report_planner
    AG->>DB: Project.topic_profile + execution_plan
    PIPE->>AG: collector (executar_buscas_web/videos)
    AG->>TOOL: 1 chamada bulk com plano completo
    TOOL->>DB: SearchCall + SearchHit + MediaItem + SearchQuery status
    PIPE->>AG: article_hydrator + media_relevance + fact_extraction...
    AG->>DB: MediaItem validado + FactEvent/Assertion + Classification
    PIPE->>AG: report_writer (sem tools) + qa (sem tools)
    AG->>DB: GeneratedReport + LLMCall (custos)
    EXE->>DB: ReportRun(DONE/FAILED/CANCELLED)
    UI->>API: GET /runs/{run_id} (poll stages + costs)
    UI->>API: GET /projects/{id}/export.pdf
```

Cancelamento é cooperativo (`POST /runs/{id}/cancel`): o worker checa `check_cancelled()` entre stages e preserva o que já foi persistido para auditoria.

## Pipeline — etapas acompanhadas (`app/services/pipeline.py`)

```mermaid
flowchart LR
    P["profile<br/>descobre tipo:<br/>INSTITUTIONAL/<br/>EVENT/GENERAL"] --> SP["search_plan<br/>corpus_reuse +<br/>report_planner"]
    SP --> C["collection<br/>web leve:<br/>hits brutos"] --> Y["youtube<br/>DDG Videos"]
    Y --> AR["academic_research<br/>arXiv, opcional"]
    AR --> V["validation<br/>hydrate +<br/>media_relevance"]
    V --> F1["facts_pass_1<br/>extract"] --> R1["fact_resolution_1<br/>CONFIRMED/CONFLICT"]
    R1 --> NP["nominal_plan"] --> NC["nominal_collection<br/>2ª coleta"]
    NC --> F2["facts_pass_2"] --> R2["fact_resolution_2"]
    R2 --> CL["classification<br/>tema/tom/fidelidade"] --> GF["gap_fill (fase 2)<br/>lacunas → gap_planner → web aberta"] --> RP["report<br/>sem tools"] --> QA["qa<br/>determinístico+LLM+revisão"]
```

Stages opcionais recebem `SKIPPED` com razão auditável quando o plano (`execution_plan.processes`) desabilita `youtube_collection`, `fact_extraction`, `nominal_followup`, etc. Ver `app/services/execution_profile.py`.

## Modelo de dados simplificado (`app/models.py`)

```mermaid
erDiagram
    Project ||--o{ SearchQuery : planeja
    Project ||--o{ MediaItem : contém
    Project ||--o{ FactEvent : estrutura
    Project ||--o{ GeneratedReport : gera
    Project ||--o{ ReportRun : executa
    SearchQuery ||--o{ SearchCall : audita
    SearchQuery ||--o{ SearchHit : retorna
    SearchHit }o--|| MediaItem : consolida-por-URL
    MediaItem ||--o{ Classification : classifica
    MediaItem ||--o{ FactAssertion : sustenta
    FactEvent ||--o{ FactAssertion : possui
    CorpusDocument ||--o{ ProjectCorpusLink : reutiliza
    ProjectCorpusLink }o--|| MediaItem : vincula
    Project ||--o{ LLMCall : custa
```

Principais tabelas: `projects`, `search_queries`, `search_calls`, `search_hits` (bruto imutável + `technical_flags`), `media_items` (consolidado por `canonical_url`), `corpus_documents` (reuso histórico global), `fact_events` + `fact_assertions`, `classifications`, `generated_reports` (snapshot imutável), `report_runs`, `llm_calls`.

## Estrutura principal

```text
app/
├── main.py                 # FastAPI: projects, run/run-async, facts, reports, costs, PDF
├── agent.py                # ReportAgent único (13 tasks, bind_tools, saída Pydantic)
├── llm.py + cost_tracker.py# criação do chat model + registro LLMCall (tokens/custo)
├── schemas.py              # contratos Pydantic das tasks do agente
├── config.py               # Settings (limites de busca, lotes LLM, YouTube, corpus reuse)
├── models.py               # SQLAlchemy: Project, SearchQuery/Call/Hit, MediaItem, Facts...
├── database.py             # Session/engine (Postgres + SQLite dev)
│
├── tools/
│   ├── registry.py         # build_agent_tools(web/video/article_fetch/academic, bulk)
│   ├── search.py           # pesquisar_internet/videos + executar_buscas_web/videos (lote)
│   ├── hydration.py        # hidratar_artigos (fetch paralelo com anti-SSRF)
│   ├── academic.py         # pesquisar_artigos_arxiv
│   ├── providers/arxiv.py  # API pública Atom do arXiv
│   └── providers/
│       └── duckduckgo.py   # provedor único (web + videos)
│
├── services/
│   ├── pipeline.py         # run_full_methodology() — orquestra os 13 stages
│   ├── project_profile.py  # discover_project_profile() (topic_profile + documentalist)
│   ├── search_planning.py  # plan_report_with_llm() + plan_queries()
│   ├── execution_profile.py# AUTO → MIDIATICO_SIMPLES / COM_FATOS / COMPLETO_NOMINAL
│   ├── corpus_reuse.py     # reuse_prior_corpus() — reaproveita CorpusDocument
│   ├── article_hydration.py# hydrate_media_items() — corpo completo p/ validação
│   ├── news_validation.py  # validate_news_stage() — triagem media_relevance
│   ├── validation.py       # validate_and_classify() e guardas de aderência
│   ├── classification.py   # classify_with_llm() — tema/enquadramento/tom
│   ├── reporting.py        # draft_report_with_llm() + export_report_pdf()
│   ├── metrics.py          # agregações p/ redação e dashboard
│   ├── cache.py            # cached_report_for_* (snapshot, sem recalcular)
│   │
│   └── collection/
│       ├── orchestrator.py # collect_media_sources() — web + youtube
│       ├── web.py          # collect_web() via agente collector
│       ├── youtube.py      # coleta de vídeos (DDG Videos)
│       ├── guards.py       # regras determinísticas (âncora, janela, domínio, dedup)
│       ├── persist.py      # SearchHit → MediaItem + proveniência + media_origin
│       ├── media_origin.py # PORTAL_NOTICIAS / REDE_SOCIAL / YOUTUBE
│       ├── common.py       # canonicalize(), query_window()
│       └── youtube_helpers.py
│
├── orchestration/
│   ├── executor.py         # start_run() — Thread worker + cost_context
│   └── state.py            # stages PENDING/RUNNING/DONE/SKIPPED/FAILED/CANCELLED
│
├── fact_layer.py           # extract/resolve_project_facts(), plan_nominal_followups()
├── topic_profile.py        # janelas event_* vs collection_*, âncoras nominais
├── media_scout.py          # descoberta complementar de mídia
├── report_qa.py            # QA determinístico + LLM (CRITICAL/HIGH/MEDIUM/LOW)
├── pdf_report.py           # PDF oficial (só se aprovado) vs rascunho
├── schema_upgrade.py       # ensure_schema() — migração aditiva (futuro: Alembic)
│
└── static/
    ├── index.html
    ├── app.js
    └── styles.css
```

| Camada | Pasta | Papel | Não pode fazer |
|---|---|---|---|
| API | `app/main.py`, `app/static/` | HTTP, validação de entrada, `cost_context` | chamar provider direto |
| Orquestração | `app/orchestration/` | threads, stages, cancel | chamar LLM/provider |
| Metodologia | `app/services/pipeline.py` | ordem dos stages, flags do plano | chamar `tool.invoke()` |
| Inteligência | `app/agent.py` | único lugar com `bind_tools` + prompts por task | acessar DB direto |
| Acesso externo | `app/tools/` | tools + `SearchSink/Observer` + SSRF guard | decidir metodologia |
| Prova | `app/tools/providers/` | SDK DDG isolado | ser chamado por service |
| Prova factual | `app/fact_layer.py`, `app/report_qa.py` | regras `CONFIRMED/CONFLICT`, QA | inventar datas |
| Dados | `app/models.py`, `database.py` | proveniência, snapshots, custos | pesquisa externa |

---

# Um único agente

Toda a lógica LLM passa pelo:

```text
ReportAgent
```

definido em:

```text
app/agent.py
```

Não existem agentes independentes para coleta, documentalista, classificação ou redação.

São tarefas diferentes executadas pelo mesmo agente.

Exemplos:

```bash
get_report_agent().run(
    task="documentalist",
    ...
)

get_report_agent().run(
    task="collector",
    ...
)

get_report_agent().run(
    task="classification",
    ...
)

get_report_agent().run(
    task="report_writer",
    ...
)
```

As diferenças entre as tarefas estão no:

* prompt;
* schema Pydantic;
* contexto;
* tools disponibilizadas.

---

# Tool calling

As tools são opcionais para tarefas analíticas, mas podem ser obrigatórias quando a metodologia define explicitamente um plano de coleta.

O agente recebe as tools por:

```bash
llm.bind_tools(tools)
```

Não é utilizado:

```bash
tool_choice="required"
```

Assim, em tarefas como `documentalist`, a LLM pode decidir:

```text
contexto suficiente
→ não pesquisa

faltam evidências
→ pesquisar_internet
```

Já na tarefa `collector`, o prompt define explicitamente que o plano de consultas deve ser executado integralmente.

---

# Ferramentas de pesquisa

As ferramentas ficam em:

```text
app/tools/search.py
```

As principais tools individuais são:

```text
pesquisar_internet
pesquisar_videos
```

Para a coleta obrigatória são utilizadas ferramentas em lote:

```text
executar_buscas_web
executar_buscas_videos
```

As versões em lote reduzem:

* número de chamadas LLM;
* consumo de tokens;
* latência;
* risco de omissão de consultas planejadas.

---

# Provedores

Os SDKs externos ficam isolados em:

```text
app/tools/providers/
```

O DuckDuckGo (`ddgs`) é o provedor único de pesquisa externa, para web
(`News` → `Text`) e para vídeos (`Videos`).

## Pesquisa web

```text
DuckDuckGo News
    ↓
sem resultado?
    ↓
DuckDuckGo Text
```

## Pesquisa de vídeos

A busca de vídeos utiliza `DuckDuckGo Videos`.

A aplicação **não utiliza YouTube Data API**.

---

# Origem da mídia (portal / redes / YouTube)

Todo link capturado recebe um `media_origin` determinístico em
`SearchHit`/`MediaItem` (`app/services/collection/media_origin.py`):

```text
YOUTUBE         → youtube.com / youtu.be
REDE_SOCIAL     → facebook, instagram, x, tiktok, threads, linkedin...
PORTAL_NOTICIAS → todo o restante (portais, blogs, sites institucionais)
```

A classificação acontece na persistência (`persist.py`), nunca descarta o
item e alimenta `metrics.media_origin_counts`, `corpus_by_origin`
(`portal_noticias` / `redes_sociais` / `youtube`) e o snapshot do relatório.

# Auditoria das pesquisas

Cada consulta planejada possui informações de execução em `SearchQuery`.

Entre os campos auditáveis estão:

```text
executed_at
execution_status
execution_error
providers_attempted
results_returned
results_accepted
```

Estados possíveis incluem:

```text
PENDING
ATTEMPTED
SUCCEEDED
NO_RESULTS
FAILED
SKIPPED
```

Além disso, cada tentativa por provedor gera um:

```text
SearchCall
```

com informações como:

```text
project_id
run_id
search_query_id
tool_name
provider
query
started_at
finished_at
success
error
results_returned
results_accepted
latency_ms
```

Isso permite diferenciar:

```text
consulta não executada

consulta executada sem resultados

consulta executada com resultados

consulta executada com erro
```

---

# Proveniência dos itens

Cada `MediaItem` preserva informações sobre a origem da descoberta.

O sistema registra, entre outros:

```text
search_source
source_provenance
discovery_purposes
retrieved_at
```

Um mesmo item pode ser localizado por consultas ou provedores diferentes sem perder sua trilha de descoberta.

---

# Finalidade das consultas

Cada `SearchQuery` possui um propósito explícito.

Os principais são:

```text
MEDIA_REPERCUSSION
FACT_DISCOVERY
OFFICIAL_FACT
NOMINAL_FOLLOWUP
```

Isso impede misturar:

```text
fonte usada para confirmar um fato
```

com:

```text
item usado para medir repercussão midiática
```

---

# Janelas temporais

A aplicação trabalha com duas janelas independentes.

## Janela factual

```text
event_start
event_end
```

Representa quando o fato ocorreu.

## Janela de repercussão

```text
collection_start
collection_end
```

Representa quando a cobertura midiática será medida.

Essas duas janelas não devem ser confundidas.

---

## Exemplo

Tema:

```text
policiais mortos em agosto de 2026 no Rio de Janeiro
```

O sistema pode inferir:

```text
event_start      = 2026-08-01
event_end        = 2026-08-31

collection_start = 2026-08-01
collection_end   = 2026-08-31
```

A camada factual pode utilizar dias adicionais de confirmação após o fim da janela sem que essas matérias sejam contadas como repercussão do período principal.

---

# Produtos institucionais

Produtos como:

```text
Dossiê Mulher 2026
```

são tratados de forma diferente de pautas factuais.

O ano da edição não é automaticamente interpretado como:

```text
01/01/2026 → 31/12/2026
```

para repercussão.

O sistema tenta primeiro identificar documentalmente:

* existência do produto;
* nome correto;
* edição;
* situação da publicação;
* data de lançamento, quando confirmável.

Essa etapa é executada pelo `ReportAgent` com a tarefa:

```text
documentalist
```

A pesquisa externa é opcional e ocorre somente através da tool:

```text
pesquisar_internet
```

---

# Guardas determinísticos

Nem toda decisão é deixada para a LLM.

Existem regras determinísticas para:

* preservar a âncora nominal;
* rejeitar edição errada de produto;
* verificar domínio solicitado;
* validar janela temporal;
* eliminar URLs inválidas;
* deduplicar resultados;
* validar canais prioritários;
* separar fato de repercussão;
* impedir mistura de períodos.

Essas regras ficam principalmente em:

```text
services/collection/guards.py
services/validation.py
topic_profile.py
fact_layer.py
```

---

# Validação temporal

Quando uma pauta possui janela temporal explícita, um item sem data verificável não entra diretamente como cobertura válida.

Ele pode receber:

```text
DATE_UNVERIFIED
```

Itens fora da janela recebem:

```text
OUTSIDE_COLLECTION_WINDOW
```

A ausência de data nunca é convertida automaticamente em uma data fictícia.

---

# Canonicalização de URLs

A aplicação remove apenas parâmetros conhecidos de rastreamento.

Exemplos removidos:

```text
utm_source
utm_medium
utm_campaign
fbclid
gclid
```

Parâmetros semanticamente importantes são preservados.

Assim:

```text
https://site/a?id=1
```

não é considerado igual a:

```text
https://site/a?id=2
```

---

# Segurança de coleta HTTP

O fetch de páginas externas aplica proteção contra SSRF.

São bloqueados destinos como:

```text
localhost
127.0.0.1
10.0.0.0/8
172.16.0.0/12
192.168.0.0/16
169.254.0.0/16
::1
```

Redirecionamentos também são validados antes de serem seguidos.

Isso evita que resultados de pesquisa sejam utilizados para acessar recursos internos da rede.

---

# Camada factual

Quando habilitada, a camada factual tenta extrair informações como:

```text
nome
instituição
posto/cargo
unidade
data do fato
data da morte
causa
circunstância
local do fato
local da morte
```

Cada informação é armazenada como afirmação vinculada a uma fonte.

A estrutura principal é:

```text
FactEvent
    ↓
FactAssertion
```

---

# Resolução de fatos

A aplicação não resolve conflitos silenciosamente.

Os estados principais são:

```text
CONFIRMED
PARTIALLY_CONFIRMED
SOURCE_CONFLICT
NOT_FOUND_IN_SAMPLE
```

Duas fontes independentes concordando podem confirmar um campo.

Fontes conflitantes resultam em:

```text
SOURCE_CONFLICT
```

e não em escolha arbitrária de um valor.

---

# Identidade factual

O sistema evita fundir automaticamente pessoas ou eventos apenas porque compartilham o mesmo nome.

A comparação considera atributos como:

```text
nome
data
instituição
unidade
cidade
cargo
```

Quando ainda há ambiguidade, eventos permanecem separados e podem ser marcados como possíveis duplicidades para revisão.

---

# Datas relativas

Expressões como:

```text
ontem
na terça-feira
no dia anterior
```

só podem ser resolvidas quando existe uma data de publicação confiável para servir de referência.

Caso contrário, a data é descartada como não confirmada.

---

# Perfis de execução

O projeto suporta diferentes níveis de processamento.

```text
AUTO
MIDIATICO_SIMPLES
MIDIATICO_COM_FATOS
COMPLETO_NOMINAL
```

## MIDIATICO_SIMPLES

Executa principalmente:

```text
perfil
planejamento
coleta
validação
classificação
relatório
QA
```

## MIDIATICO_COM_FATOS

Adiciona:

```text
extração factual
resolução factual
```

## COMPLETO_NOMINAL

Adiciona ainda:

```text
busca nominal
segunda coleta
segunda passagem factual
```

`AUTO` escolhe o perfil a partir do tipo de pauta.

---

# Acompanhamento da execução

A execução assíncrona possui acompanhamento por etapa.

Estados visuais:

```text
PENDING
RUNNING
DONE
SKIPPED
FAILED
CANCELLED
```

Etapas monitoradas:

```text
Perfil do tema
Planejamento de buscas
Coleta em sites
Coleta no YouTube
Validação cruzada
Extração factual - 1ª passagem
Consolidação factual - 1ª passagem
Planejamento de buscas nominais
Coleta nominal
Extração factual - 2ª passagem
Consolidação factual - 2ª passagem
Validação do corpus
Análise e classificação
Cobertura complementar
Redação do relatório
Auditoria QA final (+ revisão automática)
```

---

# Cancelamento

A interface possui:

```text
Parar relatório
```

O cancelamento é cooperativo.

Quando solicitado, a execução é interrompida no próximo ponto seguro.

Dados já persistidos permanecem disponíveis para auditoria.

---

# Persistência dos runs

O estado das execuções é mantido em:

```text
ReportRun
```

São persistidos:

```text
run_id
project_id
status
message
error
cancel_requested
started_at
finished_at
stages
```

Isso permite consultar o estado da execução mesmo fora da memória imediata do processo.

---

# Classificação do corpus

Depois da validação, os itens podem receber classificação analítica contendo:

```text
theme
framing
isp_mentioned
tone_toward_institution
fidelity_status
evidence
errors
```

A classificação é feita pelo `ReportAgent`, mas somente sobre itens previamente validados.

---

# Redação do relatório

A redação também utiliza o mesmo `ReportAgent`.

O agente recebe somente:

* dados do projeto;
* métricas;
* fatos oficiais;
* fatos validados;
* itens do corpus validados;
* classificações.

A redação não recebe ferramentas de pesquisa.

Isso impede que o redator introduza novas evidências externas sem passar pelas etapas anteriores do pipeline.

---

# QA

O relatório passa por duas camadas de QA:

```text
QA determinístico
+
QA por LLM
```

O QA verifica, entre outros:

* números sem sustentação;
* percentuais inconsistentes;
* mistura de períodos;
* uso de fatos excluídos;
* conclusões superiores à evidência;
* URLs ausentes;
* duplicidade;
* conflito entre fonte factual e repercussão.

O PDF oficial só deve ser liberado quando o relatório estiver aprovado.

Caso contrário, pode ser gerado apenas como rascunho para revisão interna.

## Refinamento automático (QA → revisão)

Quando o QA reprova com achados bloqueadores (`CRITICAL`/`HIGH`), o pipeline
não para na reclamação: o task `report_reviser` reescreve o rascunho guiado
pelos achados (correções mínimas, sem inventar dados) e o QA roda de novo —
até `MAX_QA_REFINEMENTS=2` rodadas (cada uma custa 1 redação + 1 QA; `0`
desliga). O que sobrar vai para revisão humana.

`ENABLE_LLM_QA=false` desliga só a camada narrativa (modo econômico);
o determinístico continua obrigatório.

---

# Histórico e snapshots

O relatório persistido armazena um snapshot do contexto utilizado na geração.

São preservados no corpo salvo:

```text
report
metrics
corpus
traditional_corpus
social_corpus
fact_events
fact_evidence
project
```

Ao consultar um relatório antigo, esses dados não são recalculados sobre o estado atual do banco.

Isso evita alterar retrospectivamente um relatório já produzido.

---

# Monitoramento de custos da LLM

Cada chamada OpenAI gera um:

```text
LLMCall
```

com informações como:

```text
project_id
run_id
operation
schema_name
caller
model
input_tokens
output_tokens
cached_input_tokens
cost_usd
success
error
```

Existem endpoints para consulta dos custos globais e por projeto.

---

# Interface

A interface permite:

* informar o tema;
* informar janela de repercussão;
* informar janela factual;
* iniciar relatório;
* acompanhar as etapas;
* cancelar execução;
* acessar histórico;
* visualizar relatório;
* baixar PDF;
* baixar rascunho.

Os quatro campos de data aparecem lado a lado em telas desktop.

---

# Instalação

## Requisitos

Recomendado:

```text
Python 3.12+
PostgreSQL 16+
```

Também existe suporte de desenvolvimento com SQLite.

---

## Criar ambiente e instalar dependências (recomendado, `uv`)

No PowerShell:

```powershell
uv sync
```

Isso cria `.venv` e instala as dependências de `pyproject.toml` + `uv.lock`.
Para incluir as dependências de desenvolvimento (pytest):

```powershell
uv sync --group dev
```

Comandos passam a rodar no env do projeto:

```powershell
uv run --group dev pytest
uv run uvicorn app.main:app --reload
```

Alternativa sem `uv`:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

---

# Configuração

Crie o arquivo:

```text
.env
```

a partir de:

```text
.env.example
```

No PowerShell:

```powershell
Copy-Item .env.example .env
```

Configuração mínima:

```dotenv
DATABASE_URL=postgresql+psycopg://relatorio:relatorio@localhost:5432/repercussao

OPENAI_API_KEY=
OPENAI_MODEL=gpt-4.1-mini
```

DuckDuckGo não exige chave e é o provedor único de pesquisa externa.

---

## Limites principais

Exemplo:

```dotenv
MAX_SEARCH_RESULTS=250
MAX_SEARCH_QUERIES=50
MAX_RESULTS_PER_QUERY=5

# Teto de custo LLM: maximo de URLs NOVAS consolidadas por projeto.
# Hits excedentes viram SearchHit com flag OVER_NEW_ITEM_BUDGET
# (auditoria preservada, sem seguir para validacao/classificacao).
# Itens REUSED do historico nao consomem esse teto.
MAX_NEW_MEDIA_ITEMS=40

# Consultas web abertas da cobertura complementar (fase 2; 1 rodada por projeto)
MAX_GAP_FILL_QUERIES=4

MAX_FACT_SOURCE_CHARS=16000

MAX_SEMANTIC_REVIEWS=250
MAX_FACT_EXTRACTIONS=40
MAX_CLASSIFICATIONS=250
MAX_CROSS_VALIDATIONS=20

MAX_YOUTUBE_TASKS=12
```

Outros limites podem ser configurados através de `app/config.py`.

---

# Executando localmente

```powershell
uvicorn app.main:app --reload
```

Abra:

```text
http://127.0.0.1:8000
```

---

# Docker

Para desenvolvimento:

```powershell
docker compose up -d --build
```

API:

```text
http://127.0.0.1:8000
```

O `docker-compose.yml` atual é voltado principalmente para ambiente de desenvolvimento.

---

# Endpoints principais

## Saúde

```http
GET /health
```

---

## Projetos

```http
POST /projects
```

---

## Descoberta de perfil

```http
POST /projects/{project_id}/discover-profile
```

---

## Planejamento

```http
POST /projects/{project_id}/plan-searches

POST /projects/{project_id}/ai/plan-searches
```

---

## Coleta

```http
POST /projects/{project_id}/collect
```

---

## Execução completa síncrona

```http
POST /projects/{project_id}/run
```

---

## Execução assíncrona

```http
POST /projects/{project_id}/run-async
```

---

## Acompanhamento

```http
GET /runs/{run_id}
```

---

## Cancelamento

```http
POST /runs/{run_id}/cancel
```

---

## Fatos oficiais

```http
POST /projects/{project_id}/official-facts

GET /projects/{project_id}/official-facts
```

---

## Fatos estruturados

```http
GET /projects/{project_id}/facts

GET /projects/{project_id}/facts/{event_id}/evidence
```

---

## Histórico

```http
GET /reports/history

GET /reports/history/{project_id}

DELETE /reports/history/{project_id}
```

---

## Cache

```http
GET /reports/cache
```

---

## Custos

```http
GET /costs

GET /costs/summary

GET /projects/{project_id}/costs
```

---

# Exemplo de teste

Um bom tema para testar a camada factual é:

```text
policiais mortos em agosto de 2026 no Rio de Janeiro
```

O sistema deve reconhecer:

```text
project_type = EVENT_TOPIC
```

e inferir:

```text
event_start = 2026-08-01
event_end   = 2026-08-31
```

Outro teste importante é:

```text
Dossiê Mulher 2026
```

O sistema deve reconhecer:

```text
project_type = INSTITUTIONAL_PRODUCT
product_anchor = Dossiê Mulher
```

sem interpretar automaticamente `2026` como janela de repercussão anual.

---

# Testes

Execute:

```powershell
pytest
```

Os testes atuais cobrem áreas como:

```text
coleta executada pelo agente
busca em lote
auditoria de consultas
provedor único DuckDuckGo
separação portal/redes/YouTube
segurança SSRF
perfil temático
janelas temporais
resolução factual
MediaScout
QA
custos da LLM
```

---

# Migração de banco

A aplicação possui atualmente uma migração aditiva em:

```text
app/schema_upgrade.py
```

Ela preserva bases existentes e adiciona novas tabelas e colunas quando necessário.

No startup:

```text
ensure_schema()
```

é executado automaticamente.

Para ambientes de produção e evolução de schema mais complexa, o próximo passo recomendado é migrar esse mecanismo para Alembic.

---

# Princípios do projeto

## 1. Pesquisa externa somente através do agente

```text
ReportAgent
    ↓
Tool
    ↓
Provider
```

---

## 2. DuckDuckGo como provedor único

```text
DuckDuckGo News → DuckDuckGo Text
DuckDuckGo Videos
```

---

## 3. Fato não é repercussão

Uma fonte posterior pode confirmar um fato sem aumentar a repercussão no período analisado.

---

## 4. Ausência de evidência não é evidência de ausência

O sistema deve preferir:

```text
não localizado na amostra
```

a:

```text
não ocorreu
```

quando não houver sustentação.

---

## 5. Conflitos não são resolvidos silenciosamente

Quando fontes divergem:

```text
SOURCE_CONFLICT
```

---

## 6. O corpus utilizado deve ser auditável

Cada item deve preservar:

```text
fonte
URL
consulta
provider
data de recuperação
finalidade da busca
```

---

## 7. A LLM não substitui regras metodológicas

Decisões como:

```text
janela temporal
deduplicação
âncora nominal
prioridade de fontes
elegibilidade
persistência
```

possuem validações determinísticas.

---

# SaaS: auth, planos e billing

Todos os endpoints exigem Bearer JWT do Supabase Auth (exceto `/health`).
Projetos têm dono (`owner_id`); sem dono (legado), só admin enxerga.
Admin via `ADMIN_EMAILS`.

Planos (`FREE`/`PRO`/`INSTITUCIONAL`) limitam relatórios/mês, US$ LLM/mês
(402 ao estourar, calculado de `report_runs` + `llm_calls`) e recursos
(perfis, YouTube, fatos, gap-fill). `POST /projects` ajusta o pedido ao
plano e avisa em `plan_notice`.

Billing Mercado Pago: `POST /billing/checkout` (501, MOCK) e
`POST /billing/webhook` (MOCK por `user_email`/`user_id`; o real valida
assinatura MP e resolve pelo `preapproval_id`). `GET /billing/me` mostra
plano + uso do mês; `GET /billing/subscriptions` é admin.

---

# Limitações e próximos passos

A arquitetura atual já implementa o núcleo do MVP auditável, mas ainda existem melhorias planejadas:
* bloqueio transacional de runs simultâneos em múltiplos workers;
* recuperação ou marcação de runs interrompidos após restart;
* auditoria `SearchCall` também para buscas opcionais do documentalista;
* política rígida de consultas permitidas nas tools;
* versionamento real de múltiplos relatórios para o mesmo projeto;
* fingerprint completo para cache;
* exposição da auditoria de buscas na interface;
* migração do schema para Alembic;
* CI automatizado;
* migração para `pyproject.toml` e `uv.lock`;
* autenticação dos endpoints administrativos;
* configuração Docker específica para produção.

---

# Status atual

O núcleo arquitetural está organizado em:

```text
ReportAgent
   ↓
Tools
   ↓
Providers
```

com:

```text
Pydantic
+
auditoria de LLM
+
auditoria de pesquisas
+
validação determinística
+
proveniência
+
QA
```
