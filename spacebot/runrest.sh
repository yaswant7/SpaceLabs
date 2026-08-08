#!/bin/bash
# The tail of runall.sh, for when a long run gets interrupted part way.
cd "$(dirname "$0")"
status=0
for t in test_conflation test_availability test_ingest_guard test_formats; do
  echo "=== $t ==="
  timeout 1800 python3 "$t.py" 2>&1 | tail -14
  [ "${PIPESTATUS[0]}" -ne 0 ] && status=1
done
if [ -f /tmp/fresh/fresh.db ]; then
  echo "=== test_fresh_install ==="
  SPACEBOT_DB=/tmp/fresh/fresh.db timeout 1800 python3 test_fresh_install.py 2>&1 | tail -12
  [ "${PIPESTATUS[0]}" -ne 0 ] && status=1
fi
echo
echo "$([ $status -eq 0 ] && echo 'TAIL SUITES PASSED' || echo 'TAIL SUITES FAILED')"
exit $status
