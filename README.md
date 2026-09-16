# Relatório de Repercussão Midiática — ISP v2.2

Esta versão separa três objetos que antes estavam misturados:

1. **fato ocorrido**;
2. **fonte que comprova o fato**;
3. **item de repercussão midiática**.

Além disso, a v2.2 adiciona **acompanhamento visual da execução** e **cancelamento do relatório em andamento**.

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
- os quatro campos de data aparecem em uma única linha em telas de desktop.

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

## Pipeline v2.2

```text
Tema
  -> perfil do tema
  -> consultas de repercussão
  -> consultas factuais
  -> consultas oficiais
  -> coleta
  -> extração factual com evidência por campo
  -> resolução determinística
  -> busca nominal pelos nomes descobertos
  -> segunda coleta factual
  -> validação da repercussão
  -> classificação analítica
  -> redação
  -> QA
  -> REPORT_READY ou REPORT_NEEDS_REVIEW
  -> PDF oficial somente se QA aprovado
```

## Endpoints de execução

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
