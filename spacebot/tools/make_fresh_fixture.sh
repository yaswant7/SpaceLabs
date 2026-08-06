#!/bin/bash
# Build the fixture test_fresh_install.py runs against: a brand-new deployment that has
# never seen the demo corpus.
#
# Separate from runall.sh because it ingests a document through a local model, which takes
# minutes on CPU. Build it once; the test itself is quick.
set -e
cd "$(dirname "$0")/.."

rm -rf /tmp/fresh
mkdir -p /tmp/fresh/docs

cat > /tmp/fresh/docs/bike_parking.txt <<'EOF'
Bike parking at Northwind Freight

Where to park. The covered bike rack is on the north side of the building, next to the
loading bay entrance. It holds around forty bikes and is covered by CCTV.

Access. The rack is inside the gated yard. Your building pass opens the pedestrian gate on
Chandler Street between 6am and 9pm. Outside those hours use the main reception entrance
and ask the night desk to let you through.

Locks. Bring your own lock. The company does not provide them and bikes left unlocked have
been taken. Two D-locks are recommended for anything expensive.

Showers. There are showers and lockers on the ground floor next to the gym. Lockers are
first come first served and are emptied every Friday evening.
EOF

export SPACEBOT_DB=/tmp/fresh/fresh.db
python3 setup.py --org "Northwind Freight" --admin ops@northwind.example \
                 --assistant-name "Wren" --password fixture-only
python3 ingest.py /tmp/fresh/docs --publish --owner ops

echo
echo "fixture ready: $SPACEBOT_DB"
echo "run: SPACEBOT_DB=$SPACEBOT_DB python3 test_fresh_install.py"
