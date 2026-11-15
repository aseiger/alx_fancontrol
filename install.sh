#!/bin/bash
# Install alx_fancontrol as a proper system service.
#
#   sudo bash install.sh              # build + install + start
#   sudo bash install.sh --dry-run    # (no sudo needed) print what would happen
#
# Install model:
#   source tree   this directory (dev: code, tests, dev venv in .venv/)
#   runtime       /opt/alx_fancontrol/venv  — a FROZEN SNAPSHOT: a
#                 non-editable `pip install` of the current source. The
#                 service runs THIS, not the source tree (the tree can be
#                 deleted without affecting the running system).
#   config        /etc/alx_fancontrol/config.json
#   status        /run/alx_fancontrol/status.json     (RuntimeDirectory=)
#   log           /var/log/alx_fancontrol/daemon.log  (LogsDirectory=) + journal
#
# Upgrading: change the source tree, re-run `sudo bash install.sh`
# (refreshes the /opt snapshot from the current source and restarts).
# Removing: `sudo bash uninstall.sh` (see that script; --purge for /etc).
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
SVC="alx_fancontrol"
OPT="/opt/$SVC"
VENV="$OPT/venv"
UNIT_SRC="$SRC/alx-fancontrol.service"
UNIT_DST="/etc/systemd/system/$SVC.service"
LAUNCHER_SRC="$SRC/alx-fancontrol"
LAUNCHER_DST="/usr/local/bin/alx-fancontrol"
CONFIG_DIR="/etc/$SVC"
CONFIG="$CONFIG_DIR/config.json"
# legacy config location from earlier development (underscore dir!)
LEGACY_DIR="${SUDO_USER:+$(getent passwd "${SUDO_USER}" | cut -d: -f6)/.config/$SVC}"
[ -n "${LEGACY_DIR:-}" ] || LEGACY_DIR="$HOME/.config/$SVC"
DRY=0

case "${1:-}" in
    --dry-run) DRY=1 ;;
    "")        ;;
    *) echo "usage: $0 [--dry-run]   (uninstall: see uninstall.sh)"; exit 2 ;;
esac

