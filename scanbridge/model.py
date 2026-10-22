"""
In-memory model of one scanner frame.

The OPC side pushes values / TBM frames in; the PLC side pulls a consistent
snapshot out.  One lock; work under the lock is small.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from . import catalog as cat
from . import logsetup
from .profile import CompletedScan, ProfileBuilder, RollingStats, fill
from .tbm import TBMFrame

log = logsetup.get("model")

ST_STOPPED, ST_STARTING, ST_CONNECTING, ST_DISCOVERING, ST_RUNNING, ST_DEGRADED, ST_RECONNECTING = range(7)
STATE_NAMES = ["stopped", "starting", "connecting", "discovering", "running", "degraded", "reconnecting"]


def to_int(v, default: int = 0) -> int:
    if v is None:
        return default
    if isinstance(v, str):
        if len(v) == 1 and not v.isdigit():
            return ord(v)
        s = v.strip("\x00 ")
        try:
            return int(float(s)) if s else default
        except ValueError:
            return default
    if isinstance(v, (bytes, bytearray)):
        return v[0] if v else default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def to_float(v, default: float = 0.0) -> float:
    try:
        f = float(v)
        return default if math.isnan(f) else f
    except (TypeError, ValueError):
        return default


def to_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v.split("\x00")[0].strip()
    if isinstance(v, (bytes, bytearray)):
        return v.split(b"\x00")[0].decode("ascii", "replace").strip()
    try:
        chars = []
        for c in v:
            c = to_int(c)
            if c == 0:
                break
            chars.append(chr(c & 0xFF))
        return "".join(chars).strip()
    except TypeError:
        return str(v)


def parse_abb_time(v) -> int:
    s = to_text(v)
    if not s:
        return 0
    for fmt in ("%m-%d-%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            return int(datetime.strptime(s, fmt).timestamp())
        except ValueError:
            continue
    return 0


def maint_bits(v) -> int:
    bits = 0
    for c in (v if isinstance(v, (list, tuple, bytes, bytearray)) else [v]):
        c = to_int(c)
        if 0x40 <= c <= 0x5F:
            bits |= 1 << (c - 0x40)
    return bits


@dataclass
class SensorState:
    key: str
    stype: cat.SensorType
    slot: int = -1
    present: bool = False
    bad_mask: int = 0xC007
    v: dict = field(default_factory=dict)
    seq: int = 0

    def __post_init__(self):
        tt, inst = cat.sensor_key_parts(self.key)
        self.v = {"Present": 0, "TypeCode": self.stype.type_code, "Instance": inst, "Status": 0,
                  "StatusBad": 0, "Shutter": 0, "Slope": 0.0, "Offset": 0.0, "StdzWaitTime": 0,
                  "StdzStatus": 0, "StdzEpoch": 0, "SmplChkStatus": 0, "SmplChkEpoch": 0,
                  "CalSmplStatus": 0, "CalSmplEpoch": 0, "MaintAlarms": 0}


@dataclass
class ChannelState:
    key: str
    sensor_key: str
    slot: int
    builder: ProfileBuilder
    scale: float = 1.0
    offset: float = 0.0
    present: bool = False
    live_seq: int = 0
    pv_seq: int = 0
    rejected_last: bool = False
    pending_profile: CompletedScan | None = None
    live_dirty: bool = False
    roll: RollingStats | None = None
    roll_ok: bool = False

    def eu(self, x: float | None) -> float:
        return 0.0 if x is None else x * self.scale + self.offset


@dataclass
class Counters:
    tbm_frames: int = 0
    tbm_rejected: int = 0
    tbm_gaps: int = 0
    opc_reconnects: int = 0
    worker_restarts: int = 0
    plc_write_errs: int = 0
    scans: int = 0
    last_error: int = 0


class ScannerModel:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.t_start = time.time()
        self.state = ST_STARTING
        self.opc_connected = False
        self.opc_server_state = 0
        self.needs_setup = False
        self.sensors: dict[str, SensorState] = {}
        self.channels: dict[str, ChannelState] = {}
        self.frame: dict = self._frame_defaults()
        self.frame_seq = 0
        self.counters = Counters()
        self.last_tbm_rx = 0.0
        self.last_tbm_ts = 0.0
        self.connected_at = 0.0
        self.item_map: dict[str, str] = {}
        self._prev_trigger_vals: dict[str, int] = {}
        self._tbm_period = cfg.tbm_period_s
        self.stale_s = cfg.stale_s

    @staticmethod
    def _frame_defaults() -> dict:
        d = {k: 0 for k in ("Mode", "Scanning", "ScanTime", "NumBoxes", "Direction", "ActiveAlarms",
                            "SafetyAlarms", "DigitalInputs", "WorkStnCtrl", "AEOSStatus", "AEOSError",
                            "SheetBreak", "CalSmplStatus", "StdzEpoch", "ScanCount")}
        d.update({k: 0.0 for k in ("DataBoxWidth", "HeadPos", "HomeEOS", "FarEOS", "HomeScanLimit",
                                   "FarScanLimit", "HomeMeasLimit", "FarMeasLimit", "SnglPtPos",
                                   "OffSheetPos", "MinSinceStdz")})
        d["ConfigArray"] = [0] * 20
        d["Mode"] = -1
        return d

    # ================================================================== #
    def configure(self, item_map: dict[str, str]) -> None:
        with self.lock:
            self.item_map = dict(item_map)
            mcfg = self.cfg.model
            rate = self.cfg.opc.tbm_rate
            sensors_found = sorted({k.split(".")[0] for k in item_map
                                    if not k.startswith(cat.FRAME_KEY) and k[:2] in cat.SENSOR_TYPES})
            chans_found = sorted({ck for k in item_map if (ck := cat.channel_key_from_canonical(k, rate))})
            cfg_s = {s.key: s for s in self.cfg.sensor}
            used = {s.slot for s in self.cfg.sensor if s.slot >= 0}
            for key in sorted(set(sensors_found) | set(cfg_s)):
                st = self.sensors.get(key)
                if st is None:
                    stype = cat.SENSOR_TYPES.get(key[:2])
                    if stype is None:
                        log.warning("[SB-405] sensor '%s' in config has unknown type - ignored", key)
                        continue
                    st = SensorState(key, stype)
                    sc = cfg_s.get(key)
                    st.bad_mask = sc.bad_status_mask if sc and sc.bad_status_mask >= 0 else stype.bad_mask
                    if sc and sc.slot >= 0:
                        st.slot = sc.slot
                    else:
                        st.slot = _next_free(used, mcfg.max_sensors)
                        if st.slot >= 0:
                            used.add(st.slot)
                            log.info("[SB-405] sensor %s (%s) auto-assigned to Sensor[%d] - pin it in the config",
                                     key, stype.name, st.slot)
                        else:
                            log.warning("[SB-405] sensor %s discovered but no free Sensor[] slot", key)
                    self.sensors[key] = st
                st.present = key in sensors_found
                st.v["Present"] = 1 if st.present else 0
                if not st.present:
                    log.warning("[SB-405] configured sensor %s not found on the OPC server", key)
            cfg_c = {c.key: c for c in self.cfg.channel}
            used = {c.slot for c in self.cfg.channel if c.slot >= 0}
            for key in sorted(set(chans_found) | set(cfg_c)):
                ch = self.channels.get(key)
                if ch is None:
                    cc = cfg_c.get(key)
                    slot = cc.slot if cc and cc.slot >= 0 else _next_free(used, mcfg.max_channels)
                    if slot < 0:
                        log.warning("[SB-405] channel %s discovered but no free Chan[] slot", key)
                        continue
                    if not (cc and cc.slot >= 0):
                        used.add(slot)
                        log.info("[SB-405] channel %s auto-assigned to Chan[%d] - pin it in the config", key, slot)
                    b = ProfileBuilder(key, mcfg.profile_len, min_coverage=mcfg.min_coverage,
                                       min_span=mcfg.min_span, edge_trim=mcfg.edge_trim_boxes,
                                       reversal_boxes=mcfg.reversal_boxes)
                    ch = ChannelState(key, key.split(".")[0], slot, b,
                                      scale=cc.scale if cc else 1.0, offset=cc.offset if cc else 0.0)
                    self.channels[key] = ch
                ch.present = key in chans_found
                if not ch.present:
                    log.warning("[SB-405] configured channel %s has no TBM%s item on the OPC server", key, rate)
            log.info("[SB-206] model configured: sensors=%s channels=%s",
                     {k: s.slot for k, s in self.sensors.items() if s.present},
                     {k: c.slot for k, c in self.channels.items() if c.present})

    # ================================================================== #
    def set_connection(self, connected: bool, server_state: int = 0) -> None:
        with self.lock:
            if connected and not self.opc_connected:
                self.counters.opc_reconnects += 1
                self.needs_setup = True
                self.connected_at = time.time()
            self.opc_connected = connected
            self.opc_server_state = server_state
            if not connected:
                for ch in self.channels.values():
                    ch.builder.abort("opc disconnected")
                self.state = ST_RECONNECTING
            else:
                self.state = ST_RUNNING

    def apply_values(self, values: dict[str, tuple]) -> None:
        with self.lock:
            for key, (val, q, ts) in values.items():
                if q is not None and (int(q) & 0xC0) != 0xC0:
                    continue
                try:
                    sensor, section, user = key.split(".", 2)
                except ValueError:
                    continue
                if sensor == cat.FRAME_KEY:
                    self._apply_frame(user, val)
                else:
                    st = self.sensors.get(sensor)
                    if st is not None:
                        self._apply_sensor(st, section, user, val)

    def _set_frame(self, member: str, value) -> None:
        if self.frame.get(member) != value:
            self.frame[member] = value
            self.frame_seq += 1

    def _apply_frame(self, user: str, val) -> None:
        f = self.frame
        if user == "Mode":
            m = to_int(val)
            if m != f["Mode"]:
                log.info("[SB-406] frame mode %s -> %s", cat.FRAME_MODE.get(f["Mode"], f["Mode"]),
                         cat.FRAME_MODE.get(m, m))
                if m in cat.MODES_ABORT_SCAN:
                    for ch in self.channels.values():
                        n = ch.builder.abort("mode")
                        if n:
                            log.info("[SB-403] %s: scan aborted (mode %s), %d boxes discarded",
                                     ch.key, cat.FRAME_MODE.get(m, m), n)
            self._set_frame("Mode", m)
            self._set_frame("Scanning", 1 if m in (6, 7) else 0)
        elif user in ("ActiveAlarms", "SafetyAlarms", "DigitalInputs"):
            new = to_int(val) & 0xFFFFFFFF
            old = f[user] & 0xFFFFFFFF
            if new != old:
                table = {"ActiveAlarms": cat.ACTIVE_ALARM_BITS, "SafetyAlarms": cat.SAFETY_ALARM_BITS,
                         "DigitalInputs": cat.DIGITAL_INPUT_BITS}[user]
                on = cat.bits_to_names(new & ~old, table)
                off = cat.bits_to_names(old & ~new, table)
                lvl = log.warning if (on and user != "DigitalInputs") else log.info
                lvl("[SB-407] %s changed: set=%s cleared=%s", user, on or "-", off or "-")
            self._set_frame(user, _s32(new))
        elif user == "AEOSErrorStatus":
            e = to_int(val)
            if e != f["AEOSError"] and e:
                log.warning("[SB-407] AEOS error %d: %s", e, cat.AEOS_ERRORS.get(e, "unknown"))
            self._set_frame("AEOSError", e)
        elif user == "ConfigurationArray":
            arr = [_s32(to_int(x)) for x in (val or [])][:20]
            arr += [0] * (20 - len(arr))
            if arr != f["ConfigArray"]:
                for e in cat.decode_config_array(arr):
                    log.info("[SB-217] config[%d]: %s (class %d type %d options %d instance %d slot %d) -> %s",
                             e.index, e.class_name, e.sensor_class, e.sensor_type, e.options, e.instance,
                             e.slot, ",".join(e.sensor_keys) or "?")
                f["ConfigArray"] = arr
                self.frame_seq += 1
        elif user == "StandardizeCompTrigger":
            v = to_int(val)
            prev = self._prev_trigger_vals.get(user)
            self._prev_trigger_vals[user] = v
            if prev is not None and v != prev:
                self._set_frame("StdzEpoch", int(time.time()))
                log.info("[SB-406] standardize complete")
        else:
            simple = {"ScanTime": ("ScanTime", to_int), "NumberOfDataBoxes": ("NumBoxes", to_int),
                      "DataBoxWidth": ("DataBoxWidth", to_float), "HomeEOSPosition": ("HomeEOS", to_float),
                      "FarEOSPosition": ("FarEOS", to_float), "HomeScanLimit": ("HomeScanLimit", to_float),
                      "FarScanLimit": ("FarScanLimit", to_float), "HomeMeasureLimit": ("HomeMeasLimit", to_float),
                      "FarMeasureLimit": ("FarMeasLimit", to_float), "SnglPtHeadPosition": ("SnglPtPos", to_float),
                      "OffSheetHeadPosition": ("OffSheetPos", to_float), "WorkStnControlCode": ("WorkStnCtrl", to_int),
                      "AEOSStatus": ("AEOSStatus", to_int), "SheetBreakIndicatorStatus": ("SheetBreak", to_int),
                      "CalibrateSampleStatus": ("CalSmplStatus", to_int),
                      "CurrentHeadPosition": ("HeadPos", to_float)}
            if user in simple:
                member, conv = simple[user]
                if user == "CurrentHeadPosition" and self.last_tbm_rx and time.time() - self.last_tbm_rx < 2 * self._tbm_period:
                    return
                new = conv(val)
                if user == "ScanTime" and isinstance(val, str) and len(val) > 1:
                    new = to_int(val.strip("\x00 "))
                if user == "NumberOfDataBoxes" and new and new != f["NumBoxes"]:
                    if new > self.cfg.model.profile_len:
                        log.error("[SB-404] NumberOfDataBoxes=%d exceeds PLC profile length %d - boxes above are "
                                  "dropped. Regenerate the UDTs with a larger profile_len.", new, self.cfg.model.profile_len)
                    for ch in self.channels.values():
                        ch.builder.expected_boxes = min(new, self.cfg.model.profile_len)
                if user == "WorkStnControlCode" and new != f["WorkStnCtrl"]:
                    log.info("[SB-406] control: %s", "WORKSTATION" if new else "HOST")
                self._set_frame(member, new)

    def _apply_sensor(self, st: SensorState, section: str, user: str, val) -> None:
        v = st.v
        before = dict(v)
        if section == cat.MEAS:
            if user == "Status":
                s = to_int(val) & 0xFFFF
                if s != v["Status"]:
                    on = cat.bits_to_names(s & ~v["Status"], st.stype.status_bits, 16)
                    off = cat.bits_to_names(v["Status"] & ~s, st.stype.status_bits, 16)
                    scanning = self.frame.get("Mode") in (5, 6, 7)
                    lvl = log.warning if (s & st.bad_mask & ~v["Status"]) and scanning else log.info
                    lvl("[SB-218] %s status 0x%04X -> 0x%04X set=%s cleared=%s", st.key, v["Status"], s,
                        on or "-", off or "-")
                v["Status"] = s
                v["StatusBad"] = s & st.bad_mask
            elif user == "ShutterState" and st.stype.shutter_closed_value is not None:
                sh = 2 if to_int(val) == st.stype.shutter_closed_value else 1
                if sh != v["Shutter"] and v["Shutter"]:
                    log.info("[SB-408] %s shutter %s", st.key, "CLOSED" if sh == 2 else "OPEN")
                v["Shutter"] = sh
            elif user in ("Slope", "SlopeMoisture"):
                v["Slope"] = to_float(val)
            elif user in ("Offset", "OffsetMoisture"):
                v["Offset"] = to_float(val)
            elif user == "StdzWaitTime":
                v["StdzWaitTime"] = to_int(val)
            elif user == "MaintenanceAlarms":
                bits = maint_bits(val)
                if bits != v["MaintAlarms"] and bits:
                    names = [cat.MAINT_ALARM_CODES.get(st.key[:2], {}).get(0x40 + b, f"0x{0x40+b:02X}")
                             for b in range(26) if bits & (1 << b)]
                    log.warning("[SB-218] %s maintenance alarms: %s", st.key, names)
                v["MaintAlarms"] = bits
        else:
            prefix = {cat.STDZ: "Stdz", cat.SMPLCHK: "SmplChk", cat.CALSMPL: "CalSmpl"}.get(section)
            if prefix is None:
                return
            if user == "Status":
                s = to_int(val)
                if s != v[prefix + "Status"] and s != 0:
                    log.warning("[SB-218] %s %s status %d (non-zero = failed)", st.key, prefix, s)
                v[prefix + "Status"] = s
            elif user == "Time":
                e = parse_abb_time(val)
                if e and e != v[prefix + "Epoch"]:
                    v[prefix + "Epoch"] = e
                    if prefix == "Stdz" and e > self.frame["StdzEpoch"]:
                        self._set_frame("StdzEpoch", e)
        if v != before:
            st.seq += 1

    # ------------------------------------------------------------------ #
    def _scan_time_s(self) -> float:
        st = self.frame.get("ScanTime") or 0
        return float(st) if st else 30.0

    def apply_tbm(self, fr: TBMFrame) -> list[CompletedScan]:
        out = []
        with self.lock:
            if self.last_tbm_ts and fr.ts - self.last_tbm_ts > 1.8 * self._tbm_period:
                missed = int(round((fr.ts - self.last_tbm_ts) / self._tbm_period)) - 1
                if missed > 0:
                    self.counters.tbm_gaps += missed
                    log.warning("[SB-212] TBM gap: ~%d refresh(es) missed (%.1f s since last frame)",
                                missed, fr.ts - self.last_tbm_ts)
            self.last_tbm_ts, self.last_tbm_rx = fr.ts, fr.rx
            self.counters.tbm_frames += 1
            if fr.head:
                self._set_frame("HeadPos", round(fr.head[-1], 3))
            pos = [b for b in fr.boxes if b > 0]
            if len(pos) >= 2:
                self._set_frame("Direction", 1 if pos[-1] > pos[0] else -1)
            elif not pos:
                self._set_frame("Direction", 0)
            max_age = self.cfg.model.live_max_age_scans * self._scan_time_s()
            scan_based = self.cfg.model.pv_source == "scan"
            for key, vals in fr.channels.items():
                ch = self.channels.get(key)
                if ch is None:
                    continue
                for res in ch.builder.feed(fr.boxes, vals, fr.ts):
                    if res.accepted:
                        sc = res.accepted
                        ch.pending_profile = sc
                        ch.rejected_last = False
                        self.counters.scans += 1
                        self.frame["ScanCount"] += 1
                        out.append(sc)
                        if scan_based:
                            ch.pv_seq += 1
                        log.debug("[SB-401] %s scan #%d dir %+d boxes %d-%d valid %d cov %.0f%% avg %.4f 2s %.4f",
                                  key, sc.seq, sc.direction, sc.first_box, sc.last_box, sc.valid_boxes,
                                  sc.coverage * 100, sc.avg, sc.sigma2)
                    else:
                        ch.rejected_last = True
                        log.info("[SB-402] %s scan rejected: %s", key, res.rejected_reason)
                ch.roll = ch.builder.rolling(fr.ts, max_age)
                ch.roll_ok = ch.roll.n > 0 and ch.roll.n >= self.cfg.model.min_coverage * self._roll_reference(ch)
                if not scan_based and ch.roll_ok:
                    ch.pv_seq += 1
                ch.live_seq += 1
                ch.live_dirty = True
            if fr.dropped:
                log.warning("[SB-211] TBM frame: channels dropped for length mismatch: %s", fr.dropped)
        return out

    def _roll_reference(self, ch: ChannelState) -> float:
        """Boxes a complete rolling profile should contain: span of the last accepted scan,
        or - before the first complete scan - the full NumberOfDataBoxes (conservative)."""
        last = ch.builder.last
        if last:
            return last.last_box - last.first_box + 1
        return ch.builder.expected_boxes

    def note_rejected_snapshot(self) -> None:
        with self.lock:
            self.counters.tbm_rejected += 1

    def check_stale(self, now: float) -> None:
        with self.lock:
            for ch in self.channels.values():
                if ch.builder.stale(now, max(30.0, 3 * self._scan_time_s())):
                    n = ch.builder.abort("stale")
                    log.info("[SB-403] %s: scan aborted (no boxes for a long time), %d boxes discarded", ch.key, n)

    def reset_profiles(self) -> None:
        with self.lock:
            for ch in self.channels.values():
                ch.builder.abort("reset")
                ch.builder.clear_live()
                ch.roll, ch.roll_ok, ch.live_dirty = None, False, True

    def tbm_age(self, now: float | None = None) -> float:
        if not self.last_tbm_rx:
            return float("inf")
        return (now or time.time()) - self.last_tbm_rx

    def stall_age(self, now: float | None = None) -> float:
        now = now or time.time()
        return now - max(self.last_tbm_rx, self.connected_at)

    # ================================================================== #
    def channel_quality(self, ch: ChannelState, now: float) -> int:
        q = 0
        if self.opc_connected:
            q |= 1 << 1
        if self.tbm_age(now) < self.stale_s:
            q |= 1 << 2
        if self.frame["Mode"] in (6, 7):
            q |= 1 << 3
        st = self.sensors.get(ch.sensor_key)
        if st and st.present and not st.v["StatusBad"]:
            q |= 1 << 4
        if not st or st.v["Shutter"] != 2:
            q |= 1 << 5
        if self.cfg.model.pv_source == "scan":
            last = ch.builder.last
            if last and now - last.epoch < 3 * max(self._scan_time_s(), last.duration_s or 0):
                q |= 1 << 6
        elif ch.roll_ok and ch.roll and now - ch.roll.epoch < self.stale_s:
            q |= 1 << 6
        if st and st.v["Status"] & 0x0100:
            q |= 1 << 7
        if st and st.v["Status"] & 0x0200:
            q |= 1 << 8
        if ch.rejected_last:
            q |= 1 << 9
        if self.frame["Mode"] == 5:
            q |= 1 << 10
        if ch.roll_ok:
            q |= 1 << 11
        if all(q & (1 << b) for b in (1, 2, 3, 4, 5, 6)) and ch.present:
            q |= 1
        return q

    def snapshot(self) -> dict:
        now = time.time()
        with self.lock:
            c = self.counters
            age = self.tbm_age(now)
            sts = {
                "State": self.state, "OPCConnected": int(self.opc_connected),
                "OPCServerState": self.opc_server_state, "NeedsSetup": int(self.needs_setup),
                "TBMRate": int(self._tbm_period),
                "TBMFrames": c.tbm_frames & 0x7FFFFFFF, "TBMRejected": c.tbm_rejected,
                "TBMGaps": c.tbm_gaps,
                "TBMAgeMs": int(min(age, 2_000_000) * 1000) if age != float("inf") else 2_000_000_000,
                "OPCReconnects": c.opc_reconnects, "WorkerRestarts": c.worker_restarts,
                "PLCWriteErrs": c.plc_write_errs, "LastError": c.last_error,
                "UptimeS": int(now - self.t_start), "Epoch": int(now),
            }
            f = dict(self.frame)
            if f["StdzEpoch"]:
                f["MinSinceStdz"] = round((now - f["StdzEpoch"]) / 60.0, 1)
            f["ChangeSeq"] = self.frame_seq & 0x7FFFFFFF
            if f["Mode"] < 0:
                f["Mode"] = 0
            sensors = {}
            for s in self.sensors.values():
                if s.slot >= 0:
                    d = dict(s.v)
                    d["ChangeSeq"] = s.seq
                    sensors[s.slot] = d
            chans = {}
            scan_based = self.cfg.model.pv_source == "scan"
            for ch in self.channels.values():
                b = ch.builder
                d = {"Present": int(ch.present), "Quality": self.channel_quality(ch, now),
                     "SensorSlot": self.sensors[ch.sensor_key].slot if ch.sensor_key in self.sensors else -1,
                     "Live": round(ch.eu(b.point_value), 6), "LiveSeq": ch.live_seq & 0x7FFFFFFF,
                     "PVSeq": ch.pv_seq & 0x7FFFFFFF, "Rejected": b.rejected}
                r = ch.roll
                if r and r.avg is not None:
                    d.update({"RollAvg": round(ch.eu(r.avg), 6), "Roll2Sigma": round(r.sigma2 * ch.scale, 6),
                              "RollMin": round(ch.eu(r.vmin), 6), "RollMax": round(ch.eu(r.vmax), 6)})
                d["RollBoxes"] = r.n if r else 0
                last = b.last
                if last:
                    d.update({"ScanAvg": round(ch.eu(last.avg), 6), "Scan2Sigma": round(last.sigma2 * ch.scale, 6),
                              "ScanMin": round(ch.eu(last.vmin), 6), "ScanMax": round(ch.eu(last.vmax), 6),
                              "ValidBoxes": last.valid_boxes, "FirstBox": last.first_box,
                              "LastBox": last.last_box, "Direction": last.direction,
                              "Coverage": round(last.coverage, 4), "ScanTime": round(last.duration_s, 2),
                              "ScanEpoch": int(last.epoch), "ScanSeq": last.seq})
                pv = d.get("ScanAvg") if scan_based else (d.get("RollAvg") if ch.roll_ok else None)
                if pv is not None:
                    d["PV"] = pv
                chans[ch.slot] = d
            return {"Sts": sts, "Frame": f, "Sensor": sensors, "Chan": chans}

    def take_profiles(self) -> list[tuple[int, int, list[float]]]:
        out = []
        with self.lock:
            for ch in self.channels.values():
                sc = ch.pending_profile
                if sc is not None:
                    prof = [None if v is None else ch.eu(v) for v in sc.profile]
                    out.append((ch.slot, sc.seq, fill(prof, self.cfg.model.profile_len, self.cfg.model.invalid_fill)))
                    ch.pending_profile = None
        return out

    def take_live_profiles(self) -> list[tuple[int, list[float]]]:
        out = []
        with self.lock:
            for ch in self.channels.values():
                if ch.live_dirty:
                    src = ch.roll.profile if ch.roll else []
                    prof = [None if v is None else ch.eu(v) for v in src]
                    out.append((ch.slot, fill(prof, self.cfg.model.profile_len, self.cfg.model.invalid_fill)))
                    ch.live_dirty = False
        return out

    def summary(self) -> dict:
        now = time.time()
        with self.lock:
            age = self.tbm_age(now)
            return {
                "state": STATE_NAMES[self.state], "opc_connected": self.opc_connected,
                "needs_setup": self.needs_setup, "tbm_rate_s": self._tbm_period,
                "tbm_age_s": None if age == float("inf") else round(age, 1),
                "frame_mode": cat.FRAME_MODE.get(self.frame["Mode"], self.frame["Mode"]),
                "head_pos": self.frame["HeadPos"], "scan_count": self.frame["ScanCount"],
                "active_alarms": cat.bits_to_names(self.frame["ActiveAlarms"], cat.ACTIVE_ALARM_BITS),
                "counters": vars(self.counters).copy(),
                "sensors": {k: {"slot": s.slot, "present": s.present, "status": f"0x{s.v['Status']:04X}",
                                "status_bits": cat.bits_to_names(s.v["Status"], s.stype.status_bits, 16),
                                "bad": bool(s.v["StatusBad"]), "shutter": ["n/a", "open", "closed"][s.v["Shutter"]]}
                            for k, s in self.sensors.items()},
                "channels": {k: {"slot": c.slot, "present": c.present,
                                 "quality": f"0x{self.channel_quality(c, now):03X}",
                                 "roll_avg": c.eu(c.roll.avg) if c.roll and c.roll.avg is not None else None,
                                 "roll_boxes": c.roll.n if c.roll else 0,
                                 "scan_avg": c.eu(c.builder.last.avg) if c.builder.last else None,
                                 "scan_seq": c.builder.last.seq if c.builder.last else 0,
                                 "scan_age_s": round(now - c.builder.last.epoch, 1) if c.builder.last else None,
                                 "rejected": c.builder.rejected, "aborted": c.builder.aborted}
                             for k, c in self.channels.items()},
            }


def _next_free(used: set[int], limit: int) -> int:
    for i in range(limit):
        if i not in used:
            return i
    return -1


def _s32(u: int) -> int:
    u &= 0xFFFFFFFF
    return u - 0x100000000 if u & 0x80000000 else u
