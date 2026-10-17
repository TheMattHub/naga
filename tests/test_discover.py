"""nagasim.discover (D546-D559): an event log to a draft model.

The readers on small hand-made logs (CSV, Disco's per-instance shape, complete-only, XES),
hostile logs (over a ceiling, malformed, billion laughs, an external entity, odd names), the
CLI's output (validate, fmt, export --python), and the round trip: a bundled model's run ->
its log -> a draft -> the draft's run -> its log, compared on `log_kpis`.
"""
import json
import re
from pathlib import Path

import pytest

import nagasim
from nagasim import discover as d
from nagasim.api import run_model
from nagasim.build import export_python
from nagasim.core import schema
from nagasim.core.trace import TraceRecorder
from nagasim.modelfmt import dumps, minimal

pytestmark = pytest.mark.filterwarnings("ignore")
MODELS = Path(nagasim.__file__).parent / "models"
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")


def _write(tmp_path, text, name="log.csv"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="")
    return path


def _two_step_csv(n=40):
    """Cases through 'Cut' then 'Pack', start/complete, one machine each, minutes apart."""
    rows = ["case,activity,timestamp,lifecycle,resource"]
    for i in range(n):
        t = 10.0 * i
        rows += [f"c{i},Cut,{t},start,M1", f"c{i},Cut,{t + 3 + i % 3},complete,M1",
                 f"c{i},Pack,{t + 7},start,P1", f"c{i},Pack,{t + 9},complete,P1"]
    return "\n".join(rows) + "\n"


def _errors(layout):
    return [p for p in schema.validate_model_structured(schema.migrate(layout))
            if p.severity == "error"]


# ---------------------------------------------------------------- readers

def test_csv_start_complete_gives_service_wait_and_resources(tmp_path):
    layout, report = d.discover(_write(tmp_path, _two_step_csv()))
    assert not _errors(layout)
    acts = {a["block"]: a for a in report["activities"]}
    assert acts["Cut"]["service"]["mean"] == pytest.approx(4.0, rel=0.01)
    assert acts["Pack"]["mean_wait"] == pytest.approx(7 - 4.0, rel=0.01)   # 7 - (3..5)
    assert {r["name"]: r["capacity"] for r in report["resources"]} == {"M1": 1, "P1": 1}
    assert report["routing"]["mode"] == "markov"
    blocks = {b["name"]: b for b in layout["blocks"]}
    assert blocks["Cut"]["params"]["resource_name"] == "M1"
    assert layout == minimal(layout)


def test_disco_shape_one_row_per_instance(tmp_path):
    text = "Case ID;Activity;Start;End\n" + "".join(
        f"o{i};Check;2026-10-16T08:{i:02d}:00;2026-10-16T08:{i:02d}:30\n" for i in range(30))
    _layout, report = d.discover(_write(tmp_path, text), time_unit="seconds")
    (act,) = report["activities"]
    assert act["service"]["mean"] == pytest.approx(30.0)
    assert report["source"]["timestamps"] == "date"
    assert report["observed"]["cases"] == 30


def test_complete_only_is_a_delay_of_the_sojourn(tmp_path):
    text = "case,activity,timestamp\n" + "".join(
        f"c{i},Open,{5 * i}\nc{i},Close,{5 * i + 2 + (i % 2)}\n" for i in range(20))
    layout, report = d.discover(_write(tmp_path, text))
    types = {b["name"]: b["type"] for b in layout["blocks"]}
    assert types["Open"] == types["Close"] == "DELAY"
    close = next(a for a in report["activities"] if a["block"] == "Close")
    assert close["lifecycle"] == "complete only"
    assert close["sojourn"]["mean"] == pytest.approx(2.5)
    assert any("complete-only" in a for a in report["assumptions"])
    assert not layout.get("resources")


