#!/usr/bin/env bash
# Dashboard smoke tests — fast (<10s), no Python required.
#
# Usage:
#     bash tests/smoke.sh
#     BASE=http://localhost:8015 bash tests/smoke.sh
#
# Each check prints PASS/FAIL + timing. Exits non-zero on any failure.

set -u
BASE="${BASE:-https://gdeltmonitor.com}"
FAILS=0
TOTAL=0

check() {
  local name="$1" url="$2" assertion="$3"
  TOTAL=$((TOTAL + 1))
  local t0=$(date +%s.%N)
  local body
  body=$(curl -sS --max-time 10 "$BASE$url" 2>&1)
  local rc=$?
  local t1=$(date +%s.%N)
  local dt
  dt=$(awk "BEGIN{printf \"%.2f\", $t1 - $t0}")
  if [ $rc -ne 0 ]; then
    echo "  [FAIL ${dt}s]  $name   (curl exit $rc)"
    FAILS=$((FAILS + 1))
    return
  fi
  local ok
  ok=$(echo "$body" | python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    print('1' if ($assertion) else '0')
except Exception as e:
    print('0')
" 2>/dev/null)
  if [ "$ok" = "1" ]; then
    echo "  [PASS ${dt}s]  $name"
  else
    echo "  [FAIL ${dt}s]  $name"
    echo "    body head: $(echo "$body" | head -c 200)"
    FAILS=$((FAILS + 1))
  fi
}

echo "=== endpoints ==="
check "stats ok"                  "/api/stats"                                                             "d.get('total_articles', 0) > 0"
check "stats per-source"          "/api/stats"                                                             "d.get('gal_articles', 0) > 0 and d.get('gkg_articles', 0) > 0"
check "views has fda"             "/api/views"                                                             "any(v['id'] == 'fda-medical-devices' for v in d['views'])"
check "views default_hours"       "/api/views"                                                             "next(v for v in d['views'] if v['id']=='fda-medical-devices').get('default_hours') == 24"
check "gal_facets has langs"      "/api/gal_facets"                                                        "len(d['languages']) >= 10"
check "gal_facets has outlets"    "/api/gal_facets"                                                        "len(d['top_outlets']) >= 20"

echo ""
echo "=== source=gal ==="
check "gal 1h"                    "/api/articles?source=gal&hours=1"                                       "isinstance(d.get('articles'), list) and isinstance(d.get('total'), int)"
check "gal language=en"           "/api/articles?source=gal&hours=1&language=en"                           "d['total'] > 0"
check "gal q=apple 24h"           "/api/articles?source=gal&hours=24&q=apple"                              "d['total'] >= 0"
check "gal title=recall week"     "/api/articles?source=gal&hours=168&title=recall"                        "d['total'] > 0"
check "gal domain=reuters"        "/api/articles?source=gal&hours=24&domain=reuters"                       "d['total'] >= 0"
check "gal fda view 3d"           "/api/articles?source=gal&view=fda-medical-devices&hours=72"             "isinstance(d.get('articles'), list)"

echo ""
echo "=== source=gkg ==="
check "gkg 1h"                    "/api/articles?source=gkg&hours=1"                                       "isinstance(d.get('articles'), list) and isinstance(d.get('total'), int)"
check "gkg person=Biden 24h"      "/api/articles?source=gkg&hours=24&person=Biden"                         "d['total'] > 0"
check "gkg org=Microsoft 24h"     "/api/articles?source=gkg&hours=24&org=Microsoft"                        "d['total'] > 0"
check "gkg fda view 3d"           "/api/articles?source=gkg&view=fda-medical-devices&hours=72"             "isinstance(d.get('articles'), list)"

echo ""
echo "=== source=all ==="
check "all 1h"                    "/api/articles?source=all&hours=1"                                       "isinstance(d.get('articles'), list) and isinstance(d.get('total'), int)"
check "all fda view 3d"           "/api/articles?source=all&view=fda-medical-devices&hours=72"             "d['total'] > 0"

echo ""
echo "=== regressions ==="
check "nonsense returns empty"    "/api/articles?source=gal&hours=24&q=xyzzy12345neverreal"                "d['total'] == 0 and isinstance(d.get('articles'), list)"
check "unknown source falls back" "/api/articles?source=bogus&hours=1"                                     "isinstance(d.get('articles'), list) and isinstance(d.get('total'), int)"

echo ""
if [ $FAILS -eq 0 ]; then
  echo "All $TOTAL checks passed."
  exit 0
else
  echo "$FAILS of $TOTAL checks FAILED."
  exit 1
fi
