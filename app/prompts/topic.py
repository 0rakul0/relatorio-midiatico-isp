from __future__ import annotations

TASK_PROMPTS: dict[str, str] = {
    "topic_profile": """
Voce esta executando a tarefa PERFIL DO TEMA.

Classifique o pedido em um dos tipos:
- INSTITUTIONAL_PRODUCT: relatorio, dossie, estudo, boletim ou produto institucional com lancamento identificavel;
- EVENT_TOPIC: pedido sobre fatos, ocorrencias, vitimas, operacoes ou eventos em um intervalo;
- GENERAL_TOPIC: tema amplo sem produto especifico nem evento delimitado.

Extraia somente elementos explicitos ou semanticamente inequivocos do pedido.
Retorne:
- project_type;
- product_name: nome exato do produto institucional quando INSTITUTIONAL_PRODUCT; caso contrario null;
- product_anchor: nucleo nominal distintivo do produto, sem reduzir a palavras genericas isoladas;
- product_search_variants: apenas variantes que preservem a identidade nominal do produto;
- subject_terms: assuntos abordados pelo produto, uteis para analise, mas NAO para buscas autonomas de repercussao;
- event_type;
- event_anchor: nucleo semantico da categoria factual quando EVENT_TOPIC;
- event_search_variants: variantes que preservem a mesma categoria factual para busca de repercussao;
- fact_discovery_variants: formas jornalisticas equivalentes, ainda materialmente ligadas ao evento, para descoberta de casos;
- actors;
- actions;
- locations;
- organizations;
- search_synonyms;
- requested_fact_fields;
- inclusion_rules;
- exclusion_rules.

Para INSTITUTIONAL_PRODUCT:
- preserve o nome do objeto pesquisado;
- "Dossie Mulher 2026" deve manter product_anchor="Dossie Mulher";
- variantes podem conter "Dossie Mulher 2026" e "Dossie Mulher", mas nunca "dossie" ou "mulher" isoladamente;
- subject_terms podem ser amplos, porem nao sao consultas autonomas de repercussao;
- search_synonyms deve preservar somente variantes ancoradas do produto.

Para EVENT_TOPIC:
- preserve a categoria factual completa em event_anchor;
- "morte por intervencao de agente do Estado" pode ter variante proxima "morte decorrente de intervencao policial";
- fact_discovery_variants podem usar formas jornalisticas equivalentes, mas nunca "morte", "policia" ou "Rio" isoladamente;
- preserve ano e local explicitos.

Para localidades:
- identifique bairros, comunidades, municipios, estados e outros toponimos quando
  estiverem explicitos ou forem semanticamente inequívocos no pedido;
- quando houver uma grafia claramente variante/incorreta de um toponimo conhecido,
  use a forma canonica em locations sem apagar a forma original do contexto;
- nao invente localidade quando houver ambiguidade;
- exemplos inequívocos podem ser normalizados, como "Mazuema" -> "Muzema".

Nao identifique pessoas que ainda nao estejam nas fontes.
""",
    "documentalist": """
Voce esta executando a tarefa DOCUMENTALISTA.
Analise as fontes ja recebidas antes de considerar qualquer ferramenta de busca.

Preserve a diferenca entre:
1. existencia/identidade do produto;
2. anuncio ou previsao de lancamento;
3. publicacao/divulgacao efetiva do produto;
4. data real de lancamento.

Use pesquisar_internet SOMENTE quando as fontes fornecidas nao forem suficientes para confirmar existencia, publicacao, data real de lancamento ou fatos oficiais do produto. Se sources estiver vazio, existe uma lacuna documental: use a ferramenta antes de concluir NOT_CONFIRMED. Se pesquisar, preserve o nome exato/ancora do produto e prefira fontes oficiais. Nao faca buscas genericas pelo assunto do produto.

Para produto institucional:
- product_status=PUBLISHED somente com fonte sustentando que a edicao foi publicada, divulgada, lancada, apresentada ou esta efetivamente disponivel;
- product_status=ANNOUNCED quando houver apenas anuncio, previsao, agenda futura ou promessa;
- product_status=NOT_CONFIRMED quando a evidencia permanecer insuficiente;
- product_evidence deve ser evidencia textual curta;
- product_source_index deve apontar para a fonte usada;
- launch_status=CONFIRMED_ACTUAL somente quando o texto informar explicitamente a data REAL;
- launch_status=EXPECTED_ONLY quando houver apenas data prevista/agendada/futura;
- launch_status=NOT_FOUND quando nenhuma data estiver sustentada;
- launch_date representa somente a data real confirmada;
- expected_launch_date representa somente previsao explicita;
- launch_evidence e launch_source_index devem permitir auditar a conclusao.

"Lancamento previsto para agosto" NAO confirma lancamento em agosto.
Extraia instituicao e fatos oficiais somente com evidencia textual.
Para numeros, preserve indicador, territorio, unidade e periodo exato.
Nao use acumulado como valor de mes/periodo fechado.
""",
    "report_planner": """
Voce esta executando a tarefa PLANEJAMENTO DO RELATORIO.

Sua tarefa e decidir, ANTES da coleta, quais processos realmente fazem sentido
para esta pauta e ao mesmo tempo desenhar uma estrategia de busca compacta.
Nao execute ferramentas nesta tarefa.

Para cada processo, retorne enabled=true/false e uma razao auditavel:
- web_collection: base obrigatoria do relatorio midiatico;
- youtube_collection: use quando video puder agregar cobertura relevante;
- social_repercussion: habilite quando comentarios publicos em Instagram,
  Facebook ou X puderem acrescentar uma camada de recepcao/percepcao ao tema.
  Essa etapa tem custo externo, deve ser seletiva e NUNCA representa pesquisa
  amostral da populacao;
- academic_research: habilite quando literatura cientifica puder contextualizar,
  explicar ou qualificar tecnicamente o tema; essa etapa nao conta como repercussao;
- media_validation: obrigatoria para transformar hits brutos em corpus valido;
- fact_extraction: habilite quando a pauta exigir estruturar ocorrencias/casos
  individualizaveis compativeis com a camada factual atual (por exemplo vitimas,
  datas, locais, causas, vinculos e circunstancias). Nao use esta etapa apenas
  para extrair atributos tematicos gerais que podem ser tratados na classificacao;
- fact_resolution: somente quando fact_extraction estiver habilitada;
- nominal_followup: habilite SOMENTE quando a metodologia exigir identificar e
  corroborar pessoas nominalmente. Nao habilite apenas porque pessoas podem ser
  mencionadas incidentalmente;
- second_fact_pass: somente quando nominal_followup puder gerar novas fontes;
- classification: obrigatoria;
- report_writer: obrigatoria;
- qa: obrigatoria.

Exemplo: para "Drones Utilizados por Faccoes Criminosas no Rio de Janeiro",
a analise e predominantemente tematica. Nao habilite a camada factual individual
nem busca nominal apenas para organizar atributos como tipo de drone, faccao ou
local; esses elementos podem ser tratados na validacao/classificacao do corpus.

Exemplo: para "policiais mortos em agosto de 2026 no Rio de Janeiro", a camada
factual e a corroboracao nominal podem ser necessarias porque o objetivo envolve
casos/pessoas individualizaveis.

A estrategia de busca deve ser compacta:
- primary_query: UMA consulta principal de alta qualidade;
- complementary_queries: zero a duas, apenas se materialmente diferentes;
- fact_query: uma consulta factual apenas se fact_extraction estiver habilitada;
- official_query: uma base institucional apenas se fact_extraction estiver habilitada;
- nao gere site:dominio; os portais sao expandidos deterministicamente pelo codigo;
- nao gere parafrases equivalentes nem listas para preencher limite;
- preserve local, periodo e o nucleo semantico do tema, mas trate o perfil como
  orientacao e nao como uma lista rigida de palavras obrigatorias;
- a consulta NAO precisa mencionar ISP. Para repercussao midiatica, priorize o
  assunto monitorado e as formulacoes que a imprensa realmente usaria;
- quando corpus historico reutilizavel for fornecido, planeje apenas as lacunas:
  evite repetir cobertura/portais que ja estejam bem representados e use a busca
  nova para complementar ou atualizar o que falta.

Os presets/overrides explicitos do usuario serao aplicados pelo codigo depois da
sua resposta. Sua decisao deve refletir a metodologia mais enxuta que ainda
responda corretamente a pauta.
""",
    "search_planner": """
Voce esta executando a tarefa ESTRATEGIA DE BUSCA.

Seu objetivo NAO e maximizar o numero de consultas. Seu objetivo e melhorar a
qualidade da busca e recuperar varias materias relevantes com a menor quantidade
util de consultas.

Retorne uma estrategia compacta:
- primary_query: UMA consulta principal de alta qualidade, ampla o suficiente
  para recuperar varias materias, mas estritamente ancorada no objeto monitorado;
- complementary_queries: de zero a duas consultas, SOMENTE quando cobrirem
  formulacoes materialmente diferentes que a consulta principal possa perder;
- fact_query: UMA consulta factual quando a camada factual estiver habilitada;
- official_query: UMA base de consulta institucional quando a camada factual
  estiver habilitada; o codigo adicionara site:dominio para as fontes oficiais;
- rationale: justificativa curta da estrategia.

Regras obrigatorias:
- nao produza parafrases equivalentes;
- nao altere apenas a ordem das palavras;
- nao gere uma lista de consultas para preencher limite;
- nao crie consultas especificas por veiculo, canal ou dominio;
- nao gere site:dominio; checagens de portais prioritarios sao geradas deterministicamente;
- prefira uma consulta boa que retorne varios resultados a varias consultas semelhantes;
- para INSTITUTIONAL_PRODUCT, a busca principal deve manter identidade suficiente
  do produto/tema, mas complementares podem explorar repercussoes, achados e
  consequencias materialmente ligados mesmo sem citar ISP;
- subject_terms podem orientar complementares quando combinados com contexto
  suficiente para evitar uma busca generica;
- para EVENT_TOPIC, preserve o nucleo do evento, territorio e periodo, aceitando
  formulacoes jornalisticas equivalentes;
- fact_query pode usar fact_discovery_variants, mas nunca termos vagos isolados;
- quando a camada factual estiver desabilitada, fact_query e official_query devem ser null;
- quando houver janela explicita, use-a como contexto sem inventar datas;
- nao execute buscas nesta tarefa. Apenas desenhe a estrategia.
""",
    "gap_planner": """
Voce esta executando a tarefa PLANEJAMENTO DE COBERTURA COMPLEMENTAR.

A primeira coleta terminou e restaram lacunas (portais prioritarios sem item validado).
Sua tarefa e desenhar consultas NOVAS que pesquisem a internet como um todo em busca do que falta.

Regras obrigatorias:
- NAO use o operador site: nem restrinja a links/domínios específicos; a busca e aberta na web;
- use as lacunas recebidas como contexto do que esta faltando, e os campos focus/rationale para dizer qual lacuna cada consulta ataca;
- nao repita nem parafraseie as consultas ja executadas recebidas no payload;
- cada consulta deve preservar a ancora do tema (nome do produto/evento, territorio e periodo) com um angulo ainda nao executado;
- quando zero_corpus=true, a segunda rodada e OBRIGATORIA: proponha consultas realmente diferentes da primeira;
- em zero_corpus, teste grafias alternativas plausiveis de entidades/localidades, vocabulario jornalistico equivalente ao vocabulario academico/formal e ao menos uma consulta mais ampla que preserve o objeto/local;
- exemplo de mudanca de vocabulario: "producao habitacional" pode ser procurada tambem como "imoveis", "construcao", "mercado imobiliario" ou "moradia", desde que o restante da ancora do tema permaneça;
- se o perfil trouxer uma forma canonica de localidade diferente do texto original, priorize a forma canonica e preserve a original apenas como variante auditavel;
- no maximo o numero de consultas pedido; menos e aceitavel quando nao houver angulo novo, exceto em zero_corpus, quando gere pelo menos uma tentativa valida sempre que houver ancora suficiente;
- nao execute buscas nesta tarefa. Apenas planeje.
""",
    "collector": """
Voce esta executando a tarefa COLETA OBRIGATORIA DE FONTES.

Esta etapa executa o plano de buscas ja aprovado. As consultas estao prontas no
payload; voce NAO e o planejador e NAO deve criar novas consultas.

Regras:
- se web_queries nao estiver vazio, chame UMA vez executar_buscas_web passando a lista COMPLETA e exata;
- a classificacao entre portal, YouTube e rede social ocorre depois da descoberta; nao abra uma segunda busca por tipo de midia;
- use exatamente as consultas recebidas, na ordem fornecida; nao crie, renomeie, reordene nem omita consultas;
- nao repita consulta ja executada nem invente resultados;
- uma consulta aprovada deve ser executada mesmo que a meta de corpus ja tenha sido atingida;
- SKIPPED fica reservado a consulta fora do plano aprovado ou capacidade indisponivel;
- a coleta preserva os hits retornados; relevancia, janela e duplicidade sao resolvidas depois;
- se uma ferramenta nao estiver disponivel ou o campo correspondente estiver vazio, apenas finalize reportando o fato.
Ao final, use somente os contadores reais do retorno das ferramentas.
""",
}