def test_xes_reads_traces_events_and_lifecycle(tmp_path):
    events = "".join(
        f'<trace><string key="concept:name" value="t{i}"/>'
        f'<event><string key="concept:name" value="Weld"/><string key="org:resource" value="R"/>'
        f'<string key="lifecycle:transition" value="start"/>'
        f'<date key="time:timestamp" value="2026-01-01T00:{i:02d}:00+01:00"/></event>'
        f'<event><string key="concept:name" value="Weld"/><string key="org:resource" value="R"/>'
        f'<string key="lifecycle:transition" value="complete"/>'
        f'<date key="time:timestamp" value="2026-01-01T00:{i:02d}:20+01:00"/></event></trace>'
        for i in range(10))
    path = _write(tmp_path, f'<?xml version="1.0" encoding="UTF-8"?><log xes.version="1.0">'
                            f'{events}</log>', "log.xes")
    info = {}
    evs = d.read_log(path, time_unit="seconds", info=info)
    assert info["format"] == "xes" and len(evs) == 20
    _layout, report = d.discover(path, time_unit="seconds")
    assert report["activities"][0]["service"]["mean"] == pytest.approx(20.0)


def test_suspended_time_is_taken_out_of_the_service_time(tmp_path):
    """D631: start 0, suspend 2, resume 10, complete 13: service 5, 8 suspended, reported;
    unmatched and out-of-order pairs are counted, never a crash; an abort ends its instance,
    dropped and counted; an unknown lifecycle row is dropped."""
    rows = ["case,activity,timestamp,lifecycle,resource"]
    for i in range(30):
        t = 20.0 * i
        rows += [f"c{i},Call,{t},start,Ann", f"c{i},Call,{t + 2},suspend,Ann",
                 f"c{i},Call,{t + 10},resume,Ann", f"c{i},Call,{t + 13},complete,Ann"]
    rows += ["x1,Call,700,resume,Ann", "x1,Call,701,start,Ann", "x1,Call,702,complete,Ann",
             "x2,Call,710,suspend,Ann",                              # nothing running
             "x3,Call,720,start,Ann", "x3,Call,721,suspend,Ann", "x3,Call,725,complete,Ann",
             "x4,Call,730,start,Ann", "x4,Call,731,ate_abort,Ann",      # ends x4's Call
             "x5,Call,740,start,Ann", "x5,Call,741,reassign,Ann", "x5,Call,742,complete,Ann"]
    layout, report = d.discover(_write(tmp_path, "\n".join(rows) + "\n"))
    assert not _errors(layout)
    (call,) = report["activities"]
    assert call["service"]["n"] == 33
    assert call["suspended"] == {"instances": 30, "mean": pytest.approx(8.0)}
    assert d.log_kpis(d.read_log(tmp_path / "log.csv"))["activities"]["Call"]["service"] == \
        pytest.approx((30 * 5 + 1 + 5 + 2) / 33)        # x3's suspend is never resumed: kept
    assert report["dropped"].items() >= {
        "resume with no suspended instance (ignored)": 1,
        "suspend with no running instance (ignored)": 1,
        "suspend never resumed (its time kept in the service)": 1,
        "activity instance aborted or withdrawn": 1,
        "lifecycle not one that is read": 1}.items()
    codes = {f["code"]: f for f in report["check"]["findings"]}
    assert codes["suspended"]["level"] == "ok" and "30 activity instances" in codes["suspended"]["what"]
    assert any("suspended time" in a for a in report["assumptions"])
    assert report["resources"][0]["utilisation"] == pytest.approx((30 * 5 + 8) / 742, rel=0.01)


def test_suspend_rows_keep_the_ceilings(tmp_path, monkeypatch):
    """A suspend/resume flood is rows like any other: the event ceiling still refuses it."""
    rows = ["case,activity,timestamp,lifecycle"] + [
        f"c,A,{k},{('suspend', 'resume')[k % 2]}" for k in range(80)]
    path = _write(tmp_path, "\n".join(rows) + "\n")
    monkeypatch.setattr(d, "MAX_EVENTS", 50)
    with pytest.raises(d.LogError, match="50 events"):
        d.read_log(path)
    monkeypatch.setattr(d, "MAX_EVENTS", 10**6)
    with pytest.raises(d.LogError, match="no completed"):
        d.log_kpis(d.read_log(path))


def test_a_large_sample_keeps_its_own_mean():
    """Past FIT_SAMPLE values the family is picked on a sample but fitted on all of them:
    the sample's mean alone moved a round trip's throughput by 2%."""
    import numpy as np
    from nagasim.core.distributions import distribution_mean
    values = np.random.default_rng(3).exponential(1.0, 20_000)
    fit = d._fit(values)
    assert distribution_mean(fit["distribution"], fit["dist_params"]) == pytest.approx(
        values.mean(), rel=0.002)


