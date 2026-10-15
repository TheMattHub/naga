#!/usr/bin/env bash
# The twin against Mosquitto and the Sparkplug B edge node, for SECONDS (default 3600):
#     bash tests/realserver/twin/run.sh OUT_DIR [SECONDS]
# Run from the repository root. Starts, in order: the broker (127.0.0.1:18830), a
# mosquitto_sub that logs every proposal, the twin (it must subscribe before the edge node's
# births: the reader never asks for a rebirth), then the edge node. Halfway it answers the
# latest proposal "rejected" with mosquitto_pub. Every process is killed when it ends.
set -u
OUT=$1
SECONDS_TOTAL=${2:-3600}
HERE=tests/realserver/twin
MOSQ=${NAGA_MOSQUITTO_DIR:-"/c/Program Files/mosquitto"}
PY=${PY:-python}        # an interpreter with nagasim's dependencies; set PY to choose another
export PYTHONPATH=$PWD PYTHONIOENCODING=utf-8
mkdir -p "$OUT"
pids=()
trap 'kill "${pids[@]}" 2>/dev/null; wait 2>/dev/null' EXIT

"$MOSQ/mosquitto.exe" -c $HERE/mosquitto.conf > "$OUT/broker.log" 2>&1 & pids+=($!)
sleep 2
"$MOSQ/mosquitto_sub.exe" -h 127.0.0.1 -p 18830 -t 'naga/line1/proposals/#' -v \
    > "$OUT/proposals.log" 2>&1 & pids+=($!)
"$PY" -m nagasim twin run $HERE/line.json --map $HERE/line.plantmap.json \
    --connections $HERE/connections.json --out "$OUT/twin_out" --monitor \
    --horizon 300 --replications 50 --every 5s --forecast-every 2m \
    --candidates $HERE/candidates.json --outbox mqtt:outbox > "$OUT/twin.log" 2>&1 & pids+=($!)
for _ in $(seq 120); do [ -f "$OUT/twin_out/twin.json" ] && break; sleep 1; done
[ -f "$OUT/twin_out/twin.json" ] || { echo "the twin did not start; see $OUT/twin.log"; exit 1; }
"$PY" tests/realserver/sparkplug_sim.py --port 18830 line > "$OUT/sim.log" 2>&1 & pids+=($!)

sleep $((SECONDS_TOTAL / 2))
# The latest proposal: an earlier one has expired by now, and a reply to it is counted late.
first=$(grep -o 'proposals/[0-9a-f]\{64\}' "$OUT/proposals.log" | tail -n 1 | cut -d/ -f2)
if [ -n "$first" ]; then
    "$MOSQ/mosquitto_pub.exe" -h 127.0.0.1 -p 18830 -t naga/line1/replies -q 1 \
        -m "{\"id\": \"$first\", \"decision\": \"rejected\", \"by\": \"realserver run\"}"
    echo "replied rejected to $first" > "$OUT/reply.log"
fi
sleep $((SECONDS_TOTAL - SECONDS_TOTAL / 2))
"$PY" -m nagasim twin status "$OUT/twin_out" > "$OUT/status.txt" 2>&1
