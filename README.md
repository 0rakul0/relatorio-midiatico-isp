# Relatório de Repercussão Midiática — ISP

## Um pipeline auditável com modelos de linguagem, recuperação semântica e memória de corpus para análise de cobertura jornalística

> **Status:** protótipo de pesquisa / MVP operacional  
> **Versão do software:** 0.4.0  
> **Domínio de aplicação:** monitoramento e análise de repercussão midiática em segurança pública  
> **Instituição de referência:** Instituto de Segurança Pública do Estado do Rio de Janeiro (ISP-RJ)

---

## Resumo

O acompanhamento sistemático da cobertura jornalística sobre segurança pública exige mais do que a recuperação de páginas que contenham determinadas palavras-chave. Uma mesma consulta pode recuperar notícias efetivamente relacionadas ao objeto de interesse, documentos institucionais, republicações, resultados temporalmente incompatíveis, textos apenas tangenciais ao tema e conteúdos que descrevem o fato sem constituírem repercussão midiática. A introdução de modelos de linguagem amplia a capacidade de interpretação desse material, mas também cria problemas de reprodutibilidade, proveniência e auditabilidade quando busca, seleção de fontes, inferência factual e redação são executadas como uma única operação opaca.

Este trabalho apresenta o **Relatório de Repercussão Midiática — ISP**, um sistema para coleta, validação, classificação e síntese de cobertura midiática. A arquitetura combina planejamento estruturado de consultas, pesquisa na Web, persistência dos resultados brutos, recuperação do conteúdo integral das matérias, validação semântica por modelo de linguagem, regras determinísticas, extração factual opcional, classificação temática, controle de qualidade e geração de relatório. O método distingue explicitamente o **fato**, a **evidência que sustenta o fato** e a **matéria que constitui repercussão**, evitando que fontes utilizadas para comprovação sejam automaticamente contabilizadas como cobertura jornalística.

Uma segunda contribuição é a construção incremental de uma memória de corpus. Documentos coletados são normalizados, deduplicados e armazenados globalmente, podendo ser recuperados em pesquisas posteriores por similaridade temática, lexical e semântica. As decisões de validação geram exemplos supervisionados que alimentam um reranker local, mantendo a decisão final sob a camada auditável de validação. Dessa forma, pesquisas anteriores tornam-se conhecimento reutilizável sem assumir que a relevância de uma matéria para um novo tema seja idêntica à decisão tomada no projeto original.

**Palavras-chave:** repercussão midiática; recuperação de informação; modelos de linguagem; RAG; monitoramento de mídia; proveniência; auditoria; segurança pública; recuperação semântica.

---

## 1. Introdução

A análise de repercussão midiática busca responder a uma pergunta diferente de simplesmente identificar se determinado fato ocorreu. O interesse está em observar **como, quando, onde e em que intensidade um objeto foi repercutido pelos meios de comunicação**, quais enquadramentos apareceram e quais fontes participaram dessa circulação.

Essa distinção é especialmente relevante no domínio da segurança pública. Uma fonte oficial pode comprovar uma ocorrência, mas não representa necessariamente repercussão jornalística. De forma análoga, uma notícia pode tratar de um fenômeno associado ao objeto pesquisado sem citar a instituição que motivou o monitoramento. Exigir a presença literal de um nome institucional tende, portanto, a reduzir artificialmente a cobertura observada; aceitar qualquer correspondência lexical, por outro lado, introduz grande quantidade de ruído.

Sistemas contemporâneos de monitoramento em larga escala, como GDELT e Media Cloud, demonstram a utilidade de bases estruturadas e pesquisáveis para estudar atenção, eventos e conteúdo da mídia. O GDELT mantém bases de eventos, menções e conhecimento extraído de notícias, enquanto o Media Cloud foi concebido como infraestrutura aberta para coleta e análise de notícias na Web. Esses projetos evidenciam tanto o potencial da análise computacional da mídia quanto desafios relacionados a qualidade, redundância e definição do corpus.

O presente projeto aborda um problema mais delimitado: **produzir relatórios auditáveis de repercussão midiática a partir de temas definidos pelo usuário**, preservando o caminho entre consulta, resultado bruto, documento consolidado, decisão de relevância, classificação e texto final. A proposta não é substituir bases globais de monitoramento, mas construir uma metodologia orientada a projetos na qual cada afirmação possa ser rastreada até sua evidência.

