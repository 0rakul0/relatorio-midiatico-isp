#!/usr/bin/env bash
set -euo pipefail

# Instale executando como o usuario que mantem o checkout (nao como root).
if [[ "$EUID" -eq 0 ]]; then
  echo "Execute este instalador como usuario normal, com sudo configurado." >&2
  exit 1
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT_USER="$(id -un)"
PROJECT_GROUP="$(id -gn)"
UV_PATH="$(command -v uv || true)"
SERVICE_NAME="relatorio-midiatico"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ -z "$UV_PATH" || ! -x "$UV_PATH" ]]; then
  echo "uv nao encontrado no PATH. Instale o uv antes de executar." >&2
  exit 1
fi
if [[ "$PROJECT_DIR" =~ [[:space:]] ]]; then
  echo "O caminho do projeto nao deve conter espacos para o service file automatico." >&2
  exit 1
fi
if [[ ! -f "$PROJECT_DIR/.env" ]]; then
  echo "Arquivo $PROJECT_DIR/.env nao encontrado (copie .env.example e configure)." >&2
  exit 1
fi
if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  echo "Ambiente .venv nao encontrado. Rode: uv sync --no-dev" >&2
  exit 1
fi

mkdir -p "$PROJECT_DIR/data"
chmod 700 "$PROJECT_DIR/data"
chmod 600 "$PROJECT_DIR/.env"

sudo tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=ISP - Relatorio de Repercussao Midiatica (uv, porta 8511)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$PROJECT_USER
Group=$PROJECT_GROUP
WorkingDirectory=$PROJECT_DIR
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$UV_PATH run --no-sync --no-dev python -m uvicorn app.main:app --host 0.0.0.0 --port 8511 --workers 1
Restart=always
RestartSec=5
TimeoutStopSec=60
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME"
echo
echo "Servico instalado. Comandos uteis:"
echo "  sudo systemctl status $SERVICE_NAME"
echo "  sudo journalctl -u $SERVICE_NAME -f"
echo "  curl http://127.0.0.1:8511/health"