if [ "$DRY" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: run with sudo (or use --dry-run)" >&2
    exit 1
fi

say() { printf '\n== %s ==\n' "$*"; }
run() {
    if [ "$DRY" -eq 1 ]; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi
}

# ------------------------------------------------- 1. build /opt snapshot --
say "runtime install: $OPT (snapshot of $SRC)"
if [ ! -x "$VENV/bin/python" ]; then
    if [ "$DRY" -eq 1 ]; then
        echo "  [dry-run] python3 -m venv --without-pip $VENV && get-pip"
    else
        mkdir -p "$OPT"
        python3 -m venv --without-pip "$VENV"   # Debian PEP 668: no ensurepip
        if [ -f "$SRC/scripts/get-pip.py" ]; then
            "$VENV/bin/python" "$SRC/scripts/get-pip.py" -q
        else
            GETPIP="$(mktemp)"
            curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$GETPIP"
            "$VENV/bin/python" "$GETPIP" -q
            rm -f "$GETPIP"
        fi
    fi
else
    echo "venv present: $VENV"
fi

if [ "$DRY" -eq 1 ]; then
    echo "  [dry-run] $VENV/bin/pip install [--force-reinstall --no-deps] '$SRC' (+ '[tui]' on first install)"
else
    if "$VENV/bin/python" -c "import textual" 2>/dev/null; then
        # refresh the snapshot from current source; deps untouched (offline-ok)
        "$VENV/bin/pip" install -q --force-reinstall --no-deps "$SRC"
    else
        "$VENV/bin/pip" install -q "$SRC[tui]"
    fi
    echo "snapshot installed:"
    "$VENV/bin/alx-fancontrol" --version
    "$VENV/bin/python" -c "import textual; print('  textual', textual.__version__)"
fi

# --------------------------------------------------------- 2. config (etc) --
# System config in /etc, root-owned. A pre-existing home-dir config from
# earlier development is migrated once. `cli check` seeds when missing
# (first run: discovers sources, creates zero assignments) and validates.
say "config: $CONFIG"
if [ ! -e "$CONFIG" ] && [ -f "$LEGACY_DIR/config.json" ]; then
    if [ "$DRY" -eq 1 ]; then
        echo "  [dry-run] would migrate $LEGACY_DIR/config.json -> $CONFIG"
    else
        echo "migrating existing config: $LEGACY_DIR/ -> $CONFIG_DIR/"
        mkdir -p "$CONFIG_DIR"
        cp -a "$LEGACY_DIR/config.json" "$CONFIG"
        [ -f "$LEGACY_DIR/config.json.bak" ] && cp -a "$LEGACY_DIR/config.json.bak" "$CONFIG_DIR/"
    fi
fi
if [ "$DRY" -eq 1 ]; then
    echo "  [dry-run] would seed-if-missing + validate via: $VENV/bin/python -m alx_fancontrol.cli check"
else
    mkdir -p "$CONFIG_DIR"
    if [ ! -e "$CONFIG" ]; then echo "config missing — will seed"; else echo "validating"; fi
    "$VENV/bin/python" -m alx_fancontrol.cli check
fi

# ------------------------------------------------- 3. stop manual daemons --
say "manual daemon instances"
# exclude our own process tree (pgrep -f can match shells whose command
# line happens to contain the pattern)
MANUAL_PIDS="$(pgrep -f "alx[-_]fancontrol( daemon|\.daemon)" \
    | grep -vE "^($$|$PPID)$" || true)"
if [ -n "$MANUAL_PIDS" ]; then
    echo "found: $MANUAL_PIDS — stopping (the service will own control now)"
    run kill $MANUAL_PIDS
    [ "$DRY" -eq 0 ] && sleep 1
else
    echo "none"
fi

# -------------------------------------------------- 4. launcher + unit ----
say "launcher: $LAUNCHER_DST (so: sudo alx-fancontrol tui|daemon|check)"
if [ "$DRY" -eq 1 ]; then
    echo "  [dry-run] install -m 755 $LAUNCHER_SRC $LAUNCHER_DST"
else
    [ -e "$LAUNCHER_DST" ] && cp -f "$LAUNCHER_DST" "$LAUNCHER_DST.bak"
    install -m 755 "$LAUNCHER_SRC" "$LAUNCHER_DST"
fi

say "installing unit -> $UNIT_DST"
if [ "$DRY" -eq 1 ]; then
    sed 's/^/    /' "$UNIT_SRC"
else
    [ -e "$UNIT_DST" ] && cp -f "$UNIT_DST" "$UNIT_DST.bak"
    cp -f "$UNIT_SRC" "$UNIT_DST"
    systemctl daemon-reload
    if systemctl is-enabled "$SVC" >/dev/null 2>&1; then
        systemctl restart "$SVC"
    else
        systemctl enable --now "$SVC"
    fi
fi

# --------------------------------------------------------- 5. verify -------
if [ "$DRY" -eq 0 ]; then
    sleep 2
    say "verification"
    echo "service:      $(systemctl is-active $SVC)"
    echo "gpu-fanctl:   $(systemctl is-active gpu-fanctl 2>/dev/null || echo MISSING)"
    # Who owns the GPU-blower headers? If THIS daemon's config assigns them,
    # it must be the one holding them (manual, enable=1) — a dead alx_fancontrol
    # used to mean those channels were refused by the next instance and pinned
    # at their last duty. Otherwise gpu-fanctl owns them and the daemon must
    # refuse them.
    ASSIGNED="$(python3 - "$CONFIG" <<'PY' 2>/dev/null
import json, sys
try:
    doc = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit("")
fans = {f for a in (doc.get("assignments") or {}).values()
        if isinstance(a, dict) and a.get("enabled", True)
        for f in a.get("fans", [])}
print(" ".join(sorted(fans)))
PY
)"
    IT8792="$(for h in /sys/class/hwmon/hwmon*; do
        case "$(cat "$h/name" 2>/dev/null)" in it8792*) echo "$h"; break;; esac
    done)"
    if [ -n "$IT8792" ]; then
        e1="$(cat "$IT8792/pwm1_enable" 2>/dev/null || echo '?')"
        e3="$(cat "$IT8792/pwm3_enable" 2>/dev/null || echo '?')"
        case " $ASSIGNED " in
            *" it8792:pwm1 "*|*" it8792:pwm3 "*)
                echo "it8792 pwm1/3 enable: $e1/$e3 (assigned to $SVC — expect 1/1, held by the daemon)"
                [ "$e1" = "1" ] && [ "$e3" = "1" ] \
                    || echo "WARNING: assigned GPU-blower channels are not in manual mode — the daemon does not own them; check 'journalctl -u $SVC' (manual-conflict means another process holds them)"
                ;;
            *)
                echo "it8792 pwm1/3 enable: $e1/$e3 (not assigned here — expect 1/1 while gpu-fanctl owns them)"
                [ "$e1" = "1" ] && [ "$e3" = "1" ] || echo "WARNING: it8792 pwm1/3 not in manual mode — gpu-fanctl no longer owns them; the daemon could take them over if assigned!"
                ;;
        esac
    fi
    # The ownership marker is what makes a restart reclaim its channels
    echo "marker:       $(cat /var/lib/alx_fancontrol/owned.json 2>/dev/null || echo '(none yet — written on the first takeover)')"
    # Anything the daemon considers an error is worth seeing right after install
    python3 - <<'PY' 2>/dev/null
import json, os
for p in ("/run/alx_fancontrol/status.json",
          os.path.expanduser("~/.config/alx_fancontrol/status.json")):
    try:
        st = json.load(open(p))
    except Exception:
        continue
    print(f"status:       pid={st.get('pid')} degraded={st.get('degraded')}")
    errs = st.get("errors") or {}
    print("errors:       " + ("; ".join(f"{k}: {v}" for k, v in errs.items())
                              if errs else "none"))
    for fan, f in sorted((st.get("fans") or {}).items()):
        if f.get("state") not in (None, "ok"):
            print(f"              {fan}: state={f.get('state')} "
                  f"duty={f.get('duty_pct')}% rpm={f.get('rpm')}")
    break
PY
    if [ "$(systemctl is-active gpu-fanctl 2>/dev/null)" != "active" ] \
            && ! case " $ASSIGNED " in *" it8792:pwm1 "*|*" it8792:pwm3 "*) true;; *) false;; esac; then
        echo "WARNING: gpu-fanctl is not running and $SVC does not assign the"
        echo "         V100 blowers (it8792 pwm1/pwm3) — they fall back to the BIOS curve."
    fi
    echo
    echo "--- daemon log (last 12 lines) ---"
    journalctl -u "$SVC" -n 12 --no-pager
fi

say "done"
cat <<EOF

Daemon:    systemctl status $SVC
Logs:      journalctl -u $SVC -f      (file: /var/log/$SVC/daemon.log)
Config:    $CONFIG   (edit via the TUI, or by hand)
TUI:       sudo alx-fancontrol tui   (launcher: $LAUNCHER_DST)
Upgrade:   edit $SRC, then:  sudo bash $SRC/install.sh
Uninstall: sudo bash $SRC/uninstall.sh   [--purge to also remove $CONFIG_DIR]
EOF
