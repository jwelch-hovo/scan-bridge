"""
Time-Based Measurement (TBM) frames.

App Notes rules enforced here:
  * TBM arrays of one refresh (1 s or 5 s) are spatially aligned: element i of
    every array is the same cross-machine position, and HeadPosition,
    DataBoxNumber and every sensor's TBM array have the same length.
  * DataBoxNumber > 0 measured box while scanning; == 0 edge-of-sheet marker;
    < 0 outside the sheet / not scanning (element is a 1 s average).
  * -1000000.0 = invalid measurement.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .catalog import INVALID_MEASUREMENT


@dataclass
class TBMFrame:
    ts: float
    rx: float
    trigger: int
    boxes: list[int]
    head: list[float]
    channels: dict[str, list[float]] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.boxes)


class SnapshotError(Exception):
    pass


def as_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return list(v)
    return [v]


def is_valid(v) -> bool:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return not (math.isnan(f) or math.isinf(f) or f <= INVALID_MEASUREMENT + 1.0)


def build_frame(boxes, head, channels: dict[str, object], timestamps: dict[str, float],
                trigger: int, rx: float, tolerance_s: float) -> TBMFrame:
    b = [int(x) for x in as_list(boxes)]
    h = [float(x) for x in as_list(head)]
    if len(b) != len(h):
        raise SnapshotError(f"DataBoxNumber has {len(b)} elements but HeadPosition has {len(h)}")
    ts_vals = [t for t in timestamps.values() if t]
    if ts_vals and (max(ts_vals) - min(ts_vals)) > tolerance_s:
        spread = max(ts_vals) - min(ts_vals)
        raise SnapshotError(f"item timestamps differ by {spread*1000:.0f} ms (> {tolerance_s*1000:.0f} ms)")
    frame = TBMFrame(ts=max(ts_vals) if ts_vals else rx, rx=rx, trigger=trigger, boxes=b, head=h)
    for key, vals in channels.items():
        v = as_list(vals)
        if len(v) != len(b):
            frame.dropped.append(f"{key}({len(v)}!={len(b)})")
            continue
        frame.channels[key] = [float(x) if x is not None else INVALID_MEASUREMENT for x in v]
    return frame