A arquitetura parte de quatro princípios:

1. **separação entre fato, evidência factual e repercussão midiática**;
2. **preservação da proveniência desde a busca até o relatório**;
3. **combinação de regras determinísticas e interpretação semântica por LLM**;
4. **reutilização do conhecimento coletado sem herdar cegamente decisões anteriores**.

---

## 2. Trabalhos relacionados

### 2.1 GDELT

O **Global Database of Events, Language and Tone (GDELT)** constitui uma das principais infraestruturas computacionais para análise de notícias em escala global. O GDELT 2.0 disponibiliza, entre outros componentes, uma base de eventos, uma tabela de menções e o Global Knowledge Graph (GKG). O sistema permite estudar entidades, locais, temas, volume de cobertura e medidas de tom em grandes coleções de notícias.

A escala do GDELT é uma vantagem quando o objetivo é observar fenômenos globais e séries temporais extensas. Entretanto, estudos sobre a utilização de seus dados também ressaltam a necessidade de considerar erros de codificação, redundância e qualidade antes de empregar automaticamente os registros em análises substantivas.

O sistema proposto neste repositório possui objetivo diferente. Em vez de partir de uma base global previamente codificada, ele registra o **processo de investigação de uma pauta específica**, preservando consultas executadas, retornos do mecanismo de busca, conteúdo recuperado, decisões de inclusão e evidências utilizadas.

### 2.2 Media Cloud

O **Media Cloud** é uma plataforma aberta dedicada à pesquisa de ecossistemas de mídia. Roberts et al. (2021) descrevem sua arquitetura de coleta, armazenamento e organização de notícias, permitindo a criação de conjuntos de dados para investigação quantitativa da mídia. A versão 2.0, apresentada em 2026, descreve uma infraestrutura reengenheirada que ultrapassa 1,8 bilhão de histórias e oferece índice pesquisável de notícias globais.

O Media Cloud aproxima-se deste trabalho na preocupação com coleta estruturada, pesquisa de conteúdo e análise da atenção dedicada a temas. A diferença principal está na granularidade metodológica: o presente sistema registra uma trilha de auditoria específica para cada relatório, incluindo finalidade da consulta, validação semântica do item, relação com a camada factual, classificação e controle de qualidade da narrativa produzida.

### 2.3 Recuperação aumentada por informação externa

Lewis et al. (2020) formalizaram a arquitetura de **Retrieval-Augmented Generation (RAG)** como combinação entre memória paramétrica de modelos de linguagem e memória não paramétrica recuperável. Uma motivação central é permitir que a geração seja condicionada por informação externa, atualizável e passível de proveniência, em vez de depender exclusivamente do conhecimento armazenado nos parâmetros do modelo.

Embora o presente projeto não implemente a arquitetura RAG original de forma estrita, adota princípio semelhante: o modelo de linguagem recebe evidências recuperadas de um corpus persistente e de pesquisas externas. O texto jornalístico permanece armazenado independentemente do modelo, podendo ser reconsultado e reavaliado em novas pesquisas.

### 2.4 Lacuna abordada

As soluções anteriores mostram que coleta massiva, recuperação de notícias e geração apoiada por memória externa são tecnicamente viáveis. Este projeto concentra-se em uma lacuna operacional complementar: **como transformar buscas temáticas em um relatório de repercussão com rastreabilidade de ponta a ponta**.

A unidade central não é apenas a notícia, mas a relação:

```text
tema
  → consulta
  → resultado bruto
  → documento
  → decisão de relevância
  → evidência
  → classificação
  → síntese
```

Essa relação é persistida para permitir inspeção posterior.

---

## 3. Metodologia

### 3.1 Visão geral

A aplicação é implementada em Python 3.12, FastAPI, SQLAlchemy e Pydantic. PostgreSQL é o banco principal, com suporte a SQLite para desenvolvimento. O DuckDuckGo é utilizado como mecanismo externo de busca Web/vídeos e o arXiv pode ser consultado para literatura científica. As tarefas semânticas são executadas por um `ReportAgent`, utilizando um modelo compatível com a API da OpenAI e suporte a fallback local compatível com esse protocolo.

