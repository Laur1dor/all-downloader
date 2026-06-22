#!/bin/sh
# Cap egress (and best-effort ingress) on eth0 to BANDWIDTH_MBIT.
# Requires the container to have cap_add: NET_ADMIN. No-op when unset or unable.
#
# NOTE: reliable bidirectional shaping is best done on the LXC host's interface;
# see the README. In-container egress shaping below works with NET_ADMIN alone.
set -u

DEV="${SHAPE_DEV:-eth0}"
RATE="${BANDWIDTH_MBIT:-0}"

case "$RATE" in
    ''|*[!0-9]*) exit 0 ;;   # not a number → disabled
esac
[ "$RATE" -gt 0 ] || exit 0

tc qdisc del dev "$DEV" root 2>/dev/null || true
if tc qdisc add dev "$DEV" root tbf rate "${RATE}mbit" burst 1mbit latency 400ms 2>/dev/null; then
    echo "netshape: egress on $DEV capped to ${RATE} Mbit/s"
else
    echo "netshape: could not shape $DEV (needs NET_ADMIN) — skipping"
    exit 0
fi

# Best-effort ingress shaping via an IFB device (needs the ifb module on host).
if ip link add ifb0 type ifb 2>/dev/null; then
    ip link set ifb0 up 2>/dev/null || true
    tc qdisc add dev "$DEV" handle ffff: ingress 2>/dev/null || true
    tc filter add dev "$DEV" parent ffff: protocol ip u32 match u32 0 0 \
        action mirred egress redirect dev ifb0 2>/dev/null || true
    if tc qdisc add dev ifb0 root tbf rate "${RATE}mbit" burst 1mbit latency 400ms 2>/dev/null; then
        echo "netshape: ingress on $DEV capped to ${RATE} Mbit/s"
    fi
fi
