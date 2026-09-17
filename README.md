# Relatório de Repercussão Midiática — ISP v2.3

Esta versão separa três objetos que antes estavam misturados:

1. **fato ocorrido**;
2. **fonte que comprova o fato**;
3. **item de repercussão midiática**.

A v2.3 adiciona **acompanhamento visual da execução**, **cancelamento do relatório em andamento**, **perfis de execução**, **orçamentos de busca e de IA**, **fallback DuckDuckGo → Tavily** e **validação cruzada Tavily × YouTube**.

## Principais mudanças

- `Project.project_type`: `INSTITUTIONAL_PRODUCT`, `EVENT_TOPIC` ou `GENERAL_TOPIC`;
- janela factual (`event_start`/`event_end`) separada da janela de repercussão (`collection_start`/`collection_end`);
- `FactEvent` e `FactAssertion`, com proveniência por campo;
- dois estados independentes em `MediaItem`: `status` para repercussão e `fact_status` para camada factual;
- consultas com propósito explícito: `MEDIA_REPERCUSSION`, `FACT_DISCOVERY`, `OFFICIAL_FACT` e `NOMINAL_FOLLOWUP`;
- busca em duas passagens: temática e depois nominal pelas pessoas descobertas;
- conflitos entre fontes não são resolvidos silenciosamente;
- QA determinístico + QA por LLM antes de liberar o PDF oficial;
- PDF e interface com seção **Camada de Fatos Verificados** e anexo de evidências factuais;
- O Dia, R7 e Band adicionados às fontes prioritárias, além das fontes oficiais de segurança do RJ;
- execução assíncrona com painel de etapas;
- botão **Parar relatório** com cancelamento cooperativo;
- OpenAI como único provedor de IA;
- tema ocupando toda a largura do formulário;
- os quatro campos de data aparecem em uma única linha em telas de desktop;
- **perfis de execução** com dependências lógicas e overrides individuais;
- **orçamentos** globais de busca, YouTube e chamadas de IA (com lotes);
- **fallback automático** DuckDuckGo → Tavily;
- **circuit breaker** do Tavily: após erro de quota/rate limit, as consultas seguintes ficam só com o DuckDuckGo;
- **validação cruzada** entre Tavily e YouTube para vídeos encontrados pelos dois coletores;
- **busca focada no objeto monitorado**: produtos institucionais geram consultas com âncora nominal mínima, sem termos genéricos isolados ("dossie", "relatorio", "instituto").

## Acompanhamento da execução

Cada etapa possui um estado visual:

- **cinza**: ainda não iniciada;
- **laranja**: em execução;
- **verde**: concluída;
- **vermelho**: falha ou interrupção;
- cinza esmaecido: etapa não necessária.

Etapas acompanhadas:

