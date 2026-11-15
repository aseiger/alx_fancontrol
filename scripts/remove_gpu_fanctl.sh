#!/usr/bin/env bash
# Remove gpu-fanctl from this machine — completely. It is superseded by
# alx_fancontrol, which now drives the same headers (and has the fail-safe,
# ownership marker, nvidia-smi timeout and watchdog that gpu-fanctl never had).
#
#   sudo bash scripts/remove_gpu_fanctl.sh              # do it
#        bash scripts/remove_gpu_fanctl.sh --dry-run    # show what it would do
#
# Removed (everything is archived first, see ARCHIVE below):
#   /etc/systemd/system/gpu-fanctl.service   disable --now + daemon-reload
#   /usr/local/bin/gpu-fanctl.py
#   /usr/local/bin/fan-calibration.json      gpu-fanctl's own calibration file
#   ~<user>/gpu-fanctl/                      the original source tree
#   ~<user>/fanctl-test.sh, fanctl-test-*.txt  scratch files from that work
#
# ARCHIVE: a tarball at /var/backups/gpu-fanctl-<timestamp>.tar.gz containing
# exactly what was deleted — including the duty→RPM→temperature calibration
# measurements, which are the only copy of that sweep. Restore with:
#   tar -xzf /var/backups/gpu-fanctl-<ts>.tar.gz -C /          # -P paths
#
# SAFETY PRE-CHECK — refuses to run unless alx_fancontrol demonstrably cools
# the channels gpu-fanctl used to drive (it8792 pwm1 + pwm3): assigned and
# enabled in the config, actually held by the daemon (pwmN_enable=1), and the
# service active. Removing gpu-fanctl while nothing owns those headers leaves
# the GPU blowers to a BIOS curve, and this board parks unowned headers at 0%.
set -uo pipefail

UNIT="gpu-fanctl"
SVC="alx_fancontrol"
CONFIG="/etc/$SVC/config.json"
CRITICAL_CHANNELS=("it8792:pwm1" "it8792:pwm3")   # what gpu-fanctl drove
TS="$(date +%Y%m%d-%H%M%S)"
BACKUP="/var/backups/gpu-fanctl-$TS.tar.gz"
DRY=0

case "${1:-}" in
    --dry-run) DRY=1 ;;
    "")        ;;
    *) echo "usage: $0 [--dry-run]"; exit 2 ;;
esac

# home of the invoking user (the source tree + scratch files live there)
UREF="${SUDO_USER:-$USER}"
UHOME="$(getent passwd "$UREF" 2>/dev/null | cut -d: -f6)"
[ -n "$UHOME" ] || UHOME="$HOME"

run() {                      # echo + (unless dry-run) execute
    echo "  + $*"
    [ "$DRY" -eq 1 ] && return 0
    eval "$@"
}

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

# ---------------------------------------------------- safety pre-check -----
say "pre-check: is alx_fancontrol cooling the GPU blowers?"
FAIL=0

if [ ! -r "$CONFIG" ]; then
    echo "  !! $CONFIG not readable — cannot verify coverage"
    FAIL=1
else
    ASSIGNED="$(python3 - "$CONFIG" <<'PY' 2>/dev/null
import json, sys
try:
    doc = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit(0)
for a in (doc.get("assignments") or {}).values():
    if isinstance(a, dict) and a.get("enabled", True):
        for f in a.get("fans", []):
            print(f)
PY
)"
    for ch in "${CRITICAL_CHANNELS[@]}"; do
        if printf '%s\n' "$ASSIGNED" | grep -qx "$ch"; then
            echo "  ok   $ch is assigned to $SVC"
        else
            echo "  !!   $ch is NOT assigned (and enabled) in $CONFIG"
            echo "       assign it first — in the TUI or by hand — before removing gpu-fanctl"
            FAIL=1
        fi
    done
fi

IT="$(for h in /sys/class/hwmon/hwmon*; do
        case "$(cat "$h/name" 2>/dev/null)" in it8792*) echo "$h"; break;; esac
      done)"
if [ -z "$IT" ]; then
    echo "  !! no it8792 hwmon chip found — cannot verify channel ownership"
    FAIL=1
