#!/usr/bin/env bash
#
# Bandcamp Downloader — installer for a Debian/Ubuntu LXC container.
#
#   sudo ./install.sh
#
# Safe to re-run: it keeps the existing password and leaves downloads untouched.

set -euo pipefail

APP_DIR="/opt/bandcamp-downloader"
SERVICE="bandcamp-dl"
SERVICE_USER="bandcamp"
SERVICE_PORT="${SERVICE_PORT:-8000}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "error: run this as root (sudo ./install.sh)" >&2
  exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip ffmpeg >/dev/null

echo "==> Creating service user"
if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

echo "==> Copying application to ${APP_DIR}"
if [[ "${SOURCE_DIR}" != "${APP_DIR}" ]]; then
  mkdir -p "${APP_DIR}"
  for item in app.py templates static assets requirements.txt tailwind.config.js README.md; do
    if [[ -e "${SOURCE_DIR}/${item}" ]]; then
      cp -a "${SOURCE_DIR}/${item}" "${APP_DIR}/"
    fi
  done
fi
mkdir -p "${APP_DIR}/downloads" "${APP_DIR}/data"

echo "==> Building virtual environment"
if [[ ! -x "${APP_DIR}/venv/bin/python" ]]; then
  python3 -m venv "${APP_DIR}/venv"
fi
"${APP_DIR}/venv/bin/pip" install --quiet --upgrade pip
"${APP_DIR}/venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"

# Keep the password across re-runs.
EXISTING_PASSWORD=""
if [[ -f "/etc/systemd/system/${SERVICE}.service" ]]; then
  EXISTING_PASSWORD="$(grep -oP 'APP_PASSWORD=\K[^"]+' "/etc/systemd/system/${SERVICE}.service" || true)"
fi

if [[ -n "${EXISTING_PASSWORD}" ]]; then
  APP_PASSWORD="${EXISTING_PASSWORD}"
  echo "==> Reusing the existing password"
else
  APP_PASSWORD="$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-20)"
  echo "==> Generated a new password"
fi

echo "==> Writing systemd unit"
cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=Bandcamp Downloader
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
Environment="DOWNLOAD_ROOT=${APP_DIR}/downloads"
Environment="STATE_DIR=${APP_DIR}/data"
Environment="APP_USERNAME=bandcamp"
Environment="APP_PASSWORD=${APP_PASSWORD}"
Environment="MAX_CONCURRENT_DOWNLOADS=2"
Environment="PYTHONUNBUFFERED=1"
Environment="PYTHONDONTWRITEBYTECODE=1"
ExecStart=${APP_DIR}/venv/bin/uvicorn app:app --host 0.0.0.0 --port ${SERVICE_PORT} --workers 1
Restart=on-failure
RestartSec=5

# Sandboxing: the app may only write to its downloads and data directories.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
LockPersonality=true
ReadWritePaths=${APP_DIR}/downloads ${APP_DIR}/data

[Install]
WantedBy=multi-user.target
EOF

chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"
chmod 750 "${APP_DIR}/downloads"

echo "==> Starting service"
systemctl daemon-reload
systemctl enable --quiet "${SERVICE}"
systemctl restart "${SERVICE}"
sleep 2

if systemctl is-active --quiet "${SERVICE}"; then
  IP_ADDR="$(hostname -I | awk '{print $1}')"
  cat <<EOF

  Bandcamp Downloader is running.

    URL       http://${IP_ADDR}:${SERVICE_PORT}
    Username  bandcamp
    Password  ${APP_PASSWORD}

  Files land in ${APP_DIR}/downloads
  Logs:     journalctl -u ${SERVICE} -f
  Restart:  systemctl restart ${SERVICE}

EOF
else
  echo "error: service failed to start" >&2
  journalctl -u "${SERVICE}" -n 30 --no-pager >&2
  exit 1
fi
