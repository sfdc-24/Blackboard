#!/usr/bin/env bash
# Characterize the bus's bad-auth behavior. Usage: bus_auth_probe.sh <bus_url>
URL="$1"

echo "=== wrong secret, try 2 (-m 20) ==="
curl -s -L -m 20 -H 'Content-Type: application/json' \
  -d '{"action":"time","secret":"still-wrong"}' \
  -w '\n[HTTP %{http_code}, %{time_total}s]\n' "$URL"

echo "=== wrong secret, try 3 (-m 20) ==="
curl -s -L -m 20 -H 'Content-Type: application/json' \
  -d '{"action":"time","secret":"still-wrong"}' \
  -w '\n[HTTP %{http_code}, %{time_total}s]\n' "$URL"

echo "=== no secret field at all (-m 20) ==="
curl -s -L -m 20 -H 'Content-Type: application/json' \
  -d '{"action":"time"}' \
  -w '\n[HTTP %{http_code}, %{time_total}s]\n' "$URL"

echo "=== wrong secret, verbose redirect trace (-m 20, headers only) ==="
curl -s -o /dev/null -D - -m 20 -H 'Content-Type: application/json' \
  -d '{"action":"time","secret":"still-wrong"}' \
  -w '[first hop: HTTP %{http_code}, %{time_total}s]\n' "$URL" | grep -iE '^(HTTP|location)|first hop'
