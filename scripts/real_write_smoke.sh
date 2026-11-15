#!/usr/bin/env bash
# OPTIONAL real-write smoke test — it8686 pwm3 (SYS_FAN2) only.
#
# This is the ONE channel the plan marks safe for real writes: a spinning
# chassis fan (~3200 RPM, tach fan3_input), NOT a GPU blower. It is
# self-restoring: the daemon takes the channel over (enable 2->1), drives it
# for --duration seconds, and the clean-exit finally() returns it to firmware
# (enable -> 2).
#
# gpu-fanctl used to be part of the safety story here ("not a channel another
# controller owns"). It has been removed — scripts/remove_gpu_fanctl.sh — so
# alx_fancontrol is the only writer on this machine, and the guardrail is now
# "did the experiment put everything back". That means the LIVE service must
# be stopped first, or two daemons fight over the same headers:
#
#   sudo systemctl stop alx_fancontrol
#   bash scripts/real_write_smoke.sh
#   sudo systemctl start alx_fancontrol
#
# Pre-checks: k10temp Tctl < 85 C (abort otherwise — CPU must stay cool);
#             live alx_fancontrol inactive; it8686 pwm3 in firmware mode.
# Post-checks: it8686 pwm3_enable back to 2 and duty moved away from its
#              pre-run value; EVERY other channel identical to the pre-run
#              snapshot (nothing else was touched).
#
# Run it ONLY when you explicitly want to spin a fan for ~20 s:
#   bash scripts/real_write_smoke.sh
set -euo pipefail
cd "$(dirname "$0")/.."

WORKDIR="$(mktemp -d /tmp/alx-fc-realwrite.XXXXXX)"
trap 'rm -rf "$WORKDIR"' EXIT

echo "== pre-checks =="
K10="$(ls -d /sys/class/hwmon/hwmon* | while read -r p; do
  case "$(cat "$p/name" 2>/dev/null)" in k10temp*) echo "$p"; break;; esac
done | head -1)"
TCTL="$(cat "$K10/temp1_input")"
TCTL_C=$((TCTL / 1000))
echo "  k10temp Tctl: ${TCTL_C} C"
if [ "$TCTL" -ge 85000 ]; then
  echo "ABORT: Tctl >= 85 C — not running the fan experiment" >&2
  exit 1
fi

IT8686="$(ls -d /sys/class/hwmon/hwmon* | while read -r p; do
  case "$(cat "$p/name" 2>/dev/null)" in it8686*) echo "$p"; break;; esac
done | head -1)"
[ -n "$IT8686" ] || { echo "ABORT: it8686 hwmon not found" >&2; exit 1; }
LIVE="$(systemctl is-active alx_fancontrol 2>/dev/null || true)"
if [ "$LIVE" = "active" ]; then
  echo "ABORT: alx_fancontrol.service is active and owns these headers." >&2
  echo "       Run 'sudo systemctl stop alx_fancontrol' first (and start it" >&2
  echo "       again afterwards): two daemons writing the same pwmN is not a" >&2
  echo "       test." >&2
  exit 1
fi
PRE_DUTY="$(cat "$IT8686/pwm3")"
PRE_EN="$(cat "$IT8686/pwm3_enable")"
PRE_RPM="$(cat "$IT8686/fan3_input" 2>/dev/null || echo 0)"
echo "  it8686 pwm3: duty=${PRE_DUTY} enable=${PRE_EN} fan3=${PRE_RPM} RPM"
if [ "$PRE_EN" != "2" ]; then
  echo "ABORT: it8686 pwm3 enable=${PRE_EN} (expected 2 = firmware)" >&2
  exit 1
fi

python3 scripts/protect.py snapshot "$WORKDIR/protect.snap"

CFG="$WORKDIR/config.json"
cat > "$CFG" <<EOF
{
  "version": 1,
  "config": {"poll_seconds": 1.0, "max_rate": 6, "floor_duty": 5, "dead_temp_c": -10},
  "sources": {
    "sys1": {"kind": "hwmon", "chip": "it8686", "temp": 1, "label": "System 1", "enabled": true}
  },
  "curves": {
    "realwrite": {"label": "real-write test", "points": [[40, 0], [45, 10], [50, 40], [60, 80], [70, 100]]}
  },
  "assignments": {
    "sysfan2-test": {
      "source": "sys1", "curve": "realwrite",
      "fans": ["it8686:pwm3"],
      "tach": {"it8686:pwm3": "it8686:fan3"},
      "floor_duty": 5, "max_duty": 100, "enabled": true
    }
  }
}
EOF

echo "== running daemon for 20 s on it8686 pwm3 (SYS_FAN2) =="
.venv/bin/alx-fancontrol daemon --config "$CFG" --duration 20 2>&1 | tee "$WORKDIR/daemon.log" \
  | grep -E "took over|write it8686|returned|daemon (started|stopped)" || true

echo "== post-checks =="
POST_EN="$(cat "$IT8686/pwm3_enable")"
POST_DUTY="$(cat "$IT8686/pwm3")"
POST_RPM="$(cat "$IT8686/fan3_input" 2>/dev/null || echo 0)"
echo "  it8686 pwm3: duty=${PRE_DUTY} -> ${POST_DUTY}, enable -> ${POST_EN}, rpm ${PRE_RPM} -> ${POST_RPM}"
FAIL=0
if [ "$POST_EN" != "2" ]; then
  echo "FAIL: pwm3_enable not restored to 2 (got ${POST_EN})" >&2; FAIL=1
fi
if [ "$POST_DUTY" = "$PRE_DUTY" ] && ! grep -q "write it8686:pwm3" "$WORKDIR/daemon.log"; then
  echo "FAIL: duty never moved and no write was logged" >&2; FAIL=1
fi
# Every OTHER channel must be exactly as it was: the experiment is only
# allowed to touch it8686:pwm3. (protect.py `check` asserts the daemon OWNS
# its channels, which is false by design while the service is stopped for
# this test, so compare the two snapshots directly instead.)
python3 scripts/protect.py snapshot "$WORKDIR/after.snap"
if python3 - "$WORKDIR/protect.snap" "$WORKDIR/after.snap" <<'PY'
import json, sys
before = json.load(open(sys.argv[1]))["state"]["guarded"]
after = json.load(open(sys.argv[2]))["state"]["guarded"]
bad = []
for fan in sorted(set(before) | set(after)):
    if fan == "it8686:pwm3":
        continue
    if before.get(fan) != after.get(fan):
        bad.append((fan, before.get(fan), after.get(fan)))
for fan, b, a in bad:
    print(f"FAIL: {fan} changed: {b} -> {a}", file=sys.stderr)
sys.exit(1 if bad else 0)
PY
then
  echo "  untouched-channel check: OK (only it8686:pwm3 moved)"
else
  echo "FAIL: channels other than it8686:pwm3 were disturbed" >&2; FAIL=1
fi
echo
if [ "$FAIL" -ne 0 ]; then
  echo "REAL-WRITE SMOKE: FAIL" >&2
  exit 1
fi
echo "REAL-WRITE SMOKE: OK (fan experiment self-restored, nothing else touched)"
echo "remember: sudo systemctl start alx_fancontrol"
