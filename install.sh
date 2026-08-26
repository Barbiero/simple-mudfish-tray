#!/usr/bin/env bash
# Sets up a local virtualenv and installs a .desktop launcher for the current
# user. Requires no privileged access — everything happens under $HOME.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtualenv at $VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi

echo "Installing dependencies"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" -q

ICON="$(compgen -G "/opt/mudfish/*/share/mudrun_logo.png" | sort -V | tail -n1 || true)"
ICON="${ICON:-network-vpn}"

APPS_DIR="$HOME/.local/share/applications"
mkdir -p "$APPS_DIR"
DESKTOP_FILE="$APPS_DIR/mudfish-tray.desktop"

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Mudfish Tray
Comment=System tray icon for Mudfish VPN
Exec=$VENV_DIR/bin/python $SCRIPT_DIR/tray.py
Icon=$ICON
Terminal=false
Categories=Network;
X-GNOME-Autostart-enabled=true
EOF

chmod +x "$DESKTOP_FILE"

echo "Installed $DESKTOP_FILE"
echo "Launch \"Mudfish Tray\" from your application menu, or add it to your"
echo "desktop environment's autostart/startup applications to run it on login."
