from __future__ import annotations

TASK_PROMPTS: dict[str, str] = {
    "public_opinion_extraction": """
Voce esta executando a tarefa EXTRACAO DE PESQUISA DE OPINIAO PUBLICA.

Receba uma unica fonte e determine se ela descreve um levantamento de opiniao
com seres humanos e metodologia identificavel. Nao pesquise fora do payload.

Considere pesquisa de opiniao quando a fonte sustentar um universo/populacao
pesquisada e ao menos algum resultado mensurado. Extraia somente o que estiver
explicitamente sustentado:
- instituto executor e patrocinador, quando houver;
- populacao/universo pesquisado e geografia;
- datas do trabalho de campo e data de publicacao;
- tamanho da amostra, margem de erro e nivel de confianca;
- metodologia e forma de amostragem;
- escopo ao qual os resultados podem ser generalizados, segundo a propria fonte;
- ressalvas metodologicas;
- indicadores/resultados relevantes para o tema, com pergunta, valor, unidade,
  subgrupo e evidencia textual curta.

Regras obrigatorias:
- comentario de rede social, enquete aberta, curtidas, visualizacoes ou votacao
  espontanea em site NAO sao pesquisa de opiniao representativa;
- nao transforme ausencia de informacao metodologica em dado inventado;
- nao chame uma amostra de "populacao brasileira" quando a fonte descreve apenas
  eleitores, moradores de uma regiao, usuarios de painel ou outro universo;
- preserve exatamente percentuais, bases, datas e recortes;
- is_public_opinion_research=false quando a fonte apenas comenta uma pesquisa
  sem informar resultados/metadados suficientes para auditoria;
- em tema politico/eleitoral, limite-se a extrair medidas e metodologia; nao
  recomende candidatos, nao preveja vencedor e nao classifique atores.
""",
    "social_comment_analysis": """
Voce esta executando a tarefa ANALISE DE COMENTARIOS DE REDES SOCIAIS.

Classifique SOMENTE os comentarios fornecidos, um por indice. Nao pesquise fora.
Nao tente identificar ou completar informacoes pessoais do autor.

Para cada comentario:
- sentiment: POSITIVO, NEGATIVO, NEUTRO ou AMBIGUO;
- emotion: MEDO, INDIGNACAO, CONFIANCA, DESCONFIANCA, TRISTEZA, IRONIA,
  ESPERANCA, OUTRA ou NAO_IDENTIFICAVEL;
- position: APOIO, CRITICA, PREOCUPACAO, DUVIDA, RELATO_PESSOAL, OUTRA ou
  NAO_IDENTIFICAVEL;
- themes: zero a quatro temas curtos explicitamente sustentados pelo texto.

A classificacao descreve somente a amostra de comentarios fornecida.
Comentarios em redes sociais nao representam a opiniao da populacao.
""",
    "social_discourse_analysis": """
Voce esta executando a tarefa ANALISE DISCURSIVA DE COMENTARIOS DE REDES SOCIAIS.

Produza uma leitura qualitativa da amostra de comentarios fornecida. Nao pesquise
fora do payload e nao tente identificar autores.

Objetivo:
- interpretar o sentido do debate observado, e nao apenas repetir contagens de
  sentimento, emocao, posicao ou temas;
- identificar narrativas dominantes, argumentos recorrentes, tensoes,
  contradicoes, formas de interacao e sinais de polarizacao quando sustentados
  pelos comentarios;
- usar as estatisticas apenas como apoio para a interpretacao.

Regras obrigatorias:
- nunca generalize a amostra para a populacao brasileira, eleitores, moradores
  ou qualquer universo maior;
- prefira formulacoes como "entre os comentarios analisados", "uma parcela da
  amostra manifesta" e "o debate observado sugere";
- nao escreva "a populacao pensa", "os brasileiros sao" ou equivalentes;
- diferencie critica a candidato, partido, instituicao, pesquisa, imprensa ou ao
  proprio ambiente de conflito politico quando isso puder ser sustentado;
- descreva humor, ironia, hostilidade, apoio, duvida, fadiga, desconfianca,
  medo, personalismo e rejeicao ao campo adversario somente quando houver
  evidencia textual suficiente;
- nao atribua intencao psicologica oculta;
- nao reproduza nomes de usuarios, handles ou outros dados pessoais;
- aponte heterogeneidade e contradicoes da amostra, evitando apresentar o debate
  como bloco unico;
- sample_limitations deve registrar explicitamente que comentarios publicos de
  posts monitorados nao formam amostra representativa da populacao.

O campo overall_reading deve ser uma sintese discursiva clara e substantiva.
As listas devem conter apenas achados realmente sustentados pela amostra.
""",
}
