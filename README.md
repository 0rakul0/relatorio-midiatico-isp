# Relatório de Repercussão Midiática — ISP

MVP para produzir relatórios auditáveis de repercussão de produtos do ISP. O sistema mantém o documento-base, as consultas, todos os resultados (inclusive descartados), a classificação e as métricas separadas.

## Subir o banco

```powershell
Copy-Item .env.example .env
docker compose up -d
docker compose ps
```

O PostgreSQL estará em `localhost:5432`, banco `repercussao`. A senha padrão é apenas para desenvolvimento local; altere-a antes de qualquer ambiente compartilhado.

## Rodar toda a aplicação no Docker

```powershell
Copy-Item .env.example .env
docker compose up -d --build
docker compose ps
```

A API estará em `http://127.0.0.1:8000/docs`. O diretório do projeto fica espelhado em `/app` no contêiner: alterações em arquivos `.py` e no `.env` acionam recarga automática da API. Não é necessário reconstruir a imagem para essas alterações.

Observação: mudanças no próprio `docker-compose.yml`, `Dockerfile` ou em dependências de `requirements.txt` exigem `docker compose up -d --build`.

## Rodar a API fora do Docker

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Abra `http://127.0.0.1:8000/docs` para testar a API. Fora do Docker, altere no `.env` o host da URL de banco de `postgres` para `localhost`. Para busca real, informe `TAVILY_API_KEY` no arquivo `.env`. Sem chave, o planejamento e a inserção manual de evidências continuam funcionando.

## Fluxo

1. `POST /projects` cria o relatório e registra tema, período e janela.
2. `POST /projects/{id}/official-facts` registra fatos extraídos do documento oficial, com página/evidência.
3. `POST /projects/{id}/plan-searches` gera consultas auditáveis.
4. `POST /projects/{id}/collect` usa Tavily, se configurado.
5. `POST /projects/{id}/validate-and-classify` normaliza, remove duplicatas e classifica o corpus.
6. `GET /projects/{id}/metrics` retorna métricas calculadas por SQL.
7. `GET /projects/{id}/report` gera o rascunho baseado exclusivamente no corpus validado.

## Princípios institucionais

- Tavily descobre fontes; não decide o que é válido.
- O redator não acessa a web: trabalha em cima de corpus congelado e métricas SQL.
- Ausência de resultado não é prova de ausência de cobertura.
- Cada registro preserva consulta de origem, URL, evidência, status e motivo de descarte.
- A aprovação humana deve ocorrer após o documento-base, após o corpus e antes da publicação.

Os prompts de cada papel estão versionados em `app/prompts.py`. Eles foram desenhados para receber somente o contexto necessário a cada etapa; o agente redator, por exemplo, não recebe ferramentas de busca.