```text
Perfil do tema
Planejamento de buscas
Coleta em sites
Coleta no YouTube
Validação cruzada Tavily × YouTube
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

O botão **Parar relatório** solicita interrupção e a execução é encerrada no próximo ponto seguro. Consultas e itens já persistidos permanecem no banco para auditoria.

## Como testar sobre o projeto atual

1. Faça backup do banco atual.
2. Substitua os arquivos pelos desta pasta.
3. Não copie `repercussao.db` para o Git.
4. Copie o exemplo de ambiente:

```powershell
Copy-Item .env.example .env
```

5. Preencha ao menos:

```dotenv
TAVILY_API_KEY=...
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4.1-mini
```

6. Fora do Docker, se estiver usando PostgreSQL local, ajuste `DATABASE_URL`. Se estiver usando seu SQLite atual, preserve a URL já configurada no `.env`.

7. Execute:

```powershell
uvicorn app.main:app --reload
```

ou, usando Docker:

```powershell
docker compose up -d --build
```

8. Abra:

```text
http://127.0.0.1:8000
```

A inicialização executa uma **migração aditiva** (`app/schema_upgrade.py`) para acrescentar as novas colunas sem apagar o corpus existente e cria as tabelas novas de fatos.

## Tema recomendado para o primeiro teste

```text
policiais mortos em agosto de 2026 no Rio de Janeiro
```

Se você não informar datas no formulário, o sistema reconhece `agosto de 2026` e configura automaticamente:

- fatos: `2026-08-01` a `2026-08-31`;
- repercussão: `2026-08-01` a `2026-08-31`;
- confirmação factual: usa internamente até 10 dias após o fim da janela, sem contar esses itens como repercussão de agosto. Esse ajuste não é exposto na interface.

## Limites e orçamentos

As variáveis opcionais mais comuns estão no `.env.example`:

- `MAX_SEARCH_RESULTS` — limite **global** de novos itens coletados pela etapa web (padrão 30; não é por consulta);
- `MAX_RESULTS_PER_QUERY` — teto de resultados por consulta (padrão 5);
- `MAX_SEARCH_QUERIES` — consultas executadas na primeira coleta (padrão 12), com `MAX_LLM_SEARCH_QUERIES` reservado às complementares da IA;
- `MAX_SEMANTIC_REVIEWS`, `MAX_FACT_EXTRACTIONS`, `MAX_CLASSIFICATIONS`, `MAX_CROSS_VALIDATIONS` — orçamento de **itens** de IA por execução;
- `VALIDATION_BATCH_SIZE`, `CLASSIFICATION_BATCH_SIZE` — tamanho dos lotes enviados à OpenAI (padrão 10). Ex.: 40 itens com lote 10 geram no máximo 4 chamadas na validação e 4 na classificação;
- `MAX_YOUTUBE_TASKS`, `MAX_YOUTUBE_RESULTS_TOTAL`, `MAX_YOUTUBE_RESULTS_PER_TASK` — limites globais do YouTube. Os canais prioritários são sempre preservados na checagem; o primeiro controla apenas quantas buscas temáticas adicionais entram.

## Coleta no YouTube

A coleta no YouTube segue uma cadeia por tarefa de busca: **DuckDuckGo Videos → Tavily (restrito a `youtube.com`/`youtu.be`) → YouTube Data API**. Um provedor só é abandonado quando não devolve nenhum resultado utilizável (novo ou duplicado válido) para a tarefa corrente, e a identidade dos canais prioritários é confirmada localmente.

Quando Tavily e outro coletor encontrarem o mesmo vídeo, um agente de validação
cruzada compara URL, título, canal, data e descrição que cada coletor forneceu.
O resultado (`CONFIRMED`, `PARTIALLY_CONFIRMED`, `CONFLICT` ou
`INSUFFICIENT_EVIDENCE`) fica registrado no item, com a justificativa e é
resumido no PDF. Itens com `CONFLICT` são excluídos das tabelas de cobertura e
dos rankings até revisão. Ausência de metadado no Tavily não é tratada como conflito.

Quando a página ou o resultado da busca expõe uma contagem explícita, ela é
registrada como fotografia da coleta; caso contrário, o campo permanece ausente.
O ranking é portanto o dos canais na amostra validada, não um ranking exaustivo
do YouTube.

## Coleta em sites

A coleta web usa duas fontes, sempre nesta ordem:

- **DuckDuckGo** como provedor primário (índice de notícias para repercussão e busca textual para o restante);
- **Tavily** como fallback quando o DuckDuckGo não devolve resultado utilizável ou falha.

Se o Tavily rejeitar apenas a janela temporal, a mesma consulta é repetida uma vez sem `start_date`/`end_date`. Erros de quota/rate limit abrem um **circuit breaker** e o restante da execução segue apenas com o DuckDuckGo. Todos os filtros temáticos (âncora nominal do produto) valem também para os resultados do fallback.

## Perfis de execução

`Project.execution_profile` controla quais camadas rodam. `AUTO` resolve em tempo de execução a partir do perfil temático:

- `MIDIATICO_SIMPLES` — só repercussão midiática web + YouTube;
- `MIDIATICO_COM_FATOS` — repercussão + camada factual;
- `COMPLETO_NOMINAL` — repercussão + fatos + busca nominal pelos nomes descobertos.

Resolução automática: produtos institucionais → `MIDIATICO_SIMPLES`; `EVENT_TOPIC` com nome de vítima → `COMPLETO_NOMINAL`; demais -> `MIDIATICO_COM_FATOS`. Overrides individuais (`enable_youtube`, `enable_fact_layer`, `enable_nominal_followup`, `enable_cross_validation`) podem ser passados na criação do projeto, com dependências lógicas: sem camada factual não há busca nominal, e sem YouTube não há validação cruzada. O endpoint `GET /health` expõe os limites efetivos em `search_limits` e `ai_limits`.

## Tema recomendado para o primeiro teste

```text
policiais mortos em agosto de 2026 no Rio de Janeiro
```

Se você não informar datas no formulário, o sistema reconhece `agosto de 2026` e configura automaticamente:

- fatos: `2026-08-01` a `2026-08-31`;
- repercussão: `2026-08-01` a `2026-08-31`;
- confirmação factual: usa internamente até 10 dias após o fim da janela, sem contar esses itens como repercussão de agosto. Esse ajuste não é exposto na interface.

## Pipeline v2.3

```text
Tema
  -> perfil do tema
  -> consultas de repercussão
  -> consultas factuais
  -> consultas oficiais
  -> coleta (DuckDuckGo → Tavily)
  -> coleta no YouTube (DuckDuckGo Videos → Tavily → API)
  -> validação cruzada Tavily × YouTube
  -> extração factual com evidência por campo
  -> resolução determinística
  -> busca nominal pelos nomes descobertos
  -> segunda coleta factual
  -> validação da repercussão (em lotes)
  -> classificação analítica (em lotes)
  -> redação
  -> QA
  -> REPORT_READY ou REPORT_NEEDS_REVIEW
  -> PDF oficial somente se QA aprovado
```

## Endpoints de execução

- `GET /health` — estado, versão e limites efetivos (`search_limits` e `ai_limits`);
- `POST /projects/{id}/run-async` — inicia a execução em segundo plano;
- `GET /runs/{run_id}` — retorna estado geral e estado de cada etapa;
- `POST /runs/{run_id}/cancel` — solicita o cancelamento;
- `POST /projects/{id}/run` — execução síncrona mantida para compatibilidade.

## Outros endpoints novos/alterados

- `GET /projects/{id}/facts`
- `GET /projects/{id}/facts/{event_id}/evidence`
- `POST /projects/{id}/qa`
- `GET /projects/{id}/export.pdf` — bloqueia quando QA não aprova
- `GET /projects/{id}/export-draft.pdf` — rascunho para revisão interna

## Estados factuais

- `CONFIRMED`
- `PARTIALLY_CONFIRMED`
- `SOURCE_CONFLICT`
- `NOT_FOUND_IN_SAMPLE`

A ausência de um valor na amostra nunca é convertida automaticamente em “não divulgado” ou “não ocorreu”.

## Observação importante sobre escopo

`primary_scope` é triestado:

- `true`: núcleo principal da pesquisa;
- `false`: caso relacionado, mas fora do núcleo;
- `null`: escopo ainda pendente de revisão.

Isso é útil em temas como policiais mortos, em que ex-policiais, aposentados e agentes ativos podem exigir regras diferentes.
