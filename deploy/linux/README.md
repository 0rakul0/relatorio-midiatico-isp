# Instalacao nativa no Linux (uv + systemd)

Esta instalacao usa exclusivamente SQLite local em `data/repercussao.db`.
Nao ha suporte a banco remoto ou compatibilidade com URL PostgreSQL.
URLs de banco externo sao recusadas antes de qualquer conexao de rede.

## 1. Dependencias e checkout

Requer Linux com `systemd`, Git, acesso a Internet e `uv`.

```bash
# Instalacao oficial do uv (caso ainda nao esteja instalado):
curl -LsSf https://astral.sh/uv/install.sh | sh
# Abra uma nova sessao do shell se necessario para atualizar o PATH.
git clone https://github.com/0rakul0/relatorio-midiatico-isp.git
cd relatorio-midiatico-isp
uv python install 3.13
uv sync --no-dev
```

O `uv.lock` contem somente as dependencias ativas do projeto. O servico
usa `uv run --no-sync` para nao instalar pacotes durante o boot.

## 2. Credenciais e banco

```bash
mkdir -p data
cp .env.example .env
openssl rand -hex 32
nano .env
chmod 600 .env
```

Edite `LOCAL_SESSION_SECRET` com o valor aleatorio gerado. Para Linux
nativo recomenda-se:

```dotenv
DATABASE_URL=sqlite+pysqlite:///./data/repercussao.db
APP_ENV=production
LOCAL_AUTH_BYPASS=false
LOCAL_SESSION_SECRET=COLOQUE_AQUI_O_VALOR_GERADO
OPENAI_ONLY=true
OPENAI_API_KEY=sk-SUA_CHAVE_REAL_AQUI
OPENAI_MODEL=gpt-4.1-mini
CORPUS_EMBEDDING_PROVIDER=openai
CORPUS_EMBEDDING_FALLBACK_LOCAL=false
```

O servidor Linux usa somente a API oficial da OpenAI para tarefas LLM.
Nao configure `OPENAI_BASE_URL`, `OPENAI_FALLBACK_BASE_URL`,
`OPENAI_FALLBACK_MODEL` ou `OPENAI_FALLBACK_API_KEY` nesse servidor.
Mesmo que sobrem variaveis antigas de fallback no ambiente,
`OPENAI_ONLY=true` desabilita o uso desse fallback.
O inicio em producao e bloqueado se a chave OpenAI estiver ausente.
O DuckDuckGo continua sendo o buscador das noticias; ele nao e uma LLM.

**Criar e inspecionar o SQLite local:**

```bash
uv run --no-sync python -m app.init_db
ls -lh data/repercussao.db
```

O arquivo e criado com a tabela `app_users` (id, email, password_hash,
is_admin, plan, created_at), alem das tabelas do corpus. O comando nao
remove registros existentes, e pode ser repetido. Nao ha senha padrao.
Em Linux a pasta `data` e restrita ao usuario do servico e o banco
recebe permissao `0600`.

### 2.1 Ajuste dos orcamentos de coleta

O `.env.example` tem limites separados para busca jornalistica,
busca oficial, pesquisa factual, videos, Apify e analise LLM. O
inventario mensal de operacoes requer:

```dotenv
MAX_MEDIA_QUERIES=24
MAX_OFFICIAL_QUERIES=60
ENABLE_ANNUAL_EVENT_INVENTORY=true
MAX_ANNUAL_EVENT_INVENTORY_QUERIES=12
ENABLE_OFFICIAL_ANNUAL_INVENTORY=true
MAX_OFFICIAL_INVENTORY_QUERIES=60
MAX_PRIORITY_RESULTS_PER_QUERY=8
```

Com `APIFY_API_TOKEN` vazio, deixe `APIFY_SOCIAL_ENABLED=false`.
A descoberta publica de posts pelo DuckDuckGo continua independente
com `SOCIAL_DISCOVERY_ENABLED=true`. O parametro valido para limitar
as consultas por rede e `SOCIAL_DISCOVERY_QUERIES_PER_PLATFORM`
(nao `MAX_SOCIAL_DISCOVERY_QUERIES`).

**O `.env` existente nao e alterado pelo `git pull`**. Compare-o
com `.env.example` e atualize manualmente; nao execute `cp`
sobre suas credenciais ja configuradas. Para aplicar variaveis novas,
reinicie: `sudo systemctl restart relatorio-midiatico`.

### 2.2 Memoria editorial entre relatorios

A aplicacao recupera ate tres versoes **QA aprovadas** de relatorios
anteriores sobre o mesmo tema e a mesma edicao, no mesmo usuario.
A memoria distingue leituras anteriores das fontes e fatos validados agora.

```dotenv
REPORT_HISTORY_ENABLED=true
REPORT_HISTORY_MAX_VERSIONS=3
REPORT_HISTORY_LLM_ENABLED=true
```

Quando houver historico, o planejador recebe os riscos metodologicos
anteriores para orientar a busca por evidencias ausentes, sem tratar
conclusoes antigas como fatos. A redacao gera uma leitura comparativa
separada das secoes factuais, salva o snapshot na versao do relatorio
e exibe a comparacao na web e no PDF.

