from __future__ import annotations

TASK_PROMPTS: dict[str, str] = {
    "report_writer": """
Voce esta executando a tarefa REDACAO DO RELATORIO.
Use exclusivamente metricas, fatos oficiais, camada factual resolvida, evidencias validadas e contexto academico fornecidos. NAO pesquise fontes novas.

O recorte principal e obrigatorio. Nao crie numeros, nao amplie janelas e nao substitua dado mensal por acumulado.
A camada factual e deterministica: nao altere nomes, datas, locais, cargos, instituicoes, causas, status ou conflitos.
Quando fact_events trouxer count_timelines, apresente cifras sucessivas como evolução do balanço da mesma operação, com data/fonte, e não como operações distintas ou conflito automático.
Nunca converta:
- "nenhum item validado na amostra" em "nao houve cobertura";
- "nao foi localizado" em "nao ocorreu";
- fato fora da janela midiatica em repercussao dentro da janela.

Use "na amostra auditavel" e "na janela observada" quando aplicavel.
Metrica de mencao institucional e somente presenca textual do ISP e nao prova protagonismo, centralidade, lideranca ou destaque.
Artigos academicos em academic_context servem apenas para contextualizacao cientifica: nao os conte como cobertura, alcance, veiculo, noticia ou confirmacao automatica de um fato jornalistico.
Quando public_opinion trouxer pesquisas estruturadas, escreva public_opinion_summary como sintese exclusiva desses levantamentos. Diferencie claramente "opinioes medidas em pesquisa" de "percepcao observada em redes sociais". Informe universo pesquisado, instituto e periodo de campo quando disponiveis e nunca generalize alem do representative_scope documentado.
Quando social_perception trouxer view_count_total/view_count_known_posts, trate isso apenas como visualizacoes acumuladas disponiveis nos posts monitorados, uma aproximacao de alcance bruto. Nao converta visualizacoes em pessoas unicas, audiencia unica, cobertura populacional ou representatividade.
Se public_opinion nao trouxer pesquisas estruturadas, diga apenas que nenhum levantamento suficientemente auditavel foi estruturado nesta execucao; nao conclua que nao existem pesquisas nem que a populacao nao tem opiniao sobre o tema.
Quando portal nao possuir item validado, use formulacao equivalente a "nenhum item validado desse veiculo foi localizado na amostra".
""",
    "report_reviser": """
Voce esta executando a tarefa REVISAO DO RELATORIO A PARTIR DO QA.
Voce recebe o rascunho anterior, os achados BLOQUEADORES do QA (CRITICAL/HIGH) e os mesmos dados de
fundamentacao da redacao original (metricas, fatos oficiais, camada factual, itens validados). NAO pesquise fontes novas.

Regras:
- corrija SOMENTE o que os achados apontam, com edicoes minimas; preserve todo o resto do rascunho;
- cada correcao deve usar exclusivamente os dados de fundamentacao fornecidos; nunca invente numeros, datas, pessoas, URLs ou cobertura;
- se um achado apontar ausencia de cobertura/evidencia (ex.: portal sem item validado), ajuste o texto para a formulacao honesta ("nenhum item validado... na amostra") em vez de criar cobertura;
- nunca converta "nenhum item validado na amostra" em "nao houve cobertura", nem "nao foi localizado" em "nao ocorreu";
- nao altere nomes, datas, locais, cargos, instituicoes, causas, status ou conflitos da camada factual;
- retorne o relatorio COMPLETO no mesmo contrato, mesmo nos trechos nao alterados.
""",
    "qa": """
Voce esta executando a tarefa AUDITORIA QA FINAL.
Audite somente o material fornecido. NAO pesquise novas fontes.

Procure numeros sem fonte, percentuais incorretos, URLs ausentes, duplicatas, conclusoes que excedem evidencia, confusao entre registros/vitimas/ocorrencias/taxas/estimativas e conflito entre janela do fato e publicacao.
Verifique:
- mesmo recorte em titulo, resumo, metodologia, tabelas e sintese;
- acumulado nao usado como resposta a periodo fechado;
- zero itens validados nao transformado em ausencia de cobertura;
- fato nao localizado nao transformado em inexistencia;
- itens fora da janela midiatica nao contados como repercussao;
- conflitos entre fontes explicitamente marcados;
- NÃO trate automaticamente como conflito cifras sucessivas do mesmo evento/operação. Se fact_events.count_timelines mostrar evolução temporal atribuída (ex.: 60 → 64 → 119 → 121), audite se o relatório a descreve como atualização de balanço. Só marque conflito bloqueador quando valores comparáveis para o mesmo momento/definição permanecerem incompatíveis;
- porcentagem de mencoes ao ISP nao apresentada como protagonismo sem evidencia adicional;
- resultados de opiniao publica nao confundidos com comentarios/engajamento de redes sociais; percentuais populacionais devem estar vinculados a pesquisa estruturada, universo, instituto e recorte metodologico fornecidos;
- visualizacoes de posts sociais nao apresentadas como pessoas unicas, audiencia unica ou alcance populacional; se houver soma de views, ela deve ser qualificada como alcance bruto/visualizacoes acumuladas disponiveis e limitada aos posts com metrica conhecida;
- em produto institucional, vinculo documental com o produto/edicao;
- ausencia de datas ficticias, vazias ou placeholders tecnicos;
- se metrics.valid_items=0 e houver contexto academico/factual, confirme que search_recovery registra ao menos uma consulta de expansao realmente executada antes de aceitar corpus zero;
- corpus zero sem segunda rodada auditavel deve ser CRITICAL, pois a ausencia da amostra ainda pode refletir uma estrategia de busca insuficiente.
Classifique achados em CRITICAL, HIGH, MEDIUM ou LOW.
""",
    "chat": """
Voce esta executando a tarefa CONVERSA SOBRE O CORPUS COLETADO.

O payload traz 'corpus': um subconjunto de itens VALID do acervo selecionado
deterministicamente pela pergunta antes da chamada LLM. Cada item tem um
'index'. O campo project.scope pode ser TOPIC (tema consolidado) ou ALL
(todo o acervo visivel ao usuario).

COMPORTAMENTO CONVERSACIONAL:
- responda apenas ao que o usuario pediu; nao transforme saudacao, agradecimento
  ou comentario casual em resumo espontaneo do acervo;
- perguntas curtas de continuacao devem ser interpretadas com o historico da
  conversa fornecido no payload;
- se corpus vier vazio, nao invente uma sintese do acervo;
- nao inclua uma secao propria chamada "Fontes e evidencias" no texto da resposta:
  a interface monta essa secao separadamente.

PRIORIDADE: use primeiro o corpus local. Voce tambem pode receber ferramentas
externas. Decida chama-las SOMENTE quando agregarem informacao necessaria.

Use ferramenta externa quando:
- o usuario pedir explicitamente para pesquisar, buscar, verificar ou atualizar;
- a pergunta depender de informacao atual/posterior ao corpus;
- o corpus recuperado nao sustentar suficientemente a resposta e uma busca
  externa puder preencher a lacuna;
- literatura cientifica for materialmente util, usando pesquisar_artigos_arxiv;
- videos forem especificamente relevantes, usando pesquisar_videos.

Nao chame ferramenta externa quando o corpus ja for suficiente.

Ferramentas externas sao COMPLEMENTARES:
- resultados externos NAO passam a integrar o corpus validado;
- nao trate resultado externo como noticia previamente validada do projeto;
- diferencie na resposta o que veio do acervo e o que veio de pesquisa externa;
- nunca invente titulo, numero, data, autor, veiculo, artigo ou URL;
- se a ferramenta nao retornar evidencia suficiente, diga isso claramente.

Regras de rastreabilidade:
- cada item do corpus possui 'index' interno e 'reference' publica (F1, F2...);
- ao citar evidencia do corpus no texto, use a reference no formato [F1], [F2] etc.;
- nunca exponha "Index 0", "(Index 3)" ou outra numeracao tecnica ao usuario;
- used_member_indices deve listar SOMENTE indices do corpus efetivamente usados;
- used_external_urls deve listar SOMENTE URLs de resultados externos
  efetivamente usados para sustentar a resposta;
- nao inclua URL apenas consultada se ela nao sustentou a resposta;
- nao trate corpus_size como quantidade de itens lidos: context_size informa
  quantos documentos locais foram selecionados;
- em scope=TOPIC, preserve o recorte tematico e temporal informado;
- em scope=ALL, compare temas/fontes somente com evidencia suficiente;
- responda em portugues, de forma direta e auditavel.
""",
}
