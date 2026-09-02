#!/bin/bash
# Bring up the AmneziaWG interface from data/awg/<AWG_CONFIG>.conf and expose it
# as SOCKS5. The interface is configured by hand rather than with wg-quick: that
# script wants a resolvconf and a writable /etc/resolv.conf a container does not
# have, and everything it would do here is the handful of ip(8) calls below.
set -eu

CONF="${AWG_CONFIG_DIR:-/app/data/awg}/${AWG_CONFIG:-awg0}.conf"
IFACE="${AWG_INTERFACE:-awg0}"
PORT="${AWG_SOCKS_PORT:-1080}"

# Exiting here would crash-loop until a config exists, which is the normal
# state right after deploy: the tunnel is configured from the admin chat.
while [ ! -r "$CONF" ]; do
    echo "awg: waiting for a config at $CONF"
    sleep "${AWG_POLL:-15}"
done

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
microsocks -i 0.0.0.0 -p "$PORT" &
socks_pid=$!

# A replacement config arrives from the admin chat: the bot writes it into
# data/awg/ and drops the marker below. Rebuilding the interface in place would
# mean undoing the routes and the resolver by hand and getting every step right
# on a tunnel that is already half torn down, so the container exits instead and
# docker's restart policy brings it back through this same script from the top.
#
# The bot signals through a file rather than the docker socket, which would hand
# the bot container root on the host.
RELOAD="${AWG_RELOAD_FILE:-/app/data/vpn/reload_awg}"
POLL="${AWG_POLL:-15}"
while kill -0 "$socks_pid" 2>/dev/null; do
    sleep "$POLL"
    if [ -f "$RELOAD" ]; then
        rm -f "$RELOAD"
        echo "awg: new config — restarting"
        kill "$socks_pid" 2>/dev/null || true
        exit 0
    fi
done

wait "$socks_pid"
