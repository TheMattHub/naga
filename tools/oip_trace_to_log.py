"""Turn an Open Industry Project (OIP) headless run into a plant log `naga twin --log` replays.

    PY tools/oip_trace_to_log.py TRACE.json OUT.jsonl [--source oip]

TRACE.json is what OIP's own runner prints: `npm run test-project -- <project> --only <test>
--trace all --json` (a "note:" line before the JSON is skipped). Each tag the trace followed
becomes plant-log lines, one per change: the key is the tag's name, the value its value, the
stamp the simulated second it changed at (so a mapping says `"epoch": "1970-01-01T00:00:00+00:00"`
and `"unit": "seconds"`). A tag's first value is written at second 0: for a counter it is the
baseline the twin counts from. The log is data, not OIP code: this script reads JSON only.
"""
import argparse
import json
import sys


def read_trace(path):
    """The `trace` of the one test in an OIP `--json` output (any note before the JSON is skipped)."""
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    tests = json.loads(text[text.index("{"):])["tests"]
    if len(tests) != 1:
        raise SystemExit(f"{path}: expected one test (pick it with --only), found {len(tests)}")
    return tests[0].get("trace", [])


def to_lines(trace, source="oip"):
    """Plant-log lines (dicts) of *trace*, in time order, ties by tag name."""
    rows = sorted((t, tag["tag"], v)
                  for tag in trace
                  for t, v in [(0.0, tag["start"])] + [(c["t"], c["v"]) for c in tag["changes"]])
    # The same two stamps `naga watch --record` writes; OIP has one clock, so they agree.
    return [{"source": source, "key": key, "value": value, "source_time": round(t, 3),
             "received": round(t, 3)} for t, key, value in rows]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("trace", help="the JSON OIP's test-project printed")
    parser.add_argument("out", help="the plant log to write, one JSON object a line")
    parser.add_argument("--source", default="oip", help="the source name a mapping signal may name")
    args = parser.parse_args(argv)
    lines = to_lines(read_trace(args.trace), args.source)
    with open(args.out, "w", encoding="utf-8", newline="\n") as handle:
        for line in lines:
            handle.write(json.dumps(line) + "\n")
    print(f"{len(lines)} lines to {args.out}")


if __name__ == "__main__":
    sys.exit(main())
