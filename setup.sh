#!/usr/bin/env bash
set -euo pipefail

# ─── Colors ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="trading-bot"
VENV_DIR="$SCRIPT_DIR/venv"
SERVICE_FILE="$SCRIPT_DIR/${SERVICE_NAME}.service"
NGINX_CONF="/etc/nginx/sites-available/${SERVICE_NAME}"

echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BOLD}   Trading Bot — Setup Script               ${NC}"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# ─── 1. System dependencies ───────────────────────────────────────────────────
info "Installing system dependencies..."
if ! command -v python3 &>/dev/null; then
    error "python3 not found. Install it first: sudo apt install python3"
fi
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip nginx
success "System dependencies installed."

# ─── 2. Virtualenv ────────────────────────────────────────────────────────────
info "Creating virtual environment at $VENV_DIR..."
if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR"
    success "Virtualenv created."
else
    warn "Virtualenv already exists — skipping."
fi

# ─── 3. Python dependencies ───────────────────────────────────────────────────
info "Installing Python requirements..."
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" -q
success "Python dependencies installed."

# ─── 4. Directory structure ───────────────────────────────────────────────────
info "Creating runtime directories..."
mkdir -p "$SCRIPT_DIR/logs"
success "Directories ready."

# ─── 5. Environment file ──────────────────────────────────────────────────────
if [[ ! -f "$SCRIPT_DIR/.env" ]]; then
    info "Copying .env.example to .env..."
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    warn ".env created from example. Fill in your API keys before running the bot."
else
    warn ".env already exists — skipping."
fi

# ─── 6. Systemd service ───────────────────────────────────────────────────────
if [[ ! -f "$SERVICE_FILE" ]]; then
    error "Service file not found: $SERVICE_FILE"
fi

info "Installing systemd service..."
# Patch WorkingDirectory with the actual path
sed "s|{{WORKING_DIR}}|$SCRIPT_DIR|g; s|{{VENV_PYTHON}}|$VENV_DIR/bin/python|g" \
    "$SERVICE_FILE" | sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}.service"
success "Systemd service installed and enabled."

# ─── 7. Nginx reverse proxy for Streamlit ─────────────────────────────────────
info "Configuring Nginx reverse proxy for Streamlit (port 8501)..."
sudo tee "$NGINX_CONF" > /dev/null <<'NGINX'
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass         http://127.0.0.1:8501;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 86400;
    }
}
NGINX

sudo ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/
sudo nginx -t -q && sudo systemctl reload nginx
success "Nginx configured."

# ─── Final instructions ───────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}${BOLD}   Setup complete!${NC}"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  ${BOLD}1. Edit your credentials:${NC}"
echo -e "     ${CYAN}nano $SCRIPT_DIR/.env${NC}"
echo ""
echo -e "  ${BOLD}2. Run in dry-run mode (no real orders):${NC}"
echo -e "     ${CYAN}source $VENV_DIR/bin/activate && python $SCRIPT_DIR/main.py --dry-run${NC}"
echo ""
echo -e "  ${BOLD}3. Start as system service:${NC}"
echo -e "     ${CYAN}sudo systemctl start ${SERVICE_NAME}${NC}"
echo -e "     ${CYAN}sudo journalctl -u ${SERVICE_NAME} -f${NC}"
echo ""
echo -e "  ${BOLD}4. Launch dashboard:${NC}"
echo -e "     ${CYAN}source $VENV_DIR/bin/activate && streamlit run $SCRIPT_DIR/dashboard/app.py${NC}"
echo -e "     Then open http://$(hostname -I | awk '{print $1}') in your browser."
echo ""
