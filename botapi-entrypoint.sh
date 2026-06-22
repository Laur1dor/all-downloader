#!/bin/sh
# Shape container egress so uploads to Telegram cannot saturate the uplink.
# UPLOAD_RATE_MBIT is set in .env; requires cap NET_ADMIN (see compose.yml).
if [ -n "$UPLOAD_RATE_MBIT" ]; then
  if tc qdisc replace dev eth0 root tbf rate "${UPLOAD_RATE_MBIT}mbit" burst 1mbit latency 400ms; then
    echo "Egress shaped to ${UPLOAD_RATE_MBIT} Mbit/s"
  else
    echo "WARNING: failed to apply tc shaping (NET_ADMIN capability missing?)"
  fi
fi
exec /docker-entrypoint.sh "$@"