O pipeline operacional pode ser representado por:

```mermaid
flowchart LR
    A[Tema] --> B[Perfil do tema]
    B --> C[Memória histórica]
    C --> D[Planejamento de consultas]
    D --> E[Coleta Web / vídeo]
    E --> F[SearchHit bruto]
    F --> G[Hidratação do artigo]
    G --> H[Validação semântica]
    H --> I[Camada factual opcional]
    I --> J[Classificação]
    J --> K[Detecção de lacunas]
    K --> L[Busca complementar]
    L --> M[Redação]
    M --> N[QA]
    N --> O[Relatório / PDF]
    H --> P[Exemplos supervisionados]
    P --> Q[Reranker local]
    Q --> C
```

### 3.2 Tipificação da pauta

Antes da pesquisa, o tema é transformado em um perfil estruturado. Atualmente são consideradas três categorias principais:

| Tipo | Interpretação | Exemplo |
|---|---|---|
| `INSTITUTIONAL_PRODUCT` | publicação, relatório ou produto institucional | `Dossiê Mulher 2026` |
| `EVENT_TOPIC` | ocorrência ou conjunto de eventos | `policiais mortos em agosto de 2026 no Rio de Janeiro` |
| `GENERAL_TOPIC` | pauta temática sem produto ou evento único | `segurança pública no Rio de Janeiro` |

Essa classificação altera a estratégia de busca. Em produtos institucionais, o sistema preserva o nome do produto como âncora. Em eventos, são construídas variantes semanticamente equivalentes do fato. Em temas gerais, a busca pode utilizar termos e sinônimos extraídos do perfil.

### 3.3 Separação temporal

O método diferencia:

```text
event_start / event_end
    = período em que o fato ocorreu

collection_start / collection_end
    = período em que a repercussão será observada
```

Essa separação impede que uma notícia publicada posteriormente para confirmar um fato seja automaticamente contada como repercussão dentro da janela principal.

Por exemplo:

```text
Tema:
"policiais mortos em agosto de 2026 no Rio de Janeiro"

Janela factual:
2026-08-01 → 2026-08-31

Janela de repercussão:
2026-08-01 → 2026-08-31
```

A camada factual pode consultar material posterior quando necessário para confirmação, sem alterar a janela de contagem da repercussão.

### 3.4 Planejamento das consultas

O planejador procura produzir uma **consulta principal** e um pequeno número de consultas complementares materialmente diferentes. O objetivo é evitar matrizes extensas de paráfrases que aumentam custo e redundância sem ampliar proporcionalmente a cobertura.

Consultas semanticamente muito próximas são eliminadas. O sistema também verifica deterministicamente se a consulta preserva a âncora do objeto monitorado.

#### Exemplo A — produto institucional

Tema:

```text
Dossiê Mulher 2026
```

Estratégia determinística possível:

```text
"Dossiê Mulher 2026"
"Dossiê Mulher"
"Dossiê Mulher" "Instituto de Segurança Pública"
```

Para verificar veículos prioritários, a mesma âncora pode ser combinada com restrição de domínio:

```text
site:g1.globo.com "Dossiê Mulher 2026"
site:oglobo.globo.com "Dossiê Mulher 2026"
site:odia.ig.com.br "Dossiê Mulher 2026"
```

A lista configurada de veículos prioritários inclui G1/Globo, O Globo, Extra, O Dia, CNN Brasil, UOL, R7, Band e Agência Brasil.

#### Exemplo B — evento

Tema:

```text
morte por intervenção de agente do Estado no RJ 2026
```

O perfil determinístico reconhece variantes como:

```text
"morte por intervenção de agente do Estado" "Rio de Janeiro" 2026
"mortes por intervenção de agentes do Estado" "Rio de Janeiro" 2026
"morte decorrente de intervenção policial" "Rio de Janeiro" 2026
"mortes decorrentes de intervenção policial" "Rio de Janeiro" 2026
```

Na camada de descoberta factual podem aparecer formulações adicionais, por exemplo:

```text
"morto em intervenção policial" "Rio de Janeiro" 2026
"morto durante ação policial" "Rio de Janeiro" 2026
```

