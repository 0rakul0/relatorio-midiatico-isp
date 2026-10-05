from __future__ import annotations

TASK_PROMPTS: dict[str, str] = {
    "fact_extraction": """
Voce esta executando a tarefa EXTRACAO FACTUAL AUDITAVEL.
Analise SOMENTE titulo, resumo e conteudo recebidos. NAO pesquise fora da fonte.

Regras:
- diferencie data do fato de data de publicacao;
- data de publicacao nao prova data do fato;
- preserve condicao profissional explicitamente informada;
- suspeita, hipotese, investigacao ou versao de parte nao vira fato confirmado;
- nao complete informacao ausente;
- preserve ambiguidades;
- para operações policiais, extraia operation_name quando a fonte nomear a operação;
- extraia death_count, arrest_count, weapon_count e rifle_count quando explicitamente informados;
- cifras de balanço podem ser atualizações sucessivas da MESMA operação; não conclua conflito só porque o número mudou;
- cada valor nao nulo deve ter evidencia textual curta da propria fonte;
- basis=EXPLICIT para valor escrito;
- basis=RELATIVE_TO_PUBLICATION somente para expressao relativa inequivoca;
- basis=NOT_PRESENT e value=null quando ausente.

Retorne somente eventos sustentados pelo texto recebido.
""",
    "media_relevance": """
Voce esta executando a tarefa TRIAGEM DE ADERENCIA TEMATICA.
Avalie somente o conteudo recebido. NAO pesquise a web.

O objetivo principal e medir repercussao midiatica SOBRE O TEMA e entender o que
a midia diz sobre o assunto. A mencao ao ISP e um atributo analitico posterior,
NAO uma condicao obrigatoria para uma noticia ser relevante.

Decida se cada item tem relacao material com o objeto monitorado considerando
objeto, evento, dados, atores, consequencias, debates e repercussoes derivadas.
- nao valide por uma palavra isolada, territorio ou categoria ampla;
- DIRECT_PRODUCT: mencao direta ao produto/edicao solicitada;
- ATTRIBUTED_FINDING: noticia usa dado/conclusao atribuido ao produto ou instituicao;
- DERIVED_COVERAGE: noticia repercute materialmente achado, debate ou consequencia
  do produto/tema mesmo sem citar o ISP como protagonista;
- DIRECT_EVENT: noticia cobre diretamente o evento/fato monitorado;
- THEMATIC_CONTEXT: noticia trata materialmente do mesmo assunto/contexto e e util
  para entender o ambiente midiatico, mas sem vinculo suficiente para ser cobertura
  direta do produto/evento;
- THEMATIC_ONLY fica reservado a semelhanca superficial ou generica;
- UNRELATED para conteudo sem relacao material;
- evidence deve apontar a conexao concreta com o tema;
- related=true para DIRECT_PRODUCT, ATTRIBUTED_FINDING, DERIVED_COVERAGE,
  DIRECT_EVENT e THEMATIC_CONTEXT quando houver relacao material sustentada.
""",
    "classification": """
Voce esta executando a tarefa ANALISE E CLASSIFICACAO DE REPERCUSSAO.
Trabalhe somente com itens validados e fatos oficiais/resolvidos fornecidos. NAO pesquise fora do corpus.

Para cada item, classifique tema, enquadramento, tom em relacao ao ISP quando
houver mencao e possiveis distorcoes. A ausencia de ISP nao reduz a relevancia
tematica do item; registre isp_mentioned=false e analise o que a midia diz sobre
o assunto. Separe fato oficial, fato resolvido, interpretacao jornalistica e inferencia.
Nao use dado acumulado para caracterizar periodo fechado diferente.
Toda conclusao deve apontar evidencia textual; sem evidencia, responda INVERIFICÁVEL.
""",
}
