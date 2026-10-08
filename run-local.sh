#!/bin/bash
# Run alx_fancontrol FROM THE SOURCE TREE without installing (no /opt
# snapshot, no systemd) — the interim mode.
#
#   bash run-local.sh tui                 # NO sudo: edits your home config,
#                                         # read-only on the fans
#   sudo bash run-local.sh daemon [...]   # root: drives the fans per your
#                                         # home config (Ctrl-C restores)
#
# Both are pinned to the SAME config (~/.config/alx_fancontrol/config.json)
# via ALX_FANCONTROL_CONFIG, because the euid-based defaults would otherwise
# split them (root -> /etc/alx_fancontrol, non-root -> ~/.config).
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
CMD="${1:-}"
if [ -z "$CMD" ]; then
    echo "usage: bash run-local.sh {tui} | sudo bash run-local.sh daemon [daemon args...]"
    exit 2
fi
shift

# the invoking user's home (root's HOME is /root under sudo)
USER_NAME="${SUDO_USER:-$(id -un)}"
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"
CFG="$USER_HOME/.config/alx_fancontrol/config.json"

if [ ! -x "$SRC/.venv/bin/python" ]; then
    echo "dev venv missing — run: bash $SRC/scripts/bootstrap_venv.sh" >&2
    exit 1
fi

# if we run as root, the TUI/daemon would root-own files in the user's
# .config; hand ownership back on exit.
restore_owner() {
    if [ -n "${SUDO_USER:-}" ]; then
        chown "$USER_NAME" "$CFG" 2>/dev/null || true
        [ -f "$CFG.bak" ] && chown "$USER_NAME" "$CFG.bak" 2>/dev/null || true
    fi
}
trap restore_owner EXIT

case "$CMD" in
    tui)
        if [ "$(id -u)" -eq 0 ]; then
            echo "note: running the TUI as root is fine here (config is pinned"
            echo "      to $USER_HOME); the daemon needs root, the TUI doesn't."
        fi
        echo "config: $CFG"
        exec env ALX_FANCONTROL_CONFIG="$CFG" "$SRC/.venv/bin/alx-fancontrol" tui "$@"
        ;;
    daemon)
        if [ "$(id -u)" -ne 0 ]; then
            echo "note: running as $USER_NAME — the daemon can read the config"
            echo "      but cannot write the pwm registers (takeover will fail"
            echo "      with EACCES). Use 'sudo bash run-local.sh daemon' to"
            echo "      actually drive the fans."
        fi
        echo "config: $CFG   (Ctrl-C returns driven fans to firmware control)"
        exec env ALX_FANCONTROL_CONFIG="$CFG" "$SRC/.venv/bin/python" \
            -m alx_fancontrol.daemon "$@"
        ;;
    *)
        echo "unknown command: $CMD (use 'tui' or 'daemon')" >&2
        exit 2
        ;;
esac
