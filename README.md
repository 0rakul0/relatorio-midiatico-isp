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
DuckDuckGo → Tavily
  ↓
Persistência e auditoria
  ↓
Validação do corpus
  ↓
Camada factual opcional
  ↓
Classificação
  ↓
Redação do relatório
  ↓
QA
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

Services não acessam diretamente DuckDuckGo ou Tavily.

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
SERVICE → Tavily
SERVICE → provider
SERVICE → tool.invoke()
```

A execução das tools acontece dentro do agente.

---

## Estrutura principal

```text
app/
├── agent.py
├── llm.py
├── schemas.py
├── config.py
├── models.py
│
├── tools/
│   ├── __init__.py
│   ├── registry.py
│   ├── search.py
│   └── providers/
│       ├── duckduckgo.py
│       └── tavily.py
│
├── services/
│   ├── execution_profile.py
│   ├── project_profile.py
│   ├── search_planning.py
│   ├── validation.py
│   ├── classification.py
│   ├── reporting.py
│   ├── metrics.py
│   ├── cache.py
│   ├── pipeline.py
│   │
│   └── collection/
│       ├── common.py
│       ├── guards.py
│       ├── orchestrator.py
│       ├── persist.py
│       ├── web.py
│       ├── youtube.py
│       └── youtube_helpers.py
│
├── orchestration/
│   ├── state.py
│   └── executor.py
│
├── fact_layer.py
├── topic_profile.py
├── media_scout.py
├── report_qa.py
├── pdf_report.py
├── schema_upgrade.py
│
└── static/
    ├── index.html
    ├── app.js
    └── styles.css
```

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

## Pesquisa web

Prioridade:

```text
DuckDuckGo
    ↓
resultado utilizável?
    ├── sim → encerra
    └── não
         ↓
      Tavily
```

O DuckDuckGo é sempre o provedor principal.

Tavily atua como fallback.

---

## Pesquisa de vídeos

A busca de vídeos utiliza:

```text
DuckDuckGo Videos
        ↓
resultado utilizável?
        ├── sim → encerra
        └── não
             ↓
          Tavily
```

O Tavily é restringido a resultados compatíveis com YouTube quando utilizado nessa camada.

A aplicação **não utiliza YouTube Data API**.

---

# Circuit breaker do Tavily

Falhas graves do Tavily, como problemas de quota ou rate limit, abrem um circuit breaker.

Exemplos:

```text
429
quota exceeded
usage limit
rate limit
```

Após a abertura do breaker, novas tentativas de Tavily na execução são evitadas e o sistema continua priorizando DuckDuckGo.

O estado e os contadores do breaker também são incorporados às estatísticas da coleta.

---

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
Redação do relatório
Auditoria QA final
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

## Criar ambiente

No PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Ou utilizando `uv`:

```powershell
uv venv
.\.venv\Scripts\Activate.ps1
```

---

## Instalar dependências

```powershell
pip install -r requirements.txt
```

ou:

```powershell
uv pip install -r requirements.txt
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

TAVILY_API_KEY=
```

DuckDuckGo não exige chave.

Tavily é opcional e atua como fallback.

---

## Limites principais

Exemplo:

```dotenv
MAX_SEARCH_RESULTS=250
MAX_SEARCH_QUERIES=50
MAX_RESULTS_PER_QUERY=5

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
fallback de providers
circuit breaker Tavily
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

## 2. DuckDuckGo primeiro

```text
DuckDuckGo → Tavily
```

Tavily é fallback.

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

# Limitações e próximos passos

A arquitetura atual já implementa o núcleo do MVP auditável, mas ainda existem melhorias planejadas:

* circuit breaker do Tavily isolado por execução;
* bloqueio transacional de runs simultâneos em múltiplos workers;
* recuperação ou marcação de runs interrompidos após restart;
* auditoria `SearchCall` também para buscas opcionais do documentalista;
* política rígida de consultas permitidas nas tools;
* validação mais forte de canal prioritário em fallback Tavily;
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
