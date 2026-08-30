#!/usr/bin/env bash
# One-time host setup (run as root) completing low-impact indexing isolation.
#
# The backend already wraps heavy jobs in systemd user scopes with
# CPUWeight=idle + MemoryHigh — that works out of the box. This script adds
# the two pieces that need root:
#
#   1. io cgroup controller delegation so the scopes' IOWeight is enforced
#      (blk-iocost works with the default `none` NVMe scheduler).
#   2. Bounded dirty writeback so bulk copies can't queue multi-GB flush
#      storms that stall unrelated fsyncs (256 MB background / 2 GB hard,
#      instead of 10%/20% of RAM ≈ 3/6 GB on a 32 GB machine).
#
# Both are optional defense-in-depth: CPU + page-cache isolation alone already
# removes most desktop lag.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root: sudo $0" >&2
    exit 1
fi

echo "== 1. Delegate the io controller to user sessions =="
mkdir -p /etc/systemd/system/user@.service.d
cat > /etc/systemd/system/user@.service.d/atr-delegate.conf <<'EOF'
# Added by anime-tiktok-reproducer scripts/setup_low_impact_host.sh:
# delegate io in addition to the defaults so low-impact indexing scopes
# can carry IOWeight.
[Service]
Delegate=cpu cpuset io memory pids
EOF
# Turning on IO accounting for the user tree makes systemd enable the io
# controller on every slice along the path.
systemctl set-property user.slice IOAccounting=yes
systemctl daemon-reload

echo "== 2. Enable blk-iocost on the root NVMe =="
root_source=$(findmnt -no SOURCE /)
root_disk=$(lsblk -no PKNAME "$root_source" | head -1)
devno=$(cat "/sys/block/${root_disk}/dev")
echo "root disk: ${root_disk} (${devno})"
echo "${devno} enable=1" > /sys/fs/cgroup/io.cost.qos
# Persist across reboots.
mkdir -p /etc/tmpfiles.d
cat > /etc/tmpfiles.d/atr-iocost.conf <<EOF
# Enable blk-iocost on the root disk so cgroup io.weight is enforced with
# the 'none' scheduler (anime-tiktok-reproducer low-impact indexing).
w /sys/fs/cgroup/io.cost.qos - - - - ${devno} enable=1
EOF

echo "== 3. Bound dirty writeback =="
cat > /etc/sysctl.d/90-atr-writeback.conf <<'EOF'
# Bound dirty page buildup so bulk media copies flush incrementally instead
# of in multi-GB bursts that stall interactive fsyncs.
vm.dirty_background_bytes = 268435456
vm.dirty_bytes = 2147483648
EOF
sysctl --system >/dev/null

echo "Done. Log out/in (or reboot) for the user@ delegation to fully apply."
