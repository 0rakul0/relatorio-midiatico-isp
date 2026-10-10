from __future__ import annotations

TASK_PROMPTS: dict[str, str] = {
    "article_hydrator": """
Voce esta executando a tarefa HIDRATACAO DE ARTIGOS PARA VALIDACAO.
O payload contem uma lista fechada de URLs ja coletadas e deduplicadas.
Se a lista nao estiver vazia, chame UMA vez a ferramenta hidratar_artigos com a
lista COMPLETA e exata. Nao pesquise novas URLs, nao altere enderecos e nao crie
consultas. Depois da ferramenta, retorne status e um detalhe curto.
""",
    "academic_research": """
Voce esta executando a tarefa LITERATURA CIENTIFICA.

Seu objetivo e encontrar artigos que ajudem a contextualizar cientificamente o
tema do relatorio. Literatura academica e uma camada de CONTEXTO e NAO deve ser
contada como repercussao midiatica nem como confirmacao automatica de noticias.

Regras:
- examine o tema e o perfil antes de pesquisar;
- para GENERAL_TOPIC, a pesquisa cientifica e obrigatoria: chame
  pesquisar_literatura_cientifica uma vez e nao retorne searched=false;
- para outros tipos, pesquise sempre que estudos contribuirem ao contexto;
- chame pesquisar_literatura_cientifica UMA vez com ate tres consultas
  conceituais complementares, incluindo portugues e ingles quando pertinente;
- para temas brasileiros, priorize termos e sinonimos nacionais, sem exigir
  citacao ao ISP nem restringir a janela jornalistica;
- escolha os trabalhos pela relacao demonstravel com o tema e evidencias;
- nao use exclusivamente sintaxe arXiv; a ferramenta inclui SciELO,
  OpenAlex, Crossref, Semantic Scholar e arXiv;
- selecione no maximo 10 artigos realmente relacionados;
- relevance_score mede aderencia ao tema, nao qualidade cientifica;
- relation_to_topic deve explicar concretamente, em portugues do Brasil, como o
  artigo ajuda a interpretar a pauta;
- para cada artigo selecionado, preserve title EXATAMENTE como veio da ferramenta;
- gere title_ptbr como traducao fiel para portugues do Brasil;
- gere abstract_ptbr como traducao fiel do abstract retornado pela ferramenta;
  se o artigo nao tiver abstract, retorne null;
- original_language deve usar um codigo curto quando for identificavel (por
  exemplo en, pt, es) ou null quando houver duvida;
- se titulo/abstract ja estiverem em portugues, preserve o sentido e o texto,
  sem parafrasear desnecessariamente;
- na traducao, NAO traduza nomes de autores, siglas consagradas, nomes de modelos,
  equacoes, numeros, unidades, DOI, IDs, URLs, nomes de datasets ou referencias;
- nao resuma o abstract: traduza o conteudo, mantendo qualificadores, incertezas,
  negacoes e limitacoes do original;
- nunca invente DOI, autores, titulo, URL ou artigo fora do retorno da ferramenta;
- trate arXiv como repositorio de e-prints/preprints; nao presuma revisao por pares.
""",
}
