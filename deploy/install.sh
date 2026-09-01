#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run this installer with sudo." >&2
    exit 1
fi

SOURCE_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
INSTALL_ROOT=/opt/yah-deleted-onedrive
SERVICE_USER=yahdeleted

apt-get install -y python3-venv rclone

if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
    useradd --system --user-group \
        --home-dir /var/lib/yah-deleted-onedrive \
        --create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

install -d -o root -g root -m 0755 "${INSTALL_ROOT}"
if [[ "${SOURCE_ROOT}" != "${INSTALL_ROOT}/src" ]]; then
    echo "Clone this repository at ${INSTALL_ROOT}/src before running the installer." >&2
    exit 1
fi

if [[ ! -x "${INSTALL_ROOT}/venv/bin/python" ]]; then
    python3 -m venv "${INSTALL_ROOT}/venv"
fi
"${INSTALL_ROOT}/venv/bin/python" -m pip install \
    --requirement "${SOURCE_ROOT}/requirements.txt"

install -d -o root -g "${SERVICE_USER}" -m 0750 /etc/yah-deleted-onedrive
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 \
    /var/lib/yah-deleted-onedrive/tmp

if [[ ! -e /etc/yah-deleted-onedrive/rclone.conf ]]; then
    install -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0600 \
        /dev/null /etc/yah-deleted-onedrive/rclone.conf
fi

install -o root -g root -m 0644 \
    "${SOURCE_ROOT}/deploy/yah-deleted-onedrive.service" \
    /etc/systemd/system/yah-deleted-onedrive.service
systemctl daemon-reload

echo "Installation complete. Run:"
echo "sudo ${INSTALL_ROOT}/venv/bin/python ${INSTALL_ROOT}/src/onboard.py"
