FROM python:3.13-slim

# Binário do uv (pinado na mesma versão usada para gerar o uv.lock)
COPY --from=ghcr.io/astral-sh/uv:0.5.22 /uv /uvx /bin/

WORKDIR /app

# venv fora de /app: o compose monta .:/app por cima, o que esconderia
# um .venv dentro de /app. Com PATH ajustado, o `uvicorn` do `command:`
# continua funcionando.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Dependências primeiro (layer em cache; rebuild só se pyproject/uv.lock mudar).
# README.md acompanha porque o pyproject o declara como readme do pacote.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Código da aplicação
COPY app ./app
RUN uv sync --frozen --no-dev

EXPOSE 8000
# O domínio público do Railway está encaminhado para a porta 8000.
# Mantê-la explícita evita divergência quando a plataforma injeta PORT.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
