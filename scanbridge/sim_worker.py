"""
Scanner simulator - drop-in replacement for the OPC worker process.

Same queue protocol as opc_worker.py, so the whole bridge (discovery
classification, model, profiles, PLC link, commands, supervisor) can be
commissioned on the bench without a scanner or DCOM:  [opc] simulate = true.

Simulates frame SIM.SP1 with WT1 (basis weight) and MO1 (IR moisture) on a
600-box frame, item IDs in your Data Dictionary style (SIM.SP1.WT1.MEAS.TBM5S).
Honours opc.tbm_rate (1S / 5S).  Reacts to frame commands, online/offline and
slope/offset writes.

Recovery-test hooks (environment):
  SB_SIM_HANG_AFTER=<s>    stop updating the heartbeat (simulates a hung DCOM call)
  SB_SIM_STALL_AFTER=<s>   keep running but stop sending TBM frames
"""
from __future__ import annotations

import math
import os
import queue
import random
import time
from datetime import datetime

from . import catalog as cat
from . import discovery as disc
from . import logsetup
from .tbm import TBMFrame

log = logsetup.get("sim")

SIM_PREFIX = "SIM.SP1"
SIM_SENSORS = ["wt1", "mo1"]
_SEC_TOKEN = {cat.MEAS: "MEAS", cat.STDZ: "STDZ", cat.SMPLCHK: "SMPLCHK", cat.CALSMPL: "CALSMPL"}


def sim_item_ids() -> list[str]:
    ids = [f"{SIM_PREFIX}.{u}" for u in sorted(cat.FRAME_USER_TAGS)]
    for key in SIM_SENSORS:
        st = cat.SENSOR_TYPES[key[:2]]
        for t in st.tags:
            ids.append(f"{SIM_PREFIX}.{key.upper()}.{_SEC_TOKEN[t.section]}.{t.user}")
        for suffix in st.channels:
            for rate in ("1S", "5S"):
                ids.append(f"{SIM_PREFIX}.{key.upper()}.MEAS.{cat.tbm_user_tag(suffix, rate)}")
    ids.append("SIM.SP2.Mode")                      # another frame - must be ignored by scoping
    return ids


def build_item_map(extra_patterns=None) -> tuple[dict[str, str], list]:
    clf = disc.Classifier(extra_patterns, SIM_PREFIX)
    items = [clf.classify(disc.BrowsedItem(i, [], i.split(".")[-1])) for i in sim_item_ids()]
    items = [i for i in items if i.how != "out-of-scope"]
    bad = [i.item_id for i in items if not i.canonical]
    if bad:
        log.warning("[SB-208] simulator: %d item IDs not classified: %s", len(bad), bad[:5])
    return disc.to_item_map(items), [(i.item_id, i.canonical, i.how) for i in items]


def main(cfg_dict: dict, to_core, to_worker, hb, log_q) -> None:
    from .config import Config
    cfg = Config.from_dict(cfg_dict)
    logsetup.setup_worker(log_q, cfg.logging.level)
    Sim(cfg, to_core, to_worker, hb).run()