def test_a_resource_named_like_an_activity_stays_its_own():
    events = []
    for i in range(10):
        t = 10.0 * i
        events += [d.Event(f"c{i}", "Server", t, "start"), d.Event(f"c{i}", "Server", t + 1, "complete"),
                   d.Event(f"c{i}", "Pack", t + 2, "start", "Server"),
                   d.Event(f"c{i}", "Pack", t + 3, "complete", "Server")]
    layout, _report = d.discover(events)
    seized = {b["name"]: b["params"]["resource_name"] for b in layout["blocks"]
              if b["type"] == "PROCESS"}
    assert seized["Server"] != seized["Pack"]


# ---------------------------------------------------------------- hostile logs

def test_ceilings_refuse_before_building(tmp_path, monkeypatch):
    path = _write(tmp_path, _two_step_csv())
    monkeypatch.setattr(d, "MAX_BYTES", 100)
    with pytest.raises(d.LogError, match="ceiling"):
        d.read_log(path)
    monkeypatch.setattr(d, "MAX_BYTES", 10**9)
    monkeypatch.setattr(d, "MAX_EVENTS", 50)
    with pytest.raises(d.LogError, match="50 events"):
        d.read_log(path)
    monkeypatch.setattr(d, "MAX_EVENTS", 10**6)
    monkeypatch.setattr(d, "MAX_ACTIVITIES", 1)
    with pytest.raises(d.LogError, match="activities"):
        d.discover(path)


@pytest.mark.parametrize("text, match", [
    ("", "empty"),
    ("who,what\n1,2\n", "no case column"),
    ("case,activity\n1,A\n", "no timestamp column"),
    ("case,activity,timestamp\n1,A,yesterday\n2,A,not a time\n", "no usable event"),
    ("case,activity,timestamp\n1,A," + "9" * 200_000 + "\n", "no usable event"),
    ("case,activity,timestamp\n1,A,2026-01-01T00:00:00Z\n2,A,2026-01-01T00:01:00\n", "offset"),
], ids=["empty", "no-case", "no-time", "bad-times", "huge-cell", "mixed-offsets"])
def test_malformed_csv_is_refused_with_a_reason(tmp_path, text, match):
    with pytest.raises(d.LogError, match=match):
        d.discover(_write(tmp_path, text))


BILLION_LAUGHS = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
                  '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
                  '<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>'
                  '<log><trace><string key="concept:name" value="&lol3;"/></trace></log>')
XXE = ('<?xml version="1.0"?><!DOCTYPE log [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
       '<log><trace><string key="concept:name" value="&x;"/></trace></log>')


@pytest.mark.parametrize("text, match", [
    (BILLION_LAUGHS, "DOCTYPE"),
    (XXE, "DOCTYPE"),
    ("<log><trace><event></trace></log>", "well-formed"),
    ("<log>" + "<trace>" * 10, "well-formed"),
], ids=["billion-laughs", "xxe", "bad-nesting", "unclosed"])
def test_hostile_xes_is_refused_before_any_expansion(tmp_path, text, match):
    with pytest.raises(d.LogError, match=match):
        d.read_log(_write(tmp_path, text, "log.xes"))


def test_xes_doctype_hidden_in_utf16_is_refused(tmp_path):
    path = tmp_path / "log.xes"
    path.write_bytes(XXE.encode("utf-16"))
    with pytest.raises(d.LogError, match="UTF-8"):
        d.read_log(path)


def test_the_parser_refuses_a_doctype_past_the_text_check(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "_DOCTYPE_RE", re.compile("(?!x)x"))     # matches nothing
    with pytest.raises(d.LogError, match="DOCTYPE"):
        d.read_log(_write(tmp_path, XXE, "log.xes"))


ODD = ["it's \"quoted\"", "../../etc/passwd", "__import__('os').system('x')",
       "NQ(\"Server\") + 1", "Caf" + chr(0xE9) + " " + chr(0x5DE5) + chr(0x7A0B), "1st step",
       "x" * 900, "Cut", "Cut!"]