Essas consultas não possuem necessariamente a mesma finalidade. A primeira família procura **repercussão midiática**; a segunda pode ser usada para **descoberta/validação factual**.

#### Exemplo C — evento nominal

Tema:

```text
policiais mortos em agosto de 2026 no Rio de Janeiro
```

Após a primeira passagem factual, nomes identificados podem originar consultas de acompanhamento. Conceitualmente:

```text
"<nome identificado>" policial Rio de Janeiro
"<nome identificado>" morte agosto 2026
```

A finalidade é registrada como `NOMINAL_FOLLOWUP`, evitando misturar a investigação de indivíduos com a consulta temática inicial.

### 3.5 Finalidade das consultas

Cada consulta possui uma finalidade explícita:

| Finalidade | Uso |
|---|---|
| `MEDIA_REPERCUSSION` | localizar itens que podem integrar a análise de repercussão |
| `FACT_DISCOVERY` | descobrir evidências relacionadas ao fato |
| `OFFICIAL_FACT` | procurar confirmação em fontes institucionais |
| `NOMINAL_FOLLOWUP` | aprofundar pessoas ou eventos identificados na primeira passagem |

Essa distinção é fundamental. Uma fonte encontrada para confirmar um fato não entra automaticamente nas métricas de mídia.

### 3.6 Coleta e preservação do resultado bruto

Toda tentativa de pesquisa gera registros de auditoria. `SearchQuery` representa a consulta planejada e `SearchCall` registra a tentativa efetiva, incluindo provedor, horário, sucesso, erro, latência e número de resultados.

Cada retorno bruto é persistido como `SearchHit` **antes** das decisões semânticas posteriores. Assim, um item irrelevante, duplicado ou fora da janela não desaparece da trilha de auditoria.

Os estados permitem distinguir, por exemplo:

```text
PENDING
ATTEMPTED
SUCCEEDED
NO_RESULTS
FAILED
SKIPPED
```

### 3.7 Consolidação e proveniência

Resultados que representam o mesmo endereço são consolidados em `MediaItem`, preservando a proveniência de suas diferentes descobertas.

A origem da mídia é classificada deterministicamente como:

```text
PORTAL_NOTICIAS
REDE_SOCIAL
YOUTUBE
```

Além da URL, são preservados título, domínio, data de publicação, snippet, corpo textual quando recuperável, fonte, consulta de origem e horário de recuperação.

### 3.8 Hidratação e validação semântica

O snippet retornado pelo mecanismo de busca pode ser insuficiente para decidir se uma matéria realmente pertence ao tema. Por isso, antes da validação semântica, o sistema pode recuperar o conteúdo integral da página.

A decisão de relevância considera o objeto monitorado e o texto disponível. Entre as relações aceitas pelo pipeline estão categorias como cobertura direta do produto/evento e cobertura derivada materialmente relacionada ao tema. Resultados sem relação são mantidos no banco com estado de descarte, em vez de simplesmente removidos.

O ponto metodológico é que:

```text
resultado da busca ≠ item válido de repercussão
```

A busca maximiza recuperação; a validação decide pertencimento ao corpus analítico.

### 3.9 Camada factual

Em pautas que exigem identificação de eventos, vítimas, locais ou circunstâncias, uma camada factual separada estrutura afirmações e suas evidências.

O fluxo pode incluir duas passagens:

```text
coleta inicial
   ↓
extração factual
   ↓
resolução / conflito
   ↓
planejamento nominal
   ↓
nova coleta direcionada
   ↓
segunda extração
   ↓
resolução final
```

Essa separação reduz o risco de inferir que uma matéria pertence à repercussão apenas porque foi útil para confirmar determinado fato.

### 3.10 Memória global do corpus

Uma característica central do sistema é não tratar cada relatório como uma investigação isolada.

Cada `MediaItem` pode ser associado a um `CorpusDocument` global. O documento recebe URL canônica, hash do conteúdo, fingerprint, texto, metadados e, quando necessário, representação vetorial.

Assim:

```text
Projeto A ─┐
Projeto B ─┼──> CorpusDocument
Projeto C ─┘
```

A relação entre projeto e documento permanece separada em `ProjectCorpusLink`. Isso é necessário porque **o texto da matéria é estável, mas sua relevância depende da pergunta de pesquisa**.

