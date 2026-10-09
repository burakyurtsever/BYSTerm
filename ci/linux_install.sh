#!/usr/bin/env bash
# BYSTerm'i kullanici menusune ekler (root gerekmez):  ./install.sh
# Kaldirmak icin:  ./install.sh --uninstall
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"; APPS="$HOME/.local/share/applications"; ICONS="$HOME/.local/share/icons"
if [ "${1:-}" = "--uninstall" ]; then
  rm -f "$BIN/BYSTerm" "$APPS/bysterm.desktop" "$ICONS/bysterm.png"; echo "Kaldirildi."; exit 0
fi
mkdir -p "$BIN" "$APPS" "$ICONS"
install -m 755 "$HERE/BYSTerm" "$BIN/BYSTerm"
install -m 644 "$HERE/bysterm.png" "$ICONS/bysterm.png"
cat > "$APPS/bysterm.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=BYSTerm
Comment=Seri / TCP / UDP test ve izleme
Exec=$BIN/BYSTerm
Icon=$ICONS/bysterm.png
Terminal=false
Categories=Development;Utility;
DESK
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
echo "Kuruldu: uygulama menusunde 'BYSTerm' (veya terminalde: $BIN/BYSTerm)"
if ! id -nG "$USER" | grep -qw dialout; then
  echo
  echo "Seri portlara erisim icin kullanicinizin 'dialout' grubunda olmasi gerekir:"
  echo "    sudo usermod -aG dialout $USER     (sonra oturumu kapatip acin)"
fi