def test_odd_names_never_become_code_or_paths(tmp_path):
    rows = ["case,activity,timestamp,lifecycle,resource"]
    for i in range(30):
        t = 100.0 * i
        for k, name in enumerate(ODD):
            rows += [f'c{i},"{name.replace(chr(34), chr(34) * 2)}",{t + 5 * k},start,"{name[::-1]}"',
                     f'c{i},"{name.replace(chr(34), chr(34) * 2)}",{t + 5 * k + 2},complete,'
                     f'"{name[::-1]}"']
    rows.append("c99,Cut," + "y" * 1001 + ",start,M")        # an over-long cell: dropped
    layout, report = d.discover(_write(tmp_path, "\n".join(rows) + "\n"))
    assert not _errors(layout)
    blocks = [b["name"] for b in layout["blocks"]]
    resources = [r["name"] for r in layout["resources"]]
    assert all(_NAME.match(n) for n in blocks + resources), blocks + resources
    assert len(set(blocks)) == len(blocks) and len(set(resources)) == len(resources)
    assert {"Cut", "Cut_2"} <= set(blocks)     # same folded name: made unique
    for b in layout["blocks"]:          # the original name is display text only
        label = b.pop("description", "")
        assert len(label) <= 80 and label.isascii()
    text = json.dumps(layout)
    assert "/passwd" not in text and "__import__(" not in text and "NQ(" not in text
    assert report["dropped"]["a cell over 1000 characters"] == 1


# ---------------------------------------------------------------- check and next steps

SAMPLE = d.SAMPLES / "job_shop_log.csv"


def test_the_sample_log_checks_usable_and_says_what_it_lacks():
    check = d.check_log(SAMPLE)
    assert check["verdict"] == "usable with care"
    assert set(check["columns"]) == {"case", "activity", "timestamp", "lifecycle", "resource"}
    assert check["first"] == "2026-10-05T06:00:00"                 # as written, no offset
    codes = {f["code"]: f["level"] for f in check["findings"]}
    assert codes.get("no_schedule") == "warn" and "complete_only" not in codes
    _layout, report = d.discover(SAMPLE)
    assert report["check"] == check
    steps = " ".join(report["next_steps"]["check"])
    assert "queue before the first activity" in steps and "real capacity" in steps
    # The tutorial quotes the check's own line: it must stay the one the check prints.
    # The page lives under packages/engine/public/docs here and under docs/ in the export.
    root = Path(nagasim.__file__).parents[1]
    tutorial = next(p for p in (root / "packages" / "engine" / "public" / "docs", root / "docs")
                    if (p / "event-log-tutorial.md").is_file())
    tutorial = (tutorial / "event-log-tutorial.md").read_text(encoding="utf-8")
    quoted = [line for line in tutorial.splitlines() if line.startswith("  [warn]")]
    assert quoted and set(quoted) <= set(d.check_text(check, "minutes").splitlines())


def test_a_complete_only_working_hours_log_is_weak_and_says_why(tmp_path):
    """Dated, complete-only, arrivals in office hours on weekdays, no resource: the check
    names each gap and the next steps follow from them."""
    from datetime import datetime, timedelta
    rows, start = ["case,activity,timestamp"], datetime(2026, 9, 7, 8, 0)   # a Monday
    i = 0
    for day in range(21):
        if (start + timedelta(days=day)).weekday() >= 5:
            continue
        for k in range(16):                                 # 08:00 to about 16:00
            t = start + timedelta(days=day, minutes=30 * k)
            rows += [f"c{i},Register,{t.isoformat()}",
                     f"c{i},Decide,{(t + timedelta(minutes=20 + k % 4)).isoformat()}"]
            i += 1
    check = d.check_log(_write(tmp_path, "\n".join(rows) + "\n"))
    codes = {f["code"] for f in check["findings"]}
    assert check["verdict"] == "weak"
    assert {"complete_only", "arrival_hours", "shifts", "weekly"} <= codes
    assert "no_resource" not in codes                       # only said when there are starts
    steps = d._next_steps(check, "markov")
    assert any("shift pattern" in s for s in steps["check"])
    assert steps["never"][0].startswith("shifts: the log shows when work happened")


