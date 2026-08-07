#!/bin/bash
# Every regression we have. Cheap tests first — the retrieval-only and static ones take
# seconds, and a failure there is almost always the cause of a failure in the generation
# tests below.
#
# The generation tests sample at temperature 0.3 and assert on keywords, so an occasional
# single-check failure is noise rather than a regression. Re-run a failing suite once
# before believing it — but twice in a row is real.
cd "$(dirname "$0")"
status=0
for t in test_portable test_postprocess test_traceability test_answerable test_structure test_subjects test_subjects_nonvacuous test_subject_persistence \
         test_roles_audit test_titles test_thin_match test_sreedhar test_abstain \
         test_cross_person \
         test_no_leak \
         test_conflation test_availability test_ingest_guard test_formats; do
  echo "=== $t ==="
  timeout 1800 python3 "$t.py" 2>&1 | tail -14
  [ "${PIPESTATUS[0]}" -ne 0 ] && status=1
done

# The clone-and-use path, on its own throwaway database. Kept out of the loop above because
# it must NOT run against the demo data — the whole point is proving a second deployment
# works without it. Skipped rather than failed when the fixture hasn't been built, so a
# normal run doesn't spend twenty minutes re-ingesting.
if [ -f /tmp/fresh/fresh.db ]; then
  echo "=== test_fresh_install ==="
  SPACEBOT_DB=/tmp/fresh/fresh.db timeout 1800 python3 test_fresh_install.py 2>&1 | tail -16
  [ "${PIPESTATUS[0]}" -ne 0 ] && status=1
else
  echo "=== test_fresh_install (skipped — run tools/make_fresh_fixture.sh first) ==="
fi

echo
echo "$([ $status -eq 0 ] && echo 'ALL SUITES PASSED' || echo 'SOME SUITES FAILED')"
echo
echo "NOTE: this run wrote hundreds of questions and a few edits into the tables the admin"
echo "      Overview reads from. Before demoing, restore honest numbers with:"
echo "        python3 tools/reset_activity.py --yes && python3 tools/demo_traffic.py"
exit $status
