#!/bin/bash
# Verify the INSTALL/DISTRIBUTION path end-to-end WITHOUT installing
# anything on this system: builds a throwaway venv, does a NON-EDITABLE
# install of the current source (exactly what install.sh does into
# /opt), and exercises the installed package from a neutral cwd.
#
#   bash scripts/verify_install.sh          (no sudo needed, ~1 min)
#
# Run this after any packaging-relevant change (pyproject, package
# layout, entry points) and before declaring an upgrade shippable.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d /tmp/alx-verify-install.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
VENV="$WORK/venv"

echo "== building throwaway venv: $VENV"
python3 -m venv --without-pip "$VENV"
if [ -f "$SRC/scripts/get-pip.py" ]; then
    "$VENV/bin/python" "$SRC/scripts/get-pip.py" -q
else
    curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$WORK/get-pip.py"
    "$VENV/bin/python" "$WORK/get-pip.py" -q
fi

echo "== non-editable install of $SRC (with [tui] extra)"
"$VENV/bin/pip" install -q "$SRC[tui]"

echo "== installed-package checks (from NEUTRAL cwd — not the source tree)"
cd /
PKG_FILE="$("$VENV/bin/python" -c 'import alx_fancontrol; print(alx_fancontrol.__file__)')"
case "$PKG_FILE" in
    "$SRC"/*) echo "FAIL: import resolved to the SOURCE TREE, not the venv"; exit 1;;
    "$VENV"/*) echo "ok: import resolves to the installed snapshot: $PKG_FILE";;
    *) echo "FAIL: unexpected import location: $PKG_FILE"; exit 1;;
esac
"$VENV/bin/alx-fancontrol" --version
"$VENV/bin/python" -m alx_fancontrol.daemon --help >/dev/null
echo "ok: entry point + daemon module import under the installed package"
"$VENV/bin/python" -c "import textual; print('ok: textual', textual.__version__)"

echo "== installed daemon: live DRY-RUN --once (reads /sys, writes nothing)"
CFG="$WORK/config.json"
"$VENV/bin/python" -m alx_fancontrol.cli check --config "$CFG" >/dev/null
# daemon logs to stderr — merge it so grep sees the lifecycle lines
"$VENV/bin/python" -m alx_fancontrol.daemon --config "$CFG" --dry-run --once 2>&1 \
    | grep -E "daemon started|daemon stopped"

echo
echo "VERIFY-INSTALL-OK: the /opt snapshot + service path would work."