def test_a_long_wait_at_an_idle_resource_is_flagged_not_a_queue():
    """As in an office log (BPI 2012): work scheduled, picked up half an hour later by a
    clerk busy a minute an hour. No queue in the draft makes that wait."""
    events = [e for i in range(60) for e in (
        d.Event(f"c{i}", "Review", 60.0 * i, "ready", "Ann"),
        d.Event(f"c{i}", "Review", 60.0 * i + 30, "start", "Ann"),
        d.Event(f"c{i}", "Review", 60.0 * i + 31, "complete", "Ann"))]
    _layout, report = d.discover(events)
    assert "idle_waits" in {f["code"] for f in report["check"]["findings"]}
    assert any("no queue in the draft" in s for s in report["next_steps"]["check"])
    assert "idle_waits" not in {f["code"] for f in d.check_log(SAMPLE)["findings"]}


def _idle_office_events():
    """Review waits 30 minutes at Ann, busy a minute an hour; Pack follows at once at Bob."""
    return [e for i in range(60) for e in (
        d.Event(f"c{i}", "Review", 60.0 * i, "ready", "Ann"),
        d.Event(f"c{i}", "Review", 60.0 * i + 25 + i % 11, "start", "Ann"),
        d.Event(f"c{i}", "Review", 60.0 * i + 36 + i % 11, "complete", "Ann"),
        d.Event(f"c{i}", "Pack", 60.0 * i + 36 + i % 11, "start", "Bob"),
        d.Event(f"c{i}", "Pack", 60.0 * i + 38 + i % 11, "complete", "Bob"))]


def test_idle_waits_option_models_a_delay_before_the_flagged_activity():
    """D630: on, the flagged activity gets a fitted DELAY before it, every link into it
    goes through the DELAY and the report names it; off (the default), nothing changes."""
    events = _idle_office_events()
    plain = d.discover(events)
    assert d.discover(events, idle_waits=False) == plain
    assert "idle_waits" not in plain[1]
    layout, report = d.discover(events, idle_waits=True)
    assert not _errors(layout)
    (wait,) = report["idle_waits"]
    assert (wait["block"], wait["delay"]) == ("Review", "idle_wait_Review")
    assert wait["wait"]["mean"] == pytest.approx(30.0, rel=0.01)
    blocks = {b["name"]: b for b in layout["blocks"]}
    assert blocks["idle_wait_Review"]["type"] == "DELAY"
    assert "not a queue" in blocks["idle_wait_Review"]["description"]
    into = [l["source"] for l in layout["links"] if l["target"] == "Review"]
    assert into == ["idle_wait_Review"]
    assert any("idle_wait_" in s for s in report["next_steps"]["check"])
    assert layout == minimal(layout)
    # The draft's run gives the wait back: arrival to the Review start, about 30.
    rec = TraceRecorder()
    run_model(layout, replications=1, seed=1, trace=rec, run_length=3600, warmup=0)
    made = {e.entity_id: e.t for e in rec.events if e.kind == "entity_created"}
    starts = [e.t - made[e.entity_id] for e in rec.events
              if e.kind == "entity_seized" and e.block == "Review"]
    assert sum(starts) / len(starts) == pytest.approx(30.0, rel=0.15)


@pytest.mark.parametrize("routing", ["markov", "variants"])
def test_idle_wait_delay_sits_on_every_path_into_its_activity(routing):
    """After Intake, half the cases go to Review (a long wait at an idle clerk), half to
    Pack: the DECIDE branch (markov) or the STATION (variants) into Review goes through
    the DELAY, and nothing else reaches Review."""
    events = []
    for i in range(80):
        t, c = 60.0 * i, f"c{i}"
        nxt = ("Review", "Ann", 30.0) if i % 2 else ("Pack", "Bob", 0.0)
        events += [d.Event(c, "Intake", t, "start", "Bob"),
                   d.Event(c, "Intake", t + 2, "complete", "Bob"),
                   d.Event(c, nxt[0], t + 2 + nxt[2] + i % 5, "start", nxt[1]),
                   d.Event(c, nxt[0], t + 4 + nxt[2] + i % 5, "complete", nxt[1])]
    layout, report = d.discover(events, routing=routing, idle_waits=True)
    assert not _errors(layout)
    assert [w["block"] for w in report["idle_waits"]] == ["Review"]
    types = {b["name"]: b["type"] for b in layout["blocks"]}
    into_wait = [l["source"] for l in layout["links"] if l["target"] == "idle_wait_Review"]
    assert [l["source"] for l in layout["links"] if l["target"] == "Review"] == \
        ["idle_wait_Review"]
    assert [types[s] for s in into_wait] == (["DECIDE"] if routing == "markov" else ["STATION"])


