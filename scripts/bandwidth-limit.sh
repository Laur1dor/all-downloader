#!/bin/sh
# Cap total throughput in both directions on the LXC interface. The rate is read
# from a file the bot writes (admin control panel), defaulting to 100 Mbit/s, so
# the admin can change it live. A systemd timer re-runs this every ~20s.
IFACE="${SHAPE_IFACE:-eth0}"
RATE_FILE="${RATE_FILE:-/root/tiktok-bot/data/bandwidth_mbit.txt}"

RATE_MBIT=100
if [ -r "$RATE_FILE" ]; then
    v=$(tr -dc '0-9' < "$RATE_FILE")
    [ -n "$v" ] && RATE_MBIT="$v"
fi

# 0 means "unlimited" — remove shaping.
if [ "$RATE_MBIT" -eq 0 ] 2>/dev/null; then
    tc qdisc del dev "$IFACE" root 2>/dev/null || true
    tc qdisc del dev "$IFACE" ingress 2>/dev/null || true
    exit 0
fi

RATE="${RATE_MBIT}mbit"
tc qdisc replace dev "$IFACE" root tbf rate "$RATE" burst 1mbit latency 400ms

ip link add ifb0 type ifb 2>/dev/null || true
ip link set ifb0 up
tc qdisc del dev "$IFACE" ingress 2>/dev/null || true
tc qdisc add dev "$IFACE" handle ffff: ingress
tc filter add dev "$IFACE" parent ffff: protocol all u32 match u32 0 0 \
    action mirred egress redirect dev ifb0
tc qdisc replace dev ifb0 root tbf rate "$RATE" burst 1mbit latency 400ms
