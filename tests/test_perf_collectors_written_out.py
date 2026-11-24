"""Speed review R6 (2026-11-24): `request_plain`, `_on_granted` and `release` write out the
`TimeWeighted.add` and `Tally.add` of the queue, busy and wait collectors. Every collector must
end bit-identical to the one the methods built before R6 (frozen below, calling `add`), on
models that reach every branch: plain seizes, a preempted holder, schedule blockers, a set."""
import pytest

from nagasim.core import resource_manager as rm
from nagasim.core.sim_runner import setup_run, step_until

from test_seize_plain_path import _failure, _preempted_plain, _schedule, _set, _tandem


def _request_plain(self, priority):
    req = rm.PriorityRequest(self._core, (priority, 0), False)
    t = self.env._now
    self._enqueued[req] = t
    self.queue_len.add(1, t)
    ctx = self.env.ctx
    if ctx.record_logs or ctx.watched:
        self.log_usage(t, event_type="request")
    else:
        self.log_events += 1
    req.callbacks.append(self._on_granted)
    return req


def _seized(self, req, t):
    self.busy.add(1, t)
    ctx = self.env.ctx
    if ctx.bottleneck_tracker is not None:
        self._notify_bottleneck(t)
    system = ctx.level_system
    if system is not None and system.reads_power:
        ctx.wake_power()
    if self._uncharged and req in self._uncharged:
        self._uncharged.discard(req)
        req.resumed_grant = True
        return
    self.seize_count += 1


def _on_granted(self, req):
    t = self.env._now
    if self._uncounted and req in self._uncounted:
        self._uncounted.discard(req)
        self._seized(req, t)
        self.log_usage(t, event_type="seize")
        return
    enqueued_at = self._enqueued.pop(req, None)
    if enqueued_at is None:
        return
    self.queue_len.add(-1, t)
    self._seized(req, t)
    self.wait.add(t - enqueued_at)
    ctx = self.env.ctx
    if ctx.record_logs or ctx.watched:
        self.log_usage(t, event_type="seize")
    else:
        self.log_events += 1


def _release(self, req):
    t = self.env._now
    if self._uncounted:
        self._uncounted.discard(req)
    ctx = self.env.ctx
    if self._enqueued.pop(req, None) is not None:
        self.queue_len.add(-1, t)
    else:
        self.busy.add(-1, t)
        if ctx.bottleneck_tracker is not None:
            self._notify_bottleneck(t)
        system = ctx.level_system
        if system is not None and system.reads_power:
            ctx.wake_power()
    result = self._core_release(req)
    if ctx.record_logs or ctx.watched:
        self.log_usage(t, event_type="release")
    else:
        self.log_events += 1
    return result


def _collectors(doc):
    env, _model = setup_run(doc, seed=5, run_len=200, warmup=20.0)
    step_until(env, 200)
    out = {"seizes": 0}
    for name, res in env.ctx.resource_manager.resources.items():
        if not isinstance(res, rm.LoggedSimpyResource):
            continue
        out["seizes"] += res.seize_count
        out[name] = repr(([(c.level, c.integral, c._max, c.last_t, c.start)
                           for c in (res.queue_len, res.busy)],
                          res.wait.values.tobytes(), res.seize_count, res.log_events, len(res.log)))
    return out      # by repr: an int level that became a float would compare equal


@pytest.mark.parametrize("build", [_tandem, _preempted_plain, _schedule, _failure, _set])
def test_written_out_collectors_match_the_add_calls(build, monkeypatch):
    new = _collectors(build())
    assert new["seizes"] > 50
    for name, fn in (("request_plain", _request_plain), ("_seized", _seized),
                     ("_on_granted", _on_granted), ("release", _release)):
        monkeypatch.setattr(rm.LoggedSimpyResource, name, fn)
    assert _collectors(build()) == new
