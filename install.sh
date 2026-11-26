#!/bin/sh
# Procular installer.
#   ./install.sh              install for the current user (~/.local)
#   ./install.sh --uninstall  remove the user installation
#   sudo ./install.sh --helper   install the root-owned privileged helper + polkit policy
#                                (password is then remembered for a few minutes: auth_admin_keep)
#   sudo ./install.sh --remove-helper
set -e
SRC=$(dirname "$(readlink -f "$0")")
APP_ID=io.github.ouned.Procular
DATA=${XDG_DATA_HOME:-$HOME/.local/share}
BIN=$HOME/.local/bin

case "$1" in
  --helper)
    [ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
    if [ -x /usr/libexec/procular-helper ]; then  # its policy would be overwritten
      echo "The procular .deb/.rpm package is installed and already provides the helper."
      exit 0
    fi
    install -D -o root -g root -m 0755 "$SRC/procular/helper/procular_helper.py" /usr/local/libexec/procular-helper
    install -D -o root -g root -m 0644 "$SRC/data/$APP_ID.policy" "/usr/share/polkit-1/actions/$APP_ID.policy"
    echo "Installed /usr/local/libexec/procular-helper and polkit policy."
    ;;
  --remove-helper)
    [ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
    rm -f /usr/local/libexec/procular-helper
    # leave the policy alone if it belongs to the .deb/.rpm package
    POLICY=/usr/share/polkit-1/actions/$APP_ID.policy
    if grep -qs /usr/local/libexec/procular-helper "$POLICY"; then rm -f "$POLICY"; fi
    echo "Removed privileged helper."
    ;;
  --uninstall)
    rm -rf "$DATA/procular" "$BIN/procular" "$DATA/applications/$APP_ID.desktop" \
           "$DATA/icons/hicolor/scalable/apps/$APP_ID.svg" \
           "$DATA/icons/hicolor/16x16/apps/$APP_ID.svg" "$DATA/icons/hicolor/32x32/apps/$APP_ID.svg"
    echo "Uninstalled."
    ;;
  ""|--user)
    python3 -c 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1"); import psutil' || {
      echo "Missing dependencies: install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-psutil" >&2; exit 1; }
    rm -rf "$DATA/procular"
    mkdir -p "$DATA/procular" "$BIN" "$DATA/applications" "$DATA/icons/hicolor/scalable/apps"
    cp -r "$SRC/procular" "$DATA/procular/"
    find "$DATA/procular" -name __pycache__ -type d -prune -exec rm -rf {} +
    # The copy has no .git: freeze the version from the source tree's git tags.
    python3 "$SRC/procular/_version.py" > "$DATA/procular/procular/_version.txt"
    cat > "$BIN/procular" <<LAUNCH
#!/bin/sh
export PYTHONPATH="$DATA/procular\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m procular "\$@"
LAUNCH
    chmod +x "$BIN/procular"
    sed "s|^Exec=.*|Exec=$BIN/procular|" "$SRC/data/$APP_ID.desktop" > "$DATA/applications/$APP_ID.desktop"
    cp "$SRC/data/$APP_ID.svg" "$DATA/icons/hicolor/scalable/apps/"
    for size in 16 32; do   # hand-tuned small sizes
      install -D -m 0644 "$SRC/data/$APP_ID-$size.svg" "$DATA/icons/hicolor/${size}x$size/apps/$APP_ID.svg"
    done
    gtk4-update-icon-cache -q -t "$DATA/icons/hicolor" 2>/dev/null || true
    update-desktop-database -q "$DATA/applications" 2>/dev/null || true
    echo "Installed. Launch 'Procular' from the GNOME app grid or run $BIN/procular"
    echo "Optional: sudo $0 --helper   (remember admin authentication for a few minutes)"
    ;;
  *) echo "unknown option $1" >&2; exit 2 ;;
esac
