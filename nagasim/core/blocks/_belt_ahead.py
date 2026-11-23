"""Speed review D: an index over a belt's records in belt order, so `Conveyor._ahead_due` steps
over a run of parts ahead whose terms all plainly hold instead of walking each one.

The walk stays the one source of every result: the time, the deciding record, each `at` and the
break come from its own per-record body. The index only proves that a whole leaf, a run of
records next to each other in belt order, is a plain pass: every term holds (strict or not), no
record arrived where it stands (`rec[10]`), none ends the walk at its last arrived place
(`rec[11]`) and no part that just left lies between them. Then the walk adds the leaf's lengths
as it would have added them one by one, which is exact in two cases only:

- every occupied length is a multiple of 2^-24 m and at most 1024 m, so any sum is exact
  (`_DYADIC`): the leaf's sum is added;
- every occupied length is the same float (`_UNIFORM`), so the sum after k parts is the k-th
  entry of a table summed in walk order.

A belt whose parts have other mixed lengths (a typed belt with item sizes such as 0.37 and 0.6
cells of a non-dyadic cell) turns the index off and keeps the plain walk.

A leaf holds the records whose belt place (`rec[7]`) has the same floor over `LEAF`, so a part
leaving or boarding never moves another part between leaves. Its summary, rebuilt from its
records when a stage changed since, is the least of each record's nose less its offset in the
leaf, valid as the clock runs: a stand by its place, a ride at the belt speed by min(start +
speed x time, end). The margin the test keeps (`_U` x the magnitudes) is far above the floats'
rounding and, on belts up to a few km, far below `_EPS`, so a part packed exactly behind the
part ahead (its term within 1e-12) still passes. The count of records standing where they
arrived is kept exact at every stage change, so such a leaf is walked without a rebuild."""
import math

from nagasim.core.blocks._belt_points import _BOUND_MARGIN, _EPS

LEAF = 16                       # belt places per leaf (tests set 2 to skip as much as possible)
_MIN_SKIP = 2                   # a leaf with fewer records is walked
_MAX_SKIP = 128                 # nor one with more (merges): the margin covers that many sums
_U = 2.0 ** -46                 # the margin per unit of magnitude: 128 x a float's rounding
_SLOPE = 2.0 ** -50             # how far below the belt speed a ride may run and still count
_REBASE = 64.0                  # belt travel after which the ride keys' clock is reset
_USEFUL = 1e-7                  # a margin above this skips nothing worth the work
# what using the index costs, in walk steps: a call, a try, a rebuild (about one step per
# record). Each leaf stepped over earns its steps; once the score falls below zero (short
# walks, leaves that rarely pass) the walk is the plain one for the next `_PAUSE` calls
_CALL_COST, _TRY_COST, _SCORE_CAP, _PAUSE = 2, 8, 64, 256
_DYADIC, _UNIFORM = 1, 2
_GRAIN = 2.0 ** 24
# A leaf: [records, length sum, offset, stand min, ride start min, ride end min, ride end time
# min (strict), last arrived place max, blocked, stale, offsets stale, clock epoch, records
# standing where they arrived (kept exact: such a leaf ends the walk, so it is never skipped),
# the place in it of its front record and the place its next record gets at its back]
_N, _SUM, _OFF, _S, _RA, _RC, _D, _B, _BLOCK, _STALE, _FULL, _EPOCH, _ARR, _FIRST, _NEXT = \
    range(15)
_INF = math.inf


