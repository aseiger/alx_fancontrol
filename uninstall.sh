#!/bin/bash
# Uninstall the alx_fancontrol system service.
#
#   sudo bash uninstall.sh              # stop+disable service, remove unit
#                                       # and the /opt/alx_fancontrol runtime
#   sudo bash uninstall.sh --purge      # also remove /etc/alx_fancontrol
#                                       # (your assignments/curves!) and
#                                       # /var/log/alx_fancontrol
#   sudo bash uninstall.sh --dry-run    # print what would happen
#
# Never touches the source tree (this directory) — that is your code.
# When the service stops, the daemon gets SIGTERM and returns every fan
# it was driving to firmware (BIOS) control — nothing is left stranded.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
SVC="alx_fancontrol"
UNIT_DST="/etc/systemd/system/$SVC.service"
OPT="/opt/$SVC"
CONFIG_DIR="/etc/$SVC"
LOG_DIR="/var/log/$SVC"
LAUNCHER="/usr/local/bin/alx-fancontrol"
RUN_DIR="/run/$SVC"
DRY=0
PURGE=0

for a in "$@"; do
    case "$a" in
        --purge)  PURGE=1 ;;
        --dry-run) DRY=1 ;;
        *) echo "usage: $0 [--purge] [--dry-run]"; exit 2 ;;
    esac
done

if [ "$DRY" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: run with sudo (or use --dry-run)" >&2
    exit 1
fi

say() { printf '\n== %s ==\n' "$*"; }
run() {
    if [ "$DRY" -eq 1 ]; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi
}

# ------------------------------------------------------- 1. stop + remove --
say "service $SVC"
if systemctl list-unit-files "$SVC.service" 2>/dev/null | grep -q "$SVC"; then
    # disable --now = SIGTERM; the daemon restores firmware control of the
    # fans it was driving during its clean shutdown.
    run systemctl disable --now "$SVC"
    run rm -f "$UNIT_DST" "$UNIT_DST.bak"
    run systemctl daemon-reload
else
    echo "not installed (no unit file)"
fi

# --------------------------------------------------------- 2. runtime opt --
say "runtime: $OPT"
if [ -d "$OPT" ]; then
    run rm -rf "$OPT"
else
    echo "absent"
fi
if [ -e "$LAUNCHER" ]; then
    run rm -f "$LAUNCHER" "$LAUNCHER.bak"
else
    echo "launcher absent: $LAUNCHER"
fi

# ------------------------------------------------------- 3. config + log ---
say "system files"
if [ "$PURGE" -eq 1 ]; then
    for d in "$CONFIG_DIR" "$LOG_DIR" "$RUN_DIR"; do
        if [ -e "$d" ]; then
            [ "$d" = "$CONFIG_DIR" ] && \
                echo "WARNING: removing $d — your assignments/curves go with it"
            run rm -rf "$d"
        fi
    done
else
    [ -e "$CONFIG_DIR" ] && \
        echo "kept (use --purge to remove): $CONFIG_DIR"
fi

say "done"
cat <<EOF

Kept: source tree $SRC (your code — delete it yourself if you want).
Fans: every channel the daemon drove is back under BIOS/firmware control.
EOF
