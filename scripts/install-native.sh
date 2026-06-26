#!/usr/bin/env bash
# Install native (non-Docker) systemd units for residential entry.
# Xray:  sudo bash scripts/install-native.sh
# Hy2:   sudo bash scripts/install-native.sh --hysteria
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SYSTEMD_SRC="${REPO_ROOT}/scripts/systemd"
XRAY_UNIT="/etc/systemd/system/denko-xray.service"
NGINX_UNIT="/etc/systemd/system/denko-nginx.service"
HYSTERIA_UNIT="/etc/systemd/system/denko-hysteria.service"
GEO_DIR="/usr/share/xray"
GEOIP_URL="https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat"
GEOSITE_URL="https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat"

INSTALL_HYSTERIA=false
for arg in "$@"; do
  if [[ "$arg" == "--hysteria" ]]; then
    INSTALL_HYSTERIA=true
  fi
done

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Run as root: sudo bash scripts/install-native.sh" >&2
  exit 1
fi

if [[ "$INSTALL_HYSTERIA" == true ]]; then
  if [[ ! -f "${REPO_ROOT}/hysteria/config.yaml" ]]; then
    echo "Missing ${REPO_ROOT}/hysteria/config.yaml — run setup.py with --stack hysteria --native first." >&2
    exit 1
  fi
else
  NEEDS_NGINX=true
  if [[ -f "${REPO_ROOT}/secrets/client.env" ]]; then
    if grep -q '^TRANSPORT=grpc' "${REPO_ROOT}/secrets/client.env" 2>/dev/null; then
      NEEDS_NGINX=false
    fi
  fi
  if [[ "$NEEDS_NGINX" == true && ! -f "${REPO_ROOT}/nginx/nginx.native.conf" ]]; then
    echo "Missing ${REPO_ROOT}/nginx/nginx.native.conf — run setup.py with --native first." >&2
    exit 1
  fi
fi

REPO_USER="$(stat -c '%U' "${REPO_ROOT}")"
REPO_GROUP="$(stat -c '%G' "${REPO_ROOT}")"

install_geodata() {
  if [[ "$INSTALL_HYSTERIA" == true ]]; then
    return
  fi
  mkdir -p "${GEO_DIR}"
  if [[ ! -f "${GEO_DIR}/geoip.dat" ]]; then
    echo "Downloading geoip.dat to ${GEO_DIR}..."
    curl -fsSL "${GEOIP_URL}" -o "${GEO_DIR}/geoip.dat"
  fi
  if [[ ! -f "${GEO_DIR}/geosite.dat" ]]; then
    echo "Downloading geosite.dat to ${GEO_DIR}..."
    curl -fsSL "${GEOSITE_URL}" -o "${GEO_DIR}/geosite.dat"
  fi
  chmod 644 "${GEO_DIR}/geoip.dat" "${GEO_DIR}/geosite.dat" 2>/dev/null || true
}

install_geodata

install_unit() {
  local src_name="$1"
  local dst="$2"
  sed -e "s|REPO_ROOT|${REPO_ROOT}|g" \
      -e "s|REPO_USER|${REPO_USER}|g" \
      -e "s|REPO_GROUP|${REPO_GROUP}|g" \
      "${SYSTEMD_SRC}/${src_name}" >"$dst"
  chmod 644 "$dst"
  echo "Installed $dst (User=${REPO_USER})"
}

if [[ "$INSTALL_HYSTERIA" == true ]]; then
  if ! command -v hysteria >/dev/null 2>&1; then
    echo "Warning: 'hysteria' not in PATH. Arch: install from GitHub releases or AUR." >&2
  fi
  install_unit hysteria.service "$HYSTERIA_UNIT"
  systemctl daemon-reload
  echo ""
  echo "Enable and start (forward TCP+UDP 443 on router):"
  echo "  systemctl enable --now denko-hysteria"
  echo "  ss -ulnp | grep ':443'"
else
  for pkg in xray curl; do
    if ! command -v "$pkg" >/dev/null 2>&1; then
      echo "Warning: '$pkg' not found. On Arch: pacman -S xray-bin curl" >&2
    fi
  done
  if [[ "${NEEDS_NGINX:-true}" == true ]]; then
    if ! command -v nginx >/dev/null 2>&1; then
      echo "Warning: 'nginx' not found. On Arch: pacman -S nginx" >&2
    fi
    install_unit nginx.service "$NGINX_UNIT"
  fi
  install_unit xray.service "$XRAY_UNIT"
  systemctl daemon-reload
  echo ""
  if [[ "${NEEDS_NGINX:-true}" == true ]]; then
    echo "Enable and start:"
    echo "  systemctl enable --now denko-nginx denko-xray"
    echo "  ss -tlnp | grep -E ':443|:8443'"
  else
    echo "Enable and start (gRPC provider — Xray only):"
    echo "  systemctl enable --now denko-xray"
    echo "  ss -tlnp | grep 6437"
  fi
fi

echo ""
echo "Status: systemctl status denko-*"