Uma matéria considerada válida para o Projeto A pode ser recuperada no Projeto B, porém volta a ser avaliada em relação ao novo tema.

### 3.11 Recuperação semântica

Antes de abrir novas pesquisas externas, projetos anteriores semanticamente próximos são examinados.

O ranking do corpus histórico combina:

```text
similaridade entre projetos
        +
similaridade lexical documento-tema
        +
similaridade por embedding
        +
score do reranker, quando disponível
```

Os embeddings utilizam `text-embedding-3-small` quando o endpoint OpenAI está configurado. Se embeddings externos não estiverem disponíveis, existe fallback vetorial local determinístico. Atualmente os vetores são persistidos em JSON para manter compatibilidade entre SQLite e PostgreSQL.

O corpus reutilizado não elimina automaticamente novas buscas. Ele serve como ponto de partida; o planejador pode reduzir consultas redundantes e posteriormente procurar lacunas de cobertura.

### 3.12 Aprendizado supervisionado incremental

Após a validação, decisões `VALID` e `NOT_RELATED` são convertidas em `RelevanceTrainingExample`.

Cada exemplo preserva, entre outros elementos:

```text
tema/perfil da pesquisa
texto do documento
rótulo de relevância
tipo de relação
evidência da decisão
projeto
documento de origem
```

Esses exemplos alimentam um reranker local leve. O modelo é treinado somente após atingir quantidade mínima de exemplos e representação mínima das duas classes.

O reranker **não possui autoridade para aceitar ou rejeitar definitivamente uma matéria**. Seu papel é priorizar candidatos históricos:

```text
reranker
   ↓
ordenação de candidatos
   ↓
validação semântica auditável
   ↓
decisão final
```

Dessa maneira, o sistema aprende com o uso sem transformar previsões do modelo em verdade não supervisionada.

### 3.13 Cobertura complementar

Após validação e classificação, o sistema verifica lacunas, especialmente em veículos prioritários. Se a cobertura estiver incompleta, uma segunda fase pode gerar consultas adicionais.

O `gap_fill` ocorre **depois** da primeira análise. Portanto, a pergunta deixa de ser “o que mais posso pesquisar?” e passa a ser “qual dimensão relevante ainda não foi coberta?”.

Essa estratégia reduz buscas redundantes.

### 3.14 Geração e controle de qualidade

A redação é produzida apenas após consolidação do corpus, fatos e métricas. O relatório gerado é submetido a QA determinístico e, quando habilitado, QA por LLM.

Achados críticos ou de alta severidade podem provocar nova rodada de redação. A exportação do PDF final exige aprovação; versões não aprovadas permanecem disponíveis apenas como rascunho para revisão.

---

## 4. Resultados

### 4.1 Resultados de engenharia

No estado atual, o protótipo implementa integralmente a cadeia:

```text
planejamento
→ busca
→ persistência bruta
→ consolidação
→ hidratação
→ validação
→ fatos
→ classificação
→ memória
→ redação
→ QA
→ exportação
```

Os principais resultados funcionais são:

- preservação dos resultados brutos e das tentativas de pesquisa;
- distinção entre pesquisa factual e pesquisa de repercussão;
- suporte a portais de notícias, redes sociais e YouTube;
- tratamento independente das janelas factual e midiática;
- recuperação do corpo integral de artigos antes da decisão semântica;
- deduplicação por URL e conteúdo;
- reutilização de corpus histórico entre projetos;
- recuperação semântica por embeddings;
- criação automática de exemplos supervisionados;
- treinamento incremental de reranker local;
- detecção de lacunas de cobertura;
- registro de chamadas e custos de LLM;
- geração versionada de relatório;
- QA antes da exportação final.

### 4.2 Reutilização do conhecimento

A inclusão da memória global modifica o comportamento esperado do sistema em pesquisas recorrentes.

No fluxo sem memória:

```text
tema
→ pesquisa externa completa
→ validação
→ corpus
```

No fluxo atual:

```text
tema
→ recuperação do corpus histórico
→ ranking semântico
→ revalidação
→ identificação de lacunas
→ pesquisa externa complementar
→ corpus atualizado
```