def test_idle_waits_cli_and_tool_flag(tmp_path, capsys):
    from nagasim import agent_tools as T
    rows = ["case,activity,timestamp,lifecycle,resource"] + [
        f"{e.case},{e.activity},{e.time},{ {'ready': 'schedule'}.get(e.lifecycle, e.lifecycle)},"
        f"{e.resource}" for e in _idle_office_events()]
    log = _write(tmp_path, "\n".join(rows) + "\n")
    out = tmp_path / "m.json"
    assert d.main([str(log), "-o", str(out), "--idle-waits"]) == 0
    assert "idle wait: idle_wait_Review before Review" in capsys.readouterr().out
    assert "idle_wait_Review" in out.read_text(encoding="utf-8")
    T.use_workspace(T.Workspace(tmp_path))
    assert "idle_waits" not in T.call("import_event_log", path="log.csv", name="off")
    on = T.call("import_event_log", path="log.csv", name="on", idle_waits=True)
    assert on["idle_waits"] == [{"block": "Review", "delay": "idle_wait_Review"}]


def _office_log(running: bool):
    """A case a minute through five steps of about 20 minutes each, 100 cases in progress:
    begun empty, or with the cases already running at its start (cut at time 0, as a log
    exported from a running system is)."""
    import numpy as np
    rng = np.random.default_rng(5)
    events = []
    for i in range(1500):
        t = float(i - 600 if running else i)
        for k, took in enumerate(rng.exponential(20.0, 5)):
            for when, lc in ((t, "start"), (t + took, "complete")):
                if when >= 0:
                    events.append(d.Event(f"c{i}", f"Step{k}", when, lc, ""))
            t += took
    return events


def test_a_log_that_starts_empty_is_flagged_as_a_ramp_up():
    """D632: the check flags a start from empty only when it can tell (30 cases arriving in
    the first mean cycle time); the sample and the round trips' short logs never trip it."""
    empty = {f["code"]: f for f in d.check_log(_office_log(False))["findings"]}
    assert "ramp_up" in empty and "starts empty" in empty["ramp_up"]["what"]
    assert d.discover(_office_log(False))[1]["next_steps"]["check"].count(d._ACTIONS["ramp_up"]) == 1
    assert "ramp_up" not in {f["code"] for f in d.check_log(_office_log(True))["findings"]}
    assert "ramp_up" not in {f["code"] for f in d.check_log(SAMPLE)["findings"]}
    for model in ("job_shop_jackson.json", "job_shop_routed.json", "single_server.json"):
        rec = TraceRecorder(max_events=10**8)
        run_model(str(MODELS / model), replications=1, seed=1, trace=rec, run_length=3000,
                  warmup=0)
        assert "ramp_up" not in {f["code"] for f in
                                 d.check_log(d.log_from_trace(rec))["findings"]}, model


