"""
Cross-direction profile builder - one per measurement channel.

Feeds on TBM frames (box number + value pairs) and maintains:

  * rolling (live) profile  latest value of every box with its timestamp.  Boxes
                            older than live_max_age_scans x scan time drop out.
                            Rolling average / 2-sigma are computed from it on
                            EVERY TBM refresh (every 5 s with TBM5S).
  * completed scans         produced when the head crosses edge-of-sheet (box 0)
                            or reverses, if the scan began at an edge and enough
                            of the sheet was covered.
  * point value             mean of valid elements of the latest refresh.

Completed-scan state machine:
    IDLE --positive box--> COLLECTING --box 0 / reversal--> evaluate --> IDLE
                                 |--mode leaves scanning / stale--> ABORT --> IDLE
A scan only counts if it STARTED at a sheet edge (after an element <= 0 or a
reversal); the partial scan seen right after start-up is discarded.
"""
from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass, field

from .tbm import is_valid


@dataclass
class CompletedScan:
    channel: str
    seq: int
    epoch: float
    duration_s: float
    direction: int
    first_box: int
    last_box: int
    valid_boxes: int
    coverage: float
    avg: float
    sigma2: float
    vmin: float
    vmax: float
    profile: list[float | None] = field(repr=False, default_factory=list)


@dataclass
class RollingStats:
    profile: list[float | None]
    n: int = 0
    avg: float | None = None
    sigma2: float = 0.0
    vmin: float = 0.0
    vmax: float = 0.0
    epoch: float = 0.0


@dataclass
class ScanResult:
    accepted: CompletedScan | None = None
    rejected_reason: str = ""


class ProfileBuilder:
    def __init__(self, channel: str, n_boxes: int, *, min_coverage: float = 0.8,
                 min_span: float = 0.25, edge_trim: int = 0, reversal_boxes: int = 3):
        self.channel = channel
        self.n = n_boxes
        self.min_coverage = min_coverage
        self.min_span = min_span
        self.edge_trim = edge_trim
        self.reversal_boxes = reversal_boxes
        self.expected_boxes = n_boxes
        self.live: list[float | None] = [None] * n_boxes
        self.live_ts: list[float] = [0.0] * n_boxes
        self.last: CompletedScan | None = None
        self.seq = 0
        self.point_value: float | None = None
        self.point_epoch = 0.0
        self.overflow = 0
        self.rejected = 0
        self.aborted = 0
        self._last_nonpos = False
        self._reset()

    def _reset(self) -> None:
        self._work: list[float | None] = [None] * self.n
        self._first = None
        self._prev = None
        self._dir = 0
        self._t0 = 0.0
        self._t_last_box = 0.0
        self._edge_start = False

    @property
    def collecting(self) -> bool:
        return self._first is not None

    def clear_live(self) -> None:
        self.live = [None] * self.n
        self.live_ts = [0.0] * self.n

    # ------------------------------------------------------------------ #
    def feed(self, boxes: list[int], values: list[float], ts: float | None = None) -> list[ScanResult]:
        ts = ts or time.time()
        results: list[ScanResult] = []
        valid_vals = []
        for b, v in zip(boxes, values):
            ok = is_valid(v)
            if ok:
                valid_vals.append(float(v))
            if b == 0:
                if self.collecting:
                    results.append(self._close(ts))
                self._last_nonpos = True
                continue
            if b < 0:
                self._last_nonpos = True
                continue
            if b > self.n:
                self.overflow += 1
                continue
            if self._prev is not None:
                step = b - self._prev
                d = (step > 0) - (step < 0)
                if d and self._dir and d != self._dir and abs(b - self._first) >= self.reversal_boxes \
                        and abs(step) <= self.reversal_boxes + 1:
                    results.append(self._close(ts))
                    self._last_nonpos = True
                elif d and not self._dir:
                    self._dir = d
            if not self.collecting:
                self._first, self._t0 = b, ts
                self._edge_start = self._last_nonpos
            self._last_nonpos = False
            idx = b - 1
            if ok:
                self._work[idx] = float(v)
                self.live[idx] = float(v)
                self.live_ts[idx] = ts
            self._prev = b
            self._t_last_box = ts
        if valid_vals:
            self.point_value = sum(valid_vals) / len(valid_vals)
            self.point_epoch = ts
        return results

    def rolling(self, now: float, max_age: float) -> RollingStats:
        prof: list[float | None] = [None] * self.n
        idx = []
        for i in range(self.n):
            v = self.live[i]
            if v is not None and now - self.live_ts[i] <= max_age:
                prof[i] = v
                idx.append(i)
        rs = RollingStats(prof, n=len(idx), epoch=now)
        if idx:
            t = self.edge_trim
            sidx = idx[t:len(idx) - t] if len(idx) > 2 * t else idx
            sv = [prof[i] for i in sidx]
            rs.avg = statistics.fmean(sv)
            rs.sigma2 = 2.0 * statistics.pstdev(sv) if len(sv) > 1 else 0.0
            rs.vmin, rs.vmax = min(sv), max(sv)
        return rs

    def abort(self, reason: str) -> int:
        self._last_nonpos = False
        if not self.collecting:
            return 0
        n = sum(1 for x in self._work if x is not None)
        self.aborted += 1
        self._reset()
        return n

    def stale(self, now: float, max_age: float) -> bool:
        return self.collecting and (now - self._t_last_box) > max_age

    def _close(self, ts: float) -> ScanResult:
        first, last = self._first, self._prev
        work, direction, t0, edge_start = self._work, self._dir or 1, self._t0, self._edge_start
        self._reset()
        if first is None or last is None:
            return ScanResult(rejected_reason="no boxes")
        if not edge_start:
            self.rejected += 1
            return ScanResult(rejected_reason=f"partial scan (started mid-sheet at box {first})")
        lo, hi = min(first, last), max(first, last)
        span = hi - lo + 1
        vals_idx = [i for i in range(lo - 1, hi) if work[i] is not None]
        coverage = len(vals_idx) / span if span else 0.0
        span_frac = span / max(1, self.expected_boxes)
        if span_frac < self.min_span:
            self.rejected += 1
            return ScanResult(rejected_reason=f"span {span} boxes = {span_frac:.0%} of {self.expected_boxes} (< {self.min_span:.0%})")
        if coverage < self.min_coverage:
            self.rejected += 1
            return ScanResult(rejected_reason=f"coverage {coverage:.0%} < {self.min_coverage:.0%}")
        t = self.edge_trim
        stat_idx = [i for i in vals_idx if (lo - 1 + t) <= i <= (hi - 1 - t)] or vals_idx
        sv = [work[i] for i in stat_idx]
        avg = statistics.fmean(sv)
        sig2 = 2.0 * statistics.pstdev(sv) if len(sv) > 1 else 0.0
        self.seq += 1
        prof = [None] * self.n
        for i in vals_idx:
            prof[i] = work[i]
        scan = CompletedScan(self.channel, self.seq, ts, max(0.0, ts - t0), direction, lo, hi,
                             len(vals_idx), coverage, avg, sig2, min(sv), max(sv), prof)
        self.last = scan
        return ScanResult(accepted=scan)


def fill(profile: list[float | None], length: int, fill_value: float) -> list[float]:
    out = [fill_value] * length
    for i, v in enumerate(profile[:length]):
        if v is not None and not math.isnan(v):
            out[i] = float(v)
    return out