else
    for ch in "${CRITICAL_CHANNELS[@]}"; do
        pwm="${ch##*:}"
        en="$(cat "$IT/${pwm}_enable" 2>/dev/null || echo '?')"
        rpm="$(cat "$IT/fan${pwm#pwm}_input" 2>/dev/null || echo '?')"
        if [ "$en" = "1" ]; then
            echo "  ok   $ch held in manual mode (enable=1, tach ${rpm} RPM)"
        else
            echo "  !!   $ch enable=$en — $SVC does not own it right now"
            echo "       check: journalctl -u $SVC | grep -i '$ch'  (manual-conflict?)"
            FAIL=1
        fi
    done
fi

SVCSTATE="$(systemctl is-active "$SVC" 2>/dev/null || true)"
if [ "$SVCSTATE" = "active" ]; then
    echo "  ok   $SVC.service is active"
else
    echo "  !!   $SVC.service is '$SVCSTATE' — do not remove the fallback controller"
    echo "       while the replacement is not running"
    FAIL=1
fi

if [ -r "/run/$SVC/status.json" ]; then
    ERRS="$(python3 -c "
import json,sys
try: st=json.load(open(sys.argv[1]))
except Exception: raise SystemExit('')
print('; '.join(f'{k}: {v}' for k,v in (st.get('errors') or {}).items()))
" "/run/$SVC/status.json" 2>/dev/null)"
    if [ -n "$ERRS" ]; then
        echo "  note status.json reports errors: $ERRS"
    fi
fi

if [ "$FAIL" -ne 0 ]; then
    echo
    echo "ABORTED — alx_fancontrol is not demonstrably cooling the channels"
    echo "gpu-fanctl drives. Fix coverage first (assign the channels, restart"
    echo "the service), then re-run. Nothing was changed."
    exit 1
fi
echo "  -> coverage confirmed; safe to remove gpu-fanctl"

# --------------------------------------------------------- what exists ----
TARGETS=()
for p in /etc/systemd/system/$UNIT.service \
         /usr/local/bin/$UNIT.py \
         /usr/local/bin/fan-calibration.json \
         "$UHOME/gpu-fanctl" \
         "$UHOME/fanctl-test.sh"; do
    [ -e "$p" ] && TARGETS+=("$p")
done
for p in "$UHOME"/fanctl-test-*.txt; do
    [ -e "$p" ] && TARGETS+=("$p")
done

say "targets"
if [ "${#TARGETS[@]}" -eq 0 ]; then
    echo "  nothing to remove — gpu-fanctl is already gone from disk"
else
    for p in "${TARGETS[@]}"; do echo "  $(ls -ld "$p")"; done
fi

# ------------------------------------------------------------- archive ----
say "archive (so the calibration measurements survive)"
if [ "${#TARGETS[@]}" -eq 0 ]; then
    echo "  skipped (no targets)"
elif [ "$DRY" -eq 1 ]; then
    echo "  would run: tar -czf $BACKUP -P ${TARGETS[*]}"
else
    mkdir -p /var/backups
    if tar -czf "$BACKUP" -P "${TARGETS[@]}" 2>/dev/null; then
        echo "  wrote $BACKUP ($(du -h "$BACKUP" | cut -f1))"
    else
        echo "  !! archiving failed — refusing to delete anything"
        exit 1
    fi
fi

# ------------------------------------------------------------ remove ------
say "remove service"
if systemctl list-unit-files "$UNIT.service" --no-legend 2>/dev/null | grep -q "$UNIT"; then
    run "systemctl stop $UNIT 2>/dev/null || true"
    run "systemctl disable $UNIT 2>/dev/null || true"
    if [ -e "/etc/systemd/system/$UNIT.service" ]; then
        echo "  + rm /etc/systemd/system/$UNIT.service"
        [ "$DRY" -eq 0 ] && rm -f "/etc/systemd/system/$UNIT.service"
    fi
    run "systemctl daemon-reload"
    run "systemctl reset-failed $UNIT 2>/dev/null || true"
else
    echo "  unit not installed"
fi

say "remove binaries and data"
for p in /usr/local/bin/$UNIT.py /usr/local/bin/fan-calibration.json; do
    if [ -e "$p" ]; then
        [ "$DRY" -eq 0 ] && rm -f "$p"
        echo "  + rm $p"
    fi
done

say "remove the original source tree and scratch files ($UHOME)"
for p in "$UHOME/gpu-fanctl" "$UHOME/fanctl-test.sh" "$UHOME"/fanctl-test-*.txt; do
    if [ -e "$p" ]; then
        [ "$DRY" -eq 0 ] && rm -rf "$p"
        echo "  + rm $p"
    fi
done

# ------------------------------------------------------------ verify -----
say "verify"
if [ "$DRY" -eq 1 ]; then
    echo "  (dry-run: nothing changed, so verification is skipped)"
    echo
    echo "would archive to: $BACKUP"
    exit 0
fi

OK=1
if systemctl list-unit-files "$UNIT.service" --no-legend 2>/dev/null | grep -q "$UNIT"; then
    echo "  !! unit still known to systemd"; OK=0
else
    echo "  ok   unit gone from systemd"
fi
LEFT=""
for p in /etc/systemd/system/$UNIT.service /usr/local/bin/$UNIT.py \
         /usr/local/bin/fan-calibration.json "$UHOME/gpu-fanctl"; do
    if [ -e "$p" ]; then echo "  !! still present: $p"; OK=0; LEFT="$LEFT $p"; fi
done
[ -n "$LEFT" ] || echo "  ok   files gone"

NEWSTATE="$(systemctl is-active "$SVC" 2>/dev/null || true)"
[ "$NEWSTATE" = "active" ] && echo "  ok   $SVC still active" \
    || { echo "  !! $SVC is '$NEWSTATE' — bring it back NOW: systemctl start $SVC"; OK=0; }

if [ -n "$IT" ]; then
    for ch in "${CRITICAL_CHANNELS[@]}"; do
        pwm="${ch##*:}"
        en="$(cat "$IT/${pwm}_enable" 2>/dev/null || echo '?')"
        duty="$(cat "$IT/$pwm" 2>/dev/null || echo '?')"
        rpm="$(cat "$IT/fan${pwm#pwm}_input" 2>/dev/null || echo '?')"
        [ "$en" = "1" ] \
            && echo "  ok   $ch still owned (duty=$duty tach=${rpm} RPM)" \
            || { echo "  !! $ch enable=$en — GPU blower is NOT under software control"; OK=0; }
    done
fi

echo
if [ "$OK" -eq 1 ]; then
    echo "gpu-fanctl removed. Archive: $BACKUP"
    echo "Cooling is alx_fancontrol only now:  systemctl status $SVC"
    echo "                                    journalctl -u $SVC -f"
else
    echo "DONE WITH WARNINGS — read the !! lines above."
fi
exit $(( OK ? 0 : 1 ))