Essa arquitetura é particularmente útil para temas recorrentes. Uma pesquisa sobre uma edição já investigada de um produto institucional, por exemplo, pode reaproveitar matérias históricas e concentrar novas consultas em lacunas ou conteúdo publicado posteriormente.

### 4.3 Auditabilidade

O resultado metodológico mais importante não é apenas a geração automática do relatório, mas a capacidade de reconstruir seu processo.

Para uma matéria analisada, é possível representar a cadeia:

```text
Qual tema originou a investigação?
        ↓
Qual consulta encontrou a URL?
        ↓
Qual ferramenta/provedor executou a busca?
        ↓
Qual foi o retorno bruto?
        ↓
Qual conteúdo foi recuperado?
        ↓
Por que o item foi considerado relacionado?
        ↓
Qual classificação recebeu?
        ↓
Em qual métrica ou trecho do relatório foi utilizado?
```

Essa característica diferencia a proposta de um uso direto de chatbot ou de uma chamada isolada a um modelo de linguagem.

### 4.4 Resultados ainda não estabelecidos

Os resultados acima são **resultados de implementação e comportamento do protótipo**, e não devem ser interpretados como evidência de superioridade estatística.

O repositório ainda não contém um benchmark anotado independente suficiente para reportar, de forma cientificamente defensável:

- precisão, revocação e F1 da seleção de matérias;
- ganho quantitativo do reranker em relação ao ranking lexical;
- redução média de chamadas externas produzida pelo reuso;
- redução de custo de LLM;
- concordância entre avaliadores humanos e o classificador;
- cobertura relativa frente a GDELT, Media Cloud ou buscas manuais;
- taxa de erro factual do relatório final.

Essas métricas constituem a etapa experimental natural para transformar o protótipo em um estudo empírico completo.

---

## 5. Discussão

A arquitetura adota uma posição intermediária entre automação puramente determinística e delegação integral ao modelo de linguagem.

Regras determinísticas são adequadas para propriedades objetivamente verificáveis, como janela temporal, domínio, URL, âncora nominal e deduplicação. Modelos de linguagem são empregados onde a decisão depende de interpretação contextual, como determinar se uma matéria repercute materialmente um tema mesmo sem repetir literalmente seus termos.

A memória de corpus também exige esse equilíbrio. O fato de uma matéria ter sido relevante anteriormente é evidência útil, mas não prova que ela seja relevante para uma nova pergunta. Por isso, o sistema reutiliza **documentos**, mas reavalia **relações**.

O mesmo princípio orienta o aprendizado incremental. Os rótulos produzidos durante a operação podem melhorar a priorização futura, mas o reranker permanece subordinado à validação final. Essa escolha procura evitar ciclos de retroalimentação nos quais erros históricos passem a ser aceitos automaticamente.

Outra limitação decorre da própria Web. Ausência de resultado não equivale a ausência de cobertura: mecanismos de busca possuem índices, rankings e limitações próprios. Da mesma forma, páginas removidas, paywalls, conteúdo dinâmico e metadados incompletos podem afetar a recuperação.

---

## 6. Conclusão

Este trabalho apresentou uma arquitetura auditável para análise automatizada de repercussão midiática. A proposta trata a construção do corpus como problema metodológico independente da geração textual e separa explicitamente fato, evidência factual e cobertura jornalística.

O sistema combina planejamento compacto de consultas, coleta externa, preservação dos resultados brutos, hidratação de documentos, validação semântica, regras determinísticas, extração factual, classificação, memória histórica, recuperação semântica, aprendizado supervisionado incremental e controle de qualidade.

A introdução de `CorpusDocument`, `ProjectCorpusLink` e `RelevanceTrainingExample` permite que pesquisas sucessivas deixem de ser operações independentes. O conhecimento documental é preservado e reutilizado, enquanto decisões dependentes do tema permanecem específicas de cada projeto. Embeddings e reranking tornam essa memória progressivamente mais útil sem remover a etapa final de validação.

Como trabalhos futuros, destacam-se: construção de um corpus de referência anotado por especialistas; avaliação de precisão, revocação e F1; experimentos de ablação para medir a contribuição de cada componente do ranking; migração da busca vetorial para `pgvector` em coleções maiores; avaliação longitudinal de custo e cobertura; e, em cenários com múltiplas instituições e corpora privados, investigação de **aprendizado federado** para compartilhar parâmetros do modelo de relevância sem compartilhar os textos locais.