class AheadIndex:
    """The leaves of one belt (`Conveyor._ahead`); records carry their leaf at [12], their
    offset in it at [13] (the lengths of the leaf's records ahead of it, plus the leaf's
    offset) and their place in it at [14] (counted from the leaf's first record ever, so the
    walk finds a leaf's front from any of its records)."""

    __slots__ = ("leaves", "speed", "t0", "epoch", "mode", "unit", "sums", "xmag", "score",
                 "pause")

    def __init__(self, speed: float):
        self.leaves: dict = {}
        self.speed = speed
        self.t0 = 0.0
        self.epoch = 0
        self.mode = 0                   # 0 until the first part, then _DYADIC or _UNIFORM
        self.unit = 0.0
        self.sums = [0.0]
        self.xmag = 0.0
        self.score = _SCORE_CAP
        self.pause = 0

    # -- belt order ------------------------------------------------------------------------
    def accepts(self, occ: float, count: int) -> bool:
        """False once the lengths can no longer be summed exactly out of walk order."""
        if count >= 1 << 19:
            return False
        dyadic = 0.0 <= occ <= 1024.0 and (occ * _GRAIN).is_integer()
        if self.mode == 0:
            self.mode, self.unit = (_DYADIC, 0.0) if dyadic else (_UNIFORM, occ)
            return True
        if self.mode == _DYADIC:
            return dyadic
        return occ == self.unit

    def added(self, order: list, at: int, rec) -> None:
        """*rec* is now at *at* in the belt order *order*."""
        key = math.floor(rec[7] / LEAF)
        leaf = self.leaves.get(key)
        if leaf is None:
            leaf = self.leaves[key] = [0, 0.0, 0.0, _INF, _INF, _INF, _INF, -_INF, False, False,
                                       False, self.epoch, 0, 0, 0]
        rec[12] = leaf
        if at + 1 < len(order) and order[at + 1][12] is leaf:
            leaf[_FULL] = True              # not at the leaf's back: every offset moves
            rec[13], rec[14] = 0.0, 0
        else:
            rec[13], rec[14] = leaf[_SUM] + leaf[_OFF], leaf[_NEXT]
            if leaf[_N] == 0:
                leaf[_FIRST] = rec[14]
            leaf[_NEXT] += 1
        leaf[_N] += 1
        leaf[_SUM] += rec[0]
        leaf[_STALE] = True

    def removed(self, order: list, at: int, rec) -> None:
        """*rec*, at *at* in *order*, is about to leave it."""
        leaf = rec[12]
        if leaf is None:
            return
        rec[12] = None
        leaf[_N] -= 1
        if rec[10]:
            leaf[_ARR] -= 1
        if leaf[_N] == 0:
            self.leaves.pop(math.floor(rec[7] / LEAF), None)
            return
        leaf[_SUM] -= rec[0]
        if at > 0 and order[at - 1][12] is leaf:
            leaf[_FULL] = True              # not the leaf's front: the offsets behind it move
        else:
            leaf[_OFF] += rec[0]            # the leaf's front: every offset less its length
            leaf[_FIRST] += 1
        leaf[_STALE] = True

    # -- the summary -----------------------------------------------------------------------
    def rebuild(self, order: list, bottom: int, top: int, leaf) -> None:
        """*leaf*'s summary from its records, order[bottom..top]. A blocked leaf stops at the
        ride that blocks it: it is walked until a stage change makes it stale again."""
        if leaf[_FULL]:
            e = 0.0
            for k in range(bottom, top + 1):
                rec = order[k]
                rec[13], rec[14] = e, k - bottom
                e += rec[0]
            leaf[_SUM], leaf[_OFF], leaf[_FULL] = e, 0.0, False
            leaf[_FIRST], leaf[_NEXT] = 0, top - bottom + 1
        leaf[_EPOCH], leaf[_STALE] = self.epoch, False
        s = ra = rc = dd = _INF
        bb = -_INF
        w, t0, xmag, slope = self.speed, self.t0, self.xmag, 1.0 - _SLOPE
        for k in range(bottom, top + 1):
            rec = order[k]
            e = rec[13]
            x0, x1, d = rec[4], rec[5], rec[6]
            if x1 is None or d <= _EPS:
                x = x0 if x1 is None else x1
                if x > xmag or -x > xmag:
                    xmag = x if x > 0 else -x
                if x + e < s:
                    s = x + e
            else:
                if x1 - x0 < w * d * slope:
                    leaf[_BLOCK] = True
                    self.xmag = xmag
                    return
                if x1 > xmag or -x0 > xmag:
                    xmag = x1 if x1 > -x0 else -x0
                t = rec[3]
                if x0 + e - w * (t - t0) < ra:
                    ra = x0 + e - w * (t - t0)
                if x1 + e < rc:
                    rc = x1 + e
                if t + d - _EPS < dd:
                    dd = t + d - _EPS
            if rec[11] is not None and rec[11] + e > bb:
                bb = rec[11] + e
        self.xmag = xmag
        leaf[_S], leaf[_RA], leaf[_RC], leaf[_D], leaf[_B], leaf[_BLOCK] = s, ra, rc, dd, bb, False

    def clear(self, order: list, bottom: int, top: int, leaf, y: float, before: float,
              walked: int, now: float, strict: bool, bounded: bool):
        """The walk's lengths after order[bottom..top] when every term there plainly holds for
        a part waiting to reach *y* (the walk would pass each with no `at`, no None and no
        break), else None; *before* is the lengths before the leaf, *walked* the parts walked
        up to and including order[bottom]. The lengths are *before* plus the leaf's sum, or the
        table's entry (`_UNIFORM`). Each try scores what it saved against what it cost
        (`score`, in walk steps)."""
        count = top - bottom + 1
        if self.mode == _DYADIC:
            after = before + leaf[_SUM]
        else:
            sums = self.sums
            while len(sums) <= walked:
                sums.append(sums[-1] + self.unit)
            after = sums[walked]
        w = self.speed
        span = w * (now - self.t0)
        if span > _REBASE:
            self.t0, self.epoch, span = now, self.epoch + 1, 0.0
        if leaf[_STALE] or leaf[_FULL] or leaf[_EPOCH] != self.epoch:
            self.rebuild(order, bottom, top, leaf)
            rebuilt = count
        else:
            rebuilt = 0
        ok = False
        if not leaf[_BLOCK]:
            # the rounding of every float the walk and this test compute, with room to spare
            # (`_U`): the ride formula and keys (`xmag`, the clock), z, and the offsets, which
            # a uniform belt sums in another order (up to `_MAX_SKIP` of them)
            m = ((y if y > 0 else -y) + 4.0 * (after + self.xmag + span) + 1.0) * _U
            z = y + after
            off = leaf[_OFF]
            if m <= _USEFUL and not (bounded and leaf[_B] - off >= z + _BOUND_MARGIN - m):
                low, high = z - _EPS + m, z + _EPS + m
                rides = leaf[_RA] + span
                if leaf[_RC] < rides:
                    rides = leaf[_RC]
                rides -= off
                stands = leaf[_S] - off
                if strict:
                    ok = stands > high and (rides > high or (rides >= low and now < leaf[_D]))
                else:
                    ok = stands >= low and rides >= low
        # a pass saves the leaf's steps (its rebuild serves the next walks too); a miss costs
        # the try and the rebuild
        score = self.score + (count - _TRY_COST if ok else -_TRY_COST - rebuilt)
        self.score = score if score < _SCORE_CAP else _SCORE_CAP
        return after if ok else None
