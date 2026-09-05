#!/usr/bin/env bash
# Install the GL.iNet GL-ATXPC ATX driver (glatx) into PiKVM kvmd.
# Idempotent: safe to re-run any time. Re-run after major kvmd/python updates.
# Run as root on the PiKVM itself, from the directory containing:
#   glatx.py  glatx.override.yaml  99-glatx.rules
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: run as root" >&2
    exit 1
fi
HERE="$(cd "$(dirname "$0")" && pwd)"

# 1) Canonical copy in a location no package ever touches.
install -D -m 0644 "$HERE/glatx.py" /etc/kvmd-glatx/glatx.py

# 2) Symlink it into the kvmd package (plugin loader imports
#    kvmd.plugins.atx.<type>, so the module must live inside the package).
#    The symlink itself is unowned by pacman, so regular `pacman -Syu`
#    leaves it in place. A python major-version bump moves site-packages,
#    hence: re-run this script after big updates.
KVMD_PKG="$(python3 -c 'import kvmd, os; print(os.path.dirname(kvmd.__file__))')"
ATX_DIR="$KVMD_PKG/plugins/atx"
if [ ! -d "$ATX_DIR" ]; then
    echo "ERROR: kvmd layout changed (no $ATX_DIR)." >&2
    echo "A native glatx driver may exist upstream; check before forcing this." >&2
    exit 1
fi
ln -sfn /etc/kvmd-glatx/glatx.py "$ATX_DIR/glatx.py"

# 3) Stable device name /dev/glatx (USB ID 1209:c550).
install -D -m 0644 "$HERE/99-glatx.rules" /etc/udev/rules.d/99-glatx.rules
udevadm control --reload
udevadm trigger --subsystem-match=tty || true

# 4) Config override. /etc/kvmd/override.d is merged by kvmd if present.
if [ -d /etc/kvmd/override.d ]; then
    [ -e /etc/kvmd/override.d/glatx.yaml ] || \
        install -D -m 0644 "$HERE/glatx.override.yaml" /etc/kvmd/override.d/glatx.yaml
else
    echo "NOTE: /etc/kvmd/override.d does not exist (older kvmd)."
    echo "Merge glatx.override.yaml into /etc/kvmd/override.yaml manually."
fi

# 5) Validate config (also verifies the plugin imports cleanly).
if kvmd -m >/dev/null 2>&1; then
    echo "OK: kvmd config valid, plugin imports."
else
    echo "WARNING: kvmd config check FAILED." >&2
    echo "Recover with:  rm -f /etc/kvmd/override.d/glatx.yaml  (then: kvmd -m)" >&2
    exit 1
fi

echo
echo "Device:    $(ls -l /dev/glatx 2>/dev/null || echo '/dev/glatx absent - is the board plugged in?')"
echo "Activate:  systemctl restart kvmd"
echo
echo "Read-only check (presses nothing): curl -k -u admin:<password> https://127.0.0.1/api/atx"
echo "CAUTION: any /api/atx/power or /api/atx/click call WILL press the target PC's power/reset."
