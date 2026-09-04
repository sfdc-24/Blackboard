#!/usr/bin/env bash
# SFDC24 v1 bus health check. Usage: bus_health.sh <bus_url> <secret>
# Secret comes in as an argument; it is never written to disk.
URL="$1"
SEC="$2"

echo "=== 1. bare GET (doGet) ==="
curl -s -L -m 30 -w '\n[HTTP %{http_code}, %{time_total}s]\n' "$URL" | head -c 600

echo
echo "=== 2. POST action=time (authenticated) ==="
curl -s -L -m 30 -H 'Content-Type: application/json' \
  -d "{\"action\":\"time\",\"secret\":\"$SEC\"}" \
  -w '\n[HTTP %{http_code}, %{time_total}s]\n' "$URL"

echo "=== 3. POST wrong secret (auth enforcement) ==="
curl -s -L -m 30 -H 'Content-Type: application/json' \
  -d '{"action":"time","secret":"wrong-value-on-purpose"}' \
  -w '\n[HTTP %{http_code}, %{time_total}s]\n' "$URL"

echo "=== 4. POST action=read title (round-trip, payload size) ==="
curl -s -L -m 60 -H 'Content-Type: application/json' \
  -d "{\"action\":\"read\",\"title\":\"Blackboard - Alpha DB\",\"secret\":\"$SEC\"}" \
  -o /tmp/board.json -w '[HTTP %{http_code}, %{time_total}s, %{size_download} bytes]\n' "$URL"
python3 - <<'EOF'
import json
try:
    with open('/tmp/board.json') as fh:
        d = json.load(fh)
    rows = d.get('rows') or []
    print(f"read ok={d.get('ok')} title={d.get('title')!r} rows={len(rows)}")
    if rows:
        newest = rows[-1]
        print(f"newest row ts={str(newest[1])[:32]!r} src={str(newest[2])[:24]!r}")
except Exception as exc:
    print(f"read parse FAILED: {exc}")
EOF
rm -f /tmp/board.json
