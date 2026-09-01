#!/bin/bash
# Bring up the AmneziaWG interface from data/awg/<AWG_CONFIG>.conf and expose it
# as SOCKS5. The interface is configured by hand rather than with wg-quick: that
# script wants a resolvconf and a writable /etc/resolv.conf a container does not
# have, and everything it would do here is the handful of ip(8) calls below.
set -eu

CONF="${AWG_CONFIG_DIR:-/app/data/awg}/${AWG_CONFIG:-awg0}.conf"
IFACE="${AWG_INTERFACE:-awg0}"
PORT="${AWG_SOCKS_PORT:-1080}"

[ -r "$CONF" ] || { echo "awg: no config at $CONF" >&2; exit 1; }

# These configs are written on whatever machine the operator uses, and a
# trailing carriage return turns "10.8.1.6/32" into an address ip(8) refuses.
CLEAN=/tmp/awg-clean.conf
tr -d '\015' < "$CONF" > "$CLEAN"

# awg setconf understands the crypto and obfuscation keys only; Address, MTU and
# DNS belong to wg-quick and make it reject the file.
SETCONF=/tmp/awg-setconf.conf
grep -viE '^[[:space:]]*(Address|MTU|DNS)[[:space:]]*=' "$CLEAN" > "$SETCONF"

field() { grep -iE "^[[:space:]]*$1[[:space:]]*=" "$CLEAN" | head -1 | cut -d= -f2- | tr -d ' '; }

ADDRESS="$(field Address)"
MTU="$(field MTU)"
ENDPOINT="$(field Endpoint)"
: "${ADDRESS:?awg: the config has no Address}"
: "${ENDPOINT:?awg: the config has no Endpoint}"
: "${MTU:=1280}"

mkdir -p /dev/net
[ -c /dev/net/tun ] || mknod /dev/net/tun c 10 200

amneziawg-go "$IFACE"
awg setconf "$IFACE" "$SETCONF"
ip address add "$ADDRESS" dev "$IFACE"
ip link set mtu "$MTU" up dev "$IFACE"

# The tunnel's own packets have to keep the original route. Pointing the default
# at the interface without this sends the traffic for the endpoint into the very
# tunnel it is establishing: the handshake still completes, because it happens
# before the route changes, and then nothing moves afterwards.
ENDPOINT_HOST="${ENDPOINT%:*}"
ENDPOINT_IP="$(getent ahostsv4 "$ENDPOINT_HOST" | awk '{print $1; exit}')"
: "${ENDPOINT_IP:?awg: cannot resolve $ENDPOINT_HOST}"
GATEWAY="$(ip route show default | awk '{print $3; exit}')"
UPLINK="$(ip route show default | awk '{print $5; exit}')"
ip route add "$ENDPOINT_IP/32" via "$GATEWAY" dev "$UPLINK"
echo "awg: endpoint $ENDPOINT_IP keeps using $UPLINK via $GATEWAY"

ip route replace default dev "$IFACE"

# Names are resolved through the tunnel from here on, which is half the point of
# having it: the local resolver answers for the wrong country, and for some
# domains does not answer at all.
printf 'nameserver 1.1.1.1\nnameserver 1.0.0.1\n' > /etc/resolv.conf

echo "awg: up on $IFACE ($ADDRESS, mtu $MTU)"
for _ in $(seq 1 20); do
    awg show "$IFACE" | grep -q 'latest handshake' && { echo "awg: handshake established"; break; }
    sleep 1
done
awg show "$IFACE" | grep -E 'endpoint|handshake|transfer' | sed 's/^/  /'

echo "awg: SOCKS5 on :${PORT}"
exec microsocks -i 0.0.0.0 -p "$PORT"
