#!/bin/bash
# Bring up the AmneziaWG interface from data/awg/<AWG_CONFIG>.conf and expose it
# as SOCKS5. The interface is configured by hand rather than with wg-quick: the
# script wants a resolvconf and a writable /etc/resolv.conf that a container does
# not have, and all it would do here is the three ip(8) calls below.
set -eu

CONF="${AWG_CONFIG_DIR:-/app/data/awg}/${AWG_CONFIG:-awg0}.conf"
IFACE="${AWG_INTERFACE:-awg0}"
PORT="${AWG_SOCKS_PORT:-1080}"

[ -r "$CONF" ] || { echo "awg: no config at $CONF" >&2; exit 1; }

# awg setconf understands the crypto and obfuscation keys only; Address, MTU and
# DNS belong to wg-quick and make it reject the file.
# The config is edited on whatever machine the operator uses, so its line
# endings are not to be trusted: a trailing CR turns "10.8.1.5/32" into an
# address ip(8) refuses.
CLEAN=/tmp/awg-clean.conf
tr -d '' < "$CONF" > "$CLEAN"

SETCONF=/tmp/awg-setconf.conf
grep -viE '^[[:space:]]*(Address|MTU|DNS)[[:space:]]*=' "$CLEAN" > "$SETCONF"

ADDRESS="$(grep -iE '^[[:space:]]*Address' "$CLEAN" | head -1 | cut -d= -f2 | tr -d ' ')"
MTU="$(grep -iE '^[[:space:]]*MTU' "$CLEAN" | head -1 | cut -d= -f2 | tr -d ' ')"
: "${ADDRESS:?awg: the config has no Address}"
: "${MTU:=1280}"

mkdir -p /dev/net
[ -c /dev/net/tun ] || mknod /dev/net/tun c 10 200

export WG_QUICK_USERSPACE_IMPLEMENTATION=amneziawg-go
amneziawg-go "$IFACE"

awg setconf "$IFACE" "$SETCONF"
ip address add "$ADDRESS" dev "$IFACE"
ip link set mtu "$MTU" up dev "$IFACE"
ip route replace default dev "$IFACE"

echo "awg: up on $IFACE ($ADDRESS, mtu $MTU)"
for _ in $(seq 1 20); do
    if awg show "$IFACE" | grep -q 'latest handshake'; then
        echo "awg: handshake established"
        break
    fi
    sleep 1
done
awg show "$IFACE" | sed 's/^/  /'

echo "awg: SOCKS5 on :${PORT}"
exec microsocks -i 0.0.0.0 -p "$PORT"
