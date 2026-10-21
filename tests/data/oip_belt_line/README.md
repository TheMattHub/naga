# oip_belt_line: a physics-simulated test plant for the twin

Five minutes of a box belt that Open Industry Project's physics simulator ran, recorded as a plant
log the twin replays, with a Naga model of the same belt and the mapping between them. The twin is
tested against it because a physics engine, not our own model, decided when each box reached the
belt's end. Nothing here is OIP code: only the log it produced, written out by `tools/oip_trace_to_log.py`.

| File | What it is |
|---|---|
| `plant.jsonl` | the plant log: 596 lines, one tag change each, stamped in simulated seconds |
| `line.json` | the Naga model: a spawner, a 3.635 m belt at 1 m/s, a sink |
| `line.plantmap.json` | the mapping: two OIP counters become two block counters |
| `record.test.json` | the OIP test spec that drives the recording (ours, not OIP's) |

## Where it comes from

[Open Industry Project](https://www.openindustryproject.org/) (OIP) is an MIT-licensed industrial
simulator that runs in a browser, with Jolt physics and an IEC 61131-3 PLC. Its headless runner
(`npm run test-project`) steps a scene and a PLC program for N simulated seconds and prints every
tag change with its time. This log is that output for a line OIP's own scaffolder builds, at OIP
commit `ce9cd16660688e6964d630ab40b14cc5fa0ff951` (a clone of
`https://github.com/Open-Industry-Project/Open-Industry-Project`). OIP's run is deterministic (fixed
step, seeded random numbers): three runs on one machine, one of them from a fresh scaffold, gave the
same log byte for byte. OIP does not promise the same floats across machines, so a regeneration
elsewhere may differ in the last millisecond; the facts below should not.

The line: a box spawner (30 boxes a minute, each gap drawn between half and one and a half times the
2 s period) drops 0.6 m boxes onto a 4 m belt at 1 m/s; a photo eye sits near the belt's
head; a despawner on the floor past the head counts what it takes. A PLC latch runs the belt and the
spawner from `Start` until `Stop`. The recording presses Start at 1 s, Stop at 150 s and Start
again at 165 s (`record.test.json`), so the log holds a running stretch, a stop and a restart.

## Regenerate it

Needs Node 24 and the OIP clone. Run the OIP commands from the clone, with `WORK` any folder
outside it, and the last from this repository. On Windows give the runner the long form of a path
(`C:/Users/Name/...`): with an 8.3 short name (`NAME~1`) its file watcher stops on a libuv
assertion.

```console
git clone https://github.com/Open-Industry-Project/Open-Industry-Project oip
cd oip && git checkout ce9cd16
npm ci --ignore-scripts
npm run add-part -- WORK/line oip.box-spawner --params boxesPerMinute=30,fixedRate=false,countTag=SpawnCount oip.belt-conveyor --params speed=1 oip.diffuse-sensor oip.despawner --scaffold
cp tests/data/oip_belt_line/record.test.json WORK/line/tests/       # from this repository
npm run test-project --silent -- WORK/line --only record --trace all --json > WORK/trace.json
python tools/oip_trace_to_log.py WORK/trace.json tests/data/oip_belt_line/plant.jsonl   # from this repository
```

`npm ci --ignore-scripts` runs no install script and the runner needs none. The scaffolder names the
tags; the ones in the log are below. OIP also prints "flow facts" (a box dropped, tipped or
stalled): this run has none.

## What the mapping reads

| OIP tag | Type | Meaning | Mapped to |
|---|---|---|---|
| `SpawnCount` | DINT | boxes the spawner has dispensed (its `countTag`) | `counter` on block `Infeed` |
| `SinkCount` | INT | boxes the despawner has taken (its `countTag`) | `counter` on block `Belt`: what left the belt |
| `DischargeEye` | BOOL | the photo eye is blocked | not mapped |
| `Belt1Run`, `SpawnerRun`, `Running` | BOOL | the PLC runs the belt and spawner | not mapped |
| `Start`, `Stop` | BOOL | the operator's inputs | not mapped |

The model has no stop, a belt's contents the plant does not name, and no eye, so those keys stay in
the log and out of the mapping. Two things follow, both stated rather than hidden:

- **Boxes on the belt are unknown to the twin.** A plant that gives counters and no items starts
  every forecast from an empty belt, so the forecast of exits runs about two boxes (the model's own
  mean on the belt) below what the plant counted. The forecast of arrivals has no such offset.
- **The model does not know the line stopped.** A forecast made before the stop runs on while the
  plant counts nothing for about 17 s; the twin shows it as a forecast far above the observation.

## Hand-checked figures

From the log, not from the twin: 144 boxes spawned (the first at 1.05 s) and 143 counted into the
sink (the first at 4.9 s); no count changes between 149.333 s and 165.917 s (spawner) or between
149.017 s and 166.917 s (sink). At 100.5 s the counts are 53 and 50. `tests/test_oip_test_plant.py`
checks all of this and replays the log through the twin.

## Licence of what came from OIP

The log is OIP's output, so it carries OIP's licence. OIP's `LICENSE`, copied:

```text
The MIT License (MIT)

Copyright (c) 2024-present, Automation Standard LLC and Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```