def test_cli_check_writes_no_model_and_names_a_bundled_sample(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert d.main(["job_shop_log", "--check", "--report", "c.json"]) == 0
    assert capsys.readouterr().out.startswith("check: usable with care")
    assert json.loads((tmp_path / "c.json").read_text(encoding="utf-8"))["cases"] > 400
    assert sorted(p.name for p in tmp_path.iterdir()) == ["c.json"]
    # A path that exists wins; a name is looked up only as a plain name.
    (tmp_path / "job_shop_log").write_text("x", encoding="utf-8")
    assert d.sample_path("job_shop_log") == "job_shop_log"
    assert d.sample_path("../job_shop_log") == "../job_shop_log"
    assert d.sample_path(str(tmp_path / "none")) == str(tmp_path / "none")


# ---------------------------------------------------------------- output

def test_cli_output_validates_formats_and_exports(tmp_path, capsys):
    log, out, rep = _write(tmp_path, _two_step_csv()), tmp_path / "m.json", tmp_path / "r.json"
    assert d.main([str(log), "-o", str(out), "--report", str(rep)]) == 0
    text = out.read_text(encoding="utf-8")
    layout = json.loads(text)
    assert text == dumps(minimal(layout))                      # naga fmt --check
    assert not _errors(layout)                                 # naga validate
    namespace = {}
    exec(compile(export_python(layout), "<export>", "exec"), namespace)
    assert namespace["m"].to_dict() == minimal(layout)        # naga export --python
    report = json.loads(rep.read_text(encoding="utf-8"))
    assert report["observed"]["cases"] == 40
    assert "routing: markov" in capsys.readouterr().out


def test_cli_refusal_is_one_line(tmp_path, capsys):
    from nagasim.cli import main
    assert main(["discover", str(_write(tmp_path, BILLION_LAUGHS, "x.xes"))]) == 2
    assert "DOCTYPE" in capsys.readouterr().err


def test_agent_tool_opens_the_draft_and_stays_in_the_workspace(tmp_path):
    from nagasim import agent_tools as T
    T.use_workspace(T.Workspace(tmp_path))
    _write(tmp_path, _two_step_csv())
    result = T.call("import_event_log", path="log.csv")
    assert "error" not in result, result
    assert result["model"] == "log" and result["errors"] == 0
    assert len(json.dumps(result)) < 20_000
    assert "error" not in T.call("run_model", model="log", replications=1)
    (tmp_path.parent / "outside.csv").write_text(_two_step_csv(), encoding="utf-8")
    assert "error" in T.call("import_event_log", path="../outside.csv")
    _write(tmp_path, XXE, "bad.xes")
    assert T.call("import_event_log", path="bad.xes")["error"]["code"] == "E_DISCOVER_LOG"


# ---------------------------------------------------------------- round trip

def _round_trip(model, length, *, ready=True):
    rec = TraceRecorder(max_events=10**8)
    run_model(str(MODELS / model), replications=1, seed=1, trace=rec, run_length=length,
              warmup=0)
    log = d.log_from_trace(rec, ready=ready)
    layout, report = d.discover(log)
    assert not _errors(layout)
    rec2 = TraceRecorder(max_events=10**8)
    run_model(layout, replications=1, seed=2, trace=rec2, run_length=length, warmup=0)
    return d.log_kpis(log), d.log_kpis(d.log_from_trace(rec2, ready=ready)), layout, report


def _close(a, b, rel, floor=0.0):
    return abs(a - b) <= max(rel * abs(a), floor)


def test_round_trip_jackson_is_first_order_with_rework_and_a_set():
    source, draft, layout, report = _round_trip("job_shop_jackson.json", 3000)
    assert report["routing"]["mode"] == "markov"
    links = {(l["source"], l["target"]) for l in layout["links"]}
    assert ("after_process_assembly", "process_cutting") in links              # rework
    (pool,) = layout["resource_sets"]
    assert sorted(pool["members"]) == ["Drill1", "Drill2"]
    assert {r["name"]: r["capacity"] for r in layout["resources"]}["AssemblyStation"] == 2
    assert _close(source["throughput"], draft["throughput"], 0.05)
    for name, act in source["activities"].items():
        assert _close(act["service"], draft["activities"][name]["service"], 0.10), name


def test_round_trip_routed_shop_uses_variants():
    source, draft, layout, report = _round_trip("job_shop_routed.json", 3000)
    assert report["routing"]["mode"] == "variants"
    assert len(layout["sequences"]) == 3            # Widget, Bracket, Shaft
    assert report["transfers"] == []                # pooled travel on the ROUTE instead
    assert next(b for b in layout["blocks"] if b["type"] == "ROUTE")["params"]["dist_params"]
    assert _close(source["throughput"], draft["throughput"], 0.05)


@pytest.mark.slow
@pytest.mark.parametrize("model, ready", [("single_server.json", True),
                                          ("single_server.json", False),
                                          ("job_shop_jackson.json", True),
                                          ("job_shop_routed.json", True)])
def test_round_trip_kpis_within_tolerance(model, ready):
    """D559's tolerances on one long run each side (seed-to-seed noise of a 20 000 minute
    run is about 10% on a wait at these loads, hence the long run)."""
    source, draft, _layout, _report = _round_trip(model, 60_000, ready=ready)
    assert _close(source["throughput"], draft["throughput"], 0.05)
    assert _close(source["cycle_time"], draft["cycle_time"], 0.15)
    for name, act in source["activities"].items():
        mine = draft["activities"][name]
        assert _close(act["service"], mine["service"], 0.10), name
        assert _close(act["wait"], mine["wait"], 0.25, 0.5), name