A comparacao mostra documentos em comum, exclusivos de cada amostra,
continuidade e mudancas de enquadramentos, com referencia ao numero
da versao anterior. Janelas diferentes nao permitem estimar variacao
percentual ou tendencia de cobertura. Versoes rejeitadas pelo QA
nao sao usadas como linha de base.

Para economizar na OpenAI, use
`REPORT_HISTORY_LLM_ENABLED=false`, que mantem os indicadores
documentais comparativos, sem sintetizar as narrativas pela LLM.
A primeira execucao sem historico aprovado permanece inalterada.

## 3. Criar o administrador do banco local

```bash
uv run --no-sync python -m app.local_users create --email seu-email@exemplo.br --admin
```

A senha sera solicitada interativamente (minimo 12 caracteres), nao
fica no historico do shell e e armazenada apenas como hash. O mesmo
comando pode redefinir a senha, preservando o ID e os projetos do usuario.

### 3.1 Diagnostico do login

Se o navegador exibir `Autenticacao do servidor nao configurada`, o
servidor nao recebeu uma `LOCAL_SESSION_SECRET` valida. **Isso nao indica
senha incorreta.** A chave serve para assinar as sessoes e nao e a senha
da conta no SQLite.

```bash
cd ~/relatorio-midiatico-isp
openssl rand -hex 32
nano .env
```

Copie os 64 caracteres gerados para `LOCAL_SESSION_SECRET` no `.env`.
Nao compartilhe a chave em tickets, prints ou chats; nao sobrescreva
outras variaveis. Mantenha `APP_ENV=production` e
`LOCAL_AUTH_BYPASS=false`. Se a chave antiga ja estava correta, preserve-a:
alterar a chave invalida sessoes anteriores.

```bash
uv run --no-sync python -m app.check_auth --email seu-email@exemplo.br
sudo systemctl restart relatorio-midiatico
sudo systemctl status relatorio-midiatico
```

O diagnostico verifica a configuracao, a existencia da conta e o hash
da senha **sem revelar chaves, senhas ou tokens**. Se o usuario nao
existir no SQLite configurado, crie-o com `app.local_users create`.
Se o usuario existir mas a senha estiver incorreta, o mesmo comando
redefine a senha, preservando a conta e os projetos vinculados.

A inicializacao agora falha explicitamente se o secret nao estiver
configurado, em vez de disponibilizar a tela com o login quebrado.

## 4. Testar no terminal

```bash
uv run --no-sync python -m uvicorn app.main:app --host 0.0.0.0 --port 8511 --workers 1
```

Em outro terminal:

```bash
curl -fsS http://127.0.0.1:8511/health
```

Pare o teste com Ctrl+C antes de instalar o servico, para evitar conflito
de porta.

## 5. Instalar no systemd

```bash
bash deploy/linux/install-service.sh
sudo systemctl status relatorio-midiatico
sudo journalctl -u relatorio-midiatico -f
```

O instalador inicializa e confere o SQLite com `app.init_db`,
detecta o usuario, caminho absoluto do checkout e binario
`uv` ativos. Instala o servico em
`/etc/systemd/system/relatorio-midiatico.service`, habilita o boot e
usa `--workers 1` para preservar o estado de execucao/cancelamento.

```bash
sudo systemctl start relatorio-midiatico
sudo systemctl stop relatorio-midiatico
sudo systemctl restart relatorio-midiatico
sudo systemctl enable relatorio-midiatico
sudo systemctl disable relatorio-midiatico
```

**Se o inicializador recusar DATABASE_URL:** abra `.env` e substitua
qualquer URL remota por `sqlite+pysqlite:///./data/repercussao.db`.
Variaveis exportadas com `export DATABASE_URL=...` prevalecem sobre o .env:
execute `unset DATABASE_URL` na sessao de instalacao, e retire essa
exportacao de arquivos de inicializacao do shell. O arquivo `.env.local`
nao e mais utilizado e pode ser removido com seguranca apos conferir
que nao guarda configuracoes necessarias. Nunca publique a URL antiga,
pois ela pode conter senhas.

Acesse `http://IP-DO-SERVIDOR:8511` dentro da rede autorizada.
Nao exponha login, token e corpus por HTTP aberto na Internet:
para acesso remoto use HTTPS via proxy reverso ou VPN e limite
as origens no firewall.

## 6. Atualizar o codigo

```bash
sudo systemctl stop relatorio-midiatico
git pull --ff-only origin main
uv sync --no-dev
sudo systemctl start relatorio-midiatico
curl -fsS http://127.0.0.1:8511/health
```

A inicializacao executa migracoes aditivas. Sempre faca backup do
`data/repercussao.db` antes de upgrades importantes. Use backup
consistente do SQLite, especialmente se houver escrita concorrente.

## Observacoes importantes

- Banco externo nao e suportado; as tabelas sao inicializadas em SQLite local.
- As contas locais sao criadas com `app.local_users`; contas remotas antigas
  nao sao importadas automaticamente.
- A tela de login nao oferece cadastro publico: novas contas sao
  provisionadas exclusivamente pelo terminal do servidor.
- `LOCAL_AUTH_BYPASS=true` nao funciona em `APP_ENV=production`.
- O app depende de Internet para coleta DuckDuckGo e outros provedores
  opcionais, independentemente do banco usado.