---

## 7. Reprodutibilidade e execução

### 7.1 Requisitos

- Python 3.12+
- PostgreSQL recomendado; SQLite suportado para desenvolvimento
- chave OpenAI ou endpoint compatível para as tarefas LLM
- acesso à Internet para pesquisas externas

### 7.2 Instalação

```bash
git clone https://github.com/0rakul0/relatorio-midiatico-isp.git
cd relatorio-midiatico-isp

python -m venv .venv
```

Windows:

```powershell
.venv\Scripts\activate
pip install -e .
```

Linux/macOS:

```bash
source .venv/bin/activate
pip install -e .
```

### 7.3 Configuração mínima

Exemplo de `.env`:

```env
DATABASE_URL=postgresql+psycopg://relatorio:relatorio@localhost:5432/repercussao

OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4.1-mini

DUCKDUCKGO_REGION=br-pt
ENABLE_CORPUS_REUSE=true
CORPUS_EMBEDDING_MODEL=text-embedding-3-small
RERANKER_AUTO_TRAIN=true
```

Fallback local compatível com OpenAI:

```env
OPENAI_FALLBACK_BASE_URL=http://localhost:11434/v1
OPENAI_FALLBACK_MODEL=gemma4:12b
OPENAI_FALLBACK_API_KEY=ollama
```

### 7.4 Execução

```bash
uvicorn app.main:app --reload
```

Interface local:

```text
http://127.0.0.1:8000
```

### 7.5 Testes

```bash
pytest
```

---

## 8. Estrutura resumida do software

```text
app/
├── main.py
├── agent.py
├── models.py
├── schemas.py
├── config.py
├── topic_profile.py
├── fact_layer.py
├── report_qa.py
├── pdf_report.py
│
├── orchestration/
│   ├── executor.py
│   └── state.py
│
├── services/
│   ├── pipeline.py
│   ├── search_planning.py
│   ├── corpus_reuse.py
│   ├── relevance_learning.py
│   ├── validation.py
│   ├── classification.py
│   ├── reporting.py
│   └── collection/
│
└── tools/
    ├── search.py
    ├── hydration.py
    ├── academic.py
    └── providers/
```

---

## 9. Referências

BERMEJO, F.; BHARGAVA, R.; BUDNE, P.; et al. **Media Cloud 2.0: An Updated Open Web News Archive**. *Proceedings of the International AAAI Conference on Web and Social Media*, v. 20, n. 1, p. 2735–2746, 2026. DOI: 10.1609/icwsm.v20i1.42778.

LEWIS, P.; PEREZ, E.; PIKTUS, A.; et al. **Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks**. In: *Advances in Neural Information Processing Systems 33 (NeurIPS 2020)*, 2020.

ROBERTS, H.; BHARGAVA, R.; VALIUKAS, L.; et al. **Media Cloud: Massive Open Source Collection of Global News on the Open Web**. *Proceedings of the International AAAI Conference on Web and Social Media*, v. 15, n. 1, p. 1034–1045, 2021. DOI: 10.1609/icwsm.v15i1.18127.

THE GDELT PROJECT. **GDELT 2.0: Events Database, Global Knowledge Graph and Mentions**. Documentação técnica do projeto.

WILLIAMS, S. **Exploration of the Global Database of Events, Language and Tone (GDELT), with specific application to disaster reporting**. Office for National Statistics, 2020.

---

## 10. Citação do software

Enquanto não houver publicação acadêmica associada ao projeto, o software pode ser referenciado como:

```text
Relatório de Repercussão Midiática — ISP.
Sistema auditável para coleta, validação e análise de repercussão midiática.
Versão 0.4.0. 2026.
```

---

## Licença e responsabilidade

Este repositório constitui uma ferramenta de apoio à pesquisa e à análise. Classificações automáticas, inferências factuais e sínteses produzidas por modelos de linguagem devem permanecer sujeitas à inspeção das fontes e à validação humana, especialmente quando utilizadas em contextos institucionais, científicos ou decisórios.