class Sim:
    def __init__(self, cfg, to_core, to_worker, hb):
        self.cfg, self.to_core, self.to_worker, self.hb = cfg, to_core, to_worker, hb
        self.period = cfg.tbm_period_s
        self.n = 600
        self.scan_time = 20
        self.edge_lo, self.edge_hi = 15, 585
        self.limit_lo, self.limit_hi = 8, 592
        self.box_width = 0.5
        self.pos = 300.0
        self.dir = 1
        self.mode = 6
        self.prev_scan_mode = 6
        self.stdz_until = 0.0
        self.trigger = 1000
        self.stdz_trig = 1
        self.slope = {"wt1": 1.0, "mo1": 1.0}
        self.offset = {"wt1": 0.0, "mo1": 0.0}
        self.offline = set()
        self.snglpt = 300.0
        self.stdz_time = datetime.now().strftime("%m-%d-%Y %H:%M:%S")
        self.t0 = time.time()
        self.hang_after = float(os.environ.get("SB_SIM_HANG_AFTER", "0") or 0)
        self.stall_after = float(os.environ.get("SB_SIM_STALL_AFTER", "0") or 0)
        self.rng = random.Random(42)
        self.sent_state: dict = {}

    def send(self, m):
        self.to_core.put(m)

    def announce(self):
        self.item_map, browsed = build_item_map(self.cfg.discovery_patterns)
        self.send(("discovering", SIM_PREFIX))
        self.send(("discovered", self.item_map, {"items": len(browsed), "sensors": SIM_SENSORS + ["sp1"],
                                                 "browsed": browsed}))
        self.send(("connected", {"node": "SIMULATOR", "frame": SIM_PREFIX, "server_state": 1, "mode": "sim"}))
        self.send_state(force=True)

    def run(self):
        self.hb.value = time.time()
        self.send(("connecting", "SIMULATOR"))
        self.announce()
        log.info("[SB-202] simulator connected: %s sensors %s, %d boxes, scan time %d s, TBM every %.0f s",
                 SIM_PREFIX, SIM_SENSORS, self.n, self.scan_time, self.period)
        next_tick = time.time() + self.period
        next_state = time.time() + 1.0
        while True:
            hung = self.hang_after and time.time() - self.t0 >= self.hang_after
            if not hung:
                self.hb.value = time.time()
            try:
                msg = self.to_worker.get(timeout=0.05)
                if msg[0] == "stop":
                    return
                if msg[0] == "write":
                    self.on_write(msg[1], msg[2])
                if msg[0] in ("rediscover", "reconnect"):
                    self.send(("disconnected", msg[0]))
                    self.announce()
            except queue.Empty:
                pass
            if hung:
                continue
            now = time.time()
            if now >= next_state:
                next_state += 1.0
                self.advance_modes()
                self.send_state()
            if now >= next_tick:
                next_tick += self.period
                self.tick()

    def value(self, key: str, box: float) -> float:
        x = box / self.n
        if key == "wt1":
            raw = 45.0 + 1.2 * math.sin(2 * math.pi * x * 2) + 0.8 * x + self.rng.gauss(0, 0.15)
        else:
            raw = 6.0 + 0.6 * math.cos(2 * math.pi * x) + self.rng.gauss(0, 0.08)
        return raw * self.slope[key] + self.offset[key]

    def advance_modes(self):
        if self.mode == 8 and time.time() >= self.stdz_until:
            self.mode = self.prev_scan_mode
            self.stdz_trig += 1
            self.stdz_time = datetime.now().strftime("%m-%d-%Y %H:%M:%S")

    def tick(self):
        now = time.time()
        boxes, heads = [], []
        if self.mode in (6, 7):
            steps = int(round(self.n / self.scan_time * self.period))
            for _ in range(steps):
                prev_inside = self.edge_lo <= self.pos <= self.edge_hi
                self.pos += self.dir
                if self.pos >= self.limit_hi or self.pos <= self.limit_lo:
                    self.dir = -self.dir
                inside = self.edge_lo <= self.pos <= self.edge_hi
                if prev_inside and not inside:
                    boxes.append(0)
                    heads.append(self.pos * self.box_width)
                b = int(self.pos)
                boxes.append(b if inside else -abs(b) or -1)
                heads.append(self.pos * self.box_width)
        else:
            k = int(self.period)
            boxes = [-1] * k
            heads = [(self.snglpt if self.mode == 5 else -20.0)] * k
        chans = {}
        for key in SIM_SENSORS:
            vals = []
            for b in boxes:
                measuring = self.mode in (5, 6, 7) and key not in self.offline
                if not measuring or self.rng.random() < 0.01:
                    vals.append(cat.INVALID_MEASUREMENT)
                elif b > 0:
                    vals.append(self.value(key, b))
                else:
                    vals.append(self.value(key, self.snglpt / self.box_width if self.mode == 5 else self.pos))
            chans[f"{key}.TBM"] = vals
        self.trigger += 1
        if self.stall_after and now - self.t0 > self.stall_after:
            return
        self.send(("tbm", TBMFrame(ts=now, rx=now, trigger=self.trigger, boxes=boxes, head=heads, channels=chans)))

    def send_state(self, force=False):
        now = time.time()
        st = {
            "sp1.frame.Mode": self.mode, "sp1.frame.ScanTime": self.scan_time, "sp1.frame.NumberOfDataBoxes": self.n,
            "sp1.frame.DataBoxWidth": self.box_width, "sp1.frame.HomeEOSPosition": self.edge_lo * self.box_width,
            "sp1.frame.FarEOSPosition": self.edge_hi * self.box_width, "sp1.frame.ActiveAlarms": 0,
            "sp1.frame.SafetyAlarms": 0, "sp1.frame.DigitalInputs": 1, "sp1.frame.WorkStnControlCode": 0,
            "sp1.frame.AEOSStatus": 1, "sp1.frame.SnglPtHeadPosition": self.snglpt,
            "sp1.frame.StandardizeCompTrigger": self.stdz_trig,
            "sp1.frame.ConfigurationArray": [0x00010400, 0x00010B00] + [0] * 18,
        }
        for key in SIM_SENSORS:
            status = 0
            if self.mode not in (5, 6, 7) or key in self.offline:
                status |= 0x4000
            if self.mode == 8:
                status |= 0x0100
            st[f"{key}.meas.Status"] = status
            st[f"{key}.meas.Slope"] = self.slope[key]
            st[f"{key}.meas.Offset"] = self.offset[key]
            st[f"{key}.meas.StdzWaitTime"] = 60
            st[f"{key}.stdz.Status"] = 0
            st[f"{key}.stdz.Time"] = self.stdz_time
            if key == "wt1":
                st["wt1.meas.ShutterState"] = 1 if self.mode in (0, 8) else 0
        changed = {k: (v, 192, now) for k, v in st.items() if force or self.sent_state.get(k) != v}
        self.sent_state.update(st)
        if changed:
            self.send(("values", changed))

    def on_write(self, ticket, writes):
        res = []
        for key, value, dtype in writes:
            ok, err = True, ""
            user = key.split(".", 2)[2]
            sensor = key.split(".")[0]
            if key not in self.item_map:
                ok, err = False, "0xC0040007 OPC_E_UNKNOWNITEMID"
            elif user == "Command":
                v = int(value)
                if v == 1:
                    self.mode = 0
                elif v == 2:
                    self.mode = 5
                elif v in (3, 11):
                    self.mode = 6
                elif v == 4:
                    if self.mode in (5, 6, 7):
                        self.prev_scan_mode = 6 if self.mode == 7 else self.mode
                    self.mode, self.stdz_until = 8, time.time() + 10
            elif user == "SetScanTime":
                self.scan_time = max(5, int(value))
            elif user == "SetNumberOfDataBoxes":
                self.n = int(value)
            elif user == "SnglPtHeadPosition":
                self.snglpt = float(value)
            elif user.startswith("Slope") and sensor in self.slope:
                self.slope[sensor] = float(value)
            elif user.startswith("Offset") and sensor in self.offset:
                self.offset[sensor] = float(value)
            elif user == "SetOffline":
                self.offline.add(sensor)
            elif user == "SetOnline":
                self.offline.discard(sensor)
            log.info("[SB-214] SIM write %s (%s) = %r -> %s", key, self.item_map.get(key, "?"), value, "OK" if ok else err)
            res.append((key, ok, err))
        self.send(("write_result", ticket, res))
        self.send_state()
