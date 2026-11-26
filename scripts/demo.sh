#!/bin/sh
# One-command demo: start the simulated warehouse system, run every floor tool once against it,
# then print the live-state brief that ties them together. Nothing leaves this machine.
set -e
cd "$(dirname "$0")/.."
export HUBOPS_WMS_URL="${HUBOPS_WMS_URL:-http://127.0.0.1:8765}"
export HUBOPS_WMS_TOKEN="${HUBOPS_WMS_TOKEN:-demo}"
mkdir -p data

python3 hubsim/server.py --quiet &
SIM=$!
trap 'kill $SIM 2>/dev/null' EXIT
for i in $(seq 1 30); do
  curl -s -o /dev/null -H "Authorization: Bearer demo" -X POST "$HUBOPS_WMS_URL/api/places" -d '{}' && break
  sleep 1
done

echo "== floor board (what is left to sort, truck by truck)"; python3 hubops/unsorted_board.py --dry-run | tail -5
echo; echo "== hourly sort report (table + picture, not posted)"; python3 hubops/hourly_report.py --dry-run
echo; echo "== hourly floor card";  python3 hubops/floor_now.py && python3 hubops/floor_now.py --say
echo; echo "== stuck chutes";       python3 hubops/chute_alerts.py --any-hour
echo; echo "== lost labels";        python3 hubops/lost_labels.py --any-hour
echo; echo "== feed stations";      python3 hubops/feeder_pace.py --any-hour || true
echo; echo "== floor coach (demo: the snapshot it sends + a canned example answer)"
python3 hubops/floor_coach.py --demo | sed -n '1p;/^# Floor snapshot/,$p'
echo; echo "== loading board";      python3 hubops/dock_board.py --dry-run | head -20
echo; echo "== label station (a scanned package -> its box's label -> the print agent, dry run into data/labels/)"
python3 hubops/label_station.py --demo && python3 agent/label_agent.py --queue data/labels/queue.json --dry-run
echo; echo "== live state";         python3 hubops/live_state.py --check
