"""
PLC -> scanner command handling.

Handshake:
    PLC/HMI : load Code, Target, Tag, ValueR/ValueI, then ReqID := ReqID + 1
    bridge  : Sts.CmdBusy := ReqID ... executes ... Sts.CmdResult := r,
              Sts.CmdAck := ReqID, Sts.CmdBusy := 0
Execution is non-blocking: writes go to the OPC worker and the PLC thread keeps
writing heartbeats/data while it waits.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import catalog as cat
from . import logsetup
from .plc_types import CMD, MOTION_CMDS, RESULT

log = logsetup.get("cmd")
CT_COMPONENTS = ["Moisture", "Chalk", "Latex", "Clay"]


@dataclass
class Request:
    req_id: int
    code: int
    target: int
    tag: int
    value_r: float
    value_i: int


@dataclass
class Pending:
    req: Request | None
    ticket: int
    deadline: float
    writes: list = field(default_factory=list)


class CommandError(Exception):
    def __init__(self, result: int, msg: str, detail: int = 0):
        super().__init__(msg)
        self.result, self.detail = result, detail


class CommandProcessor:
    def __init__(self, cfg, model, broker, store=None):
        self.cfg = cfg
        self.model = model
        self.broker = broker
        self.store = store
        self.pending: Pending | None = None
        self.last_req: int | None = None
        self.ack = 0
        self.busy = 0
        self.result = 0
        self.detail = 0

    def adopt(self, req_id: int, ack_in_plc: int, expire: bool) -> None:
        if expire and req_id != ack_in_plc:
            log.warning("[SB-310] PLC request ReqID=%d was issued while the bridge was not connected - "
                        "discarded (result -9). Re-issue it if still wanted.", req_id)
            self.result = -9
        self.last_req = req_id
        self.ack = req_id if expire else ack_in_plc
        if not expire and req_id != ack_in_plc:
            self.last_req = ack_in_plc

    def status(self) -> dict:
        return {"CmdAck": self.ack, "CmdBusy": self.busy, "CmdResult": self.result, "CmdDetail": self.detail}

    def step(self, tb: dict, read_setup, now: float) -> None:
        if self.pending:
            self._poll_pending(now)
            return
        req_id = int(tb.get("ReqID", 0) or 0)
        if self.last_req is None:
            self.last_req = req_id
            return
        if req_id != self.last_req:
            req = Request(req_id, int(tb.get("Code", 0) or 0), int(tb.get("Target", 0) or 0),
                          int(tb.get("Tag", 0) or 0), float(tb.get("ValueR", 0.0) or 0.0),
                          int(tb.get("ValueI", 0) or 0))
            self.last_req = req_id
            self._start(req, int(tb.get("WriteEnable", 0) or 0), read_setup, now)
            return
        if (self.cfg.commands.auto_setup_on_connect and self.model.needs_setup and self.model.opc_connected
                and int(tb.get("WriteEnable", 0) or 0)):
            try:
                writes = self._setup_writes(read_setup())
            except CommandError as e:
                log.warning("[SB-311] automatic setup not sent: %s", e)
                self.model.needs_setup = False
                return
            if not writes:
                log.info("[SB-311] new scanner connection: setup table empty - nothing sent")
                self.model.needs_setup = False
                return
            log.info("[SB-311] new scanner connection: sending %d setup item(s) from PLC", len(writes))
            self.pending = Pending(None, self.broker.submit(writes), now + self.cfg.commands.timeout_s, writes)

    def _start(self, req: Request, write_enable: int, read_setup, now: float) -> None:
        log.info("[SB-307] request ReqID=%d code=%d (%s) target=%d tag=%d valueR=%g valueI=%d",
                 req.req_id, req.code, CMD.get(req.code, "?"), req.target, req.tag, req.value_r, req.value_i)
        self.busy = req.req_id
        try:
            if req.code in (81, 82, 83, 84):
                self._bridge_cmd(req)
                return self._finish(req, 1)
            if not write_enable:
                raise CommandError(-7, "ToBridge.WriteEnable = 0")
            if not self.model.opc_connected:
                raise CommandError(-4, "OPC not connected")
            writes = self._build(req, read_setup)
            self.pending = Pending(req, self.broker.submit(writes), now + self.cfg.commands.timeout_s, writes)
        except CommandError as e:
            log.warning("[SB-309] request ReqID=%d rejected: %s (%d %s)", req.req_id, e, e.result,
                        RESULT.get(e.result, ""))
            self._finish(req, e.result, e.detail, str(e))

    def _poll_pending(self, now: float) -> None:
        p = self.pending
        res = self.broker.poll(p.ticket)
        if res is None:
            if now > p.deadline:
                self.pending = None
                log.error("[SB-309] timeout waiting for OPC worker to execute write (ticket %d)", p.ticket)
                if p.req:
                    self._finish(p.req, -10)
            return
        self.pending = None
        failed = [(i, r) for i, r in enumerate(res) if not r[1]]
        for key, ok, err in res:
            (log.info if ok else log.error)("[SB-%s] OPC write %s = %s : %s", "214" if ok else "215", key,
                                            next((w[1] for w in p.writes if w[0] == key), "?"), "OK" if ok else err)
        if p.req is None:
            self.model.needs_setup = bool(failed)
            if failed:
                log.error("[SB-311] automatic setup: %d of %d items failed", len(failed), len(res))
            return
        if failed:
            self._finish(p.req, -5, failed[0][0] + 1, failed[0][1][2])
        else:
            if p.req.code == 80:
                self.model.needs_setup = False
            self._finish(p.req, 1)

    def _finish(self, req: Request, result: int, detail: int = 0, text: str = "") -> None:
        self.ack, self.busy, self.result, self.detail = req.req_id, 0, result, detail
        log.info("[SB-308] request ReqID=%d code=%d finished: %d %s %s", req.req_id, req.code, result,
                 RESULT.get(result, ""), text)
        if self.store:
            self.store.audit(req, result, text)

    def _bridge_cmd(self, req: Request) -> None:
        if req.code == 81:
            self.broker.control(("rediscover",))
        elif req.code == 82:
            self.model.reset_profiles()
        elif req.code == 83:
            self.broker.control(("reconnect",))
        elif req.code == 84:
            c = self.model.counters
            c.tbm_rejected = c.tbm_gaps = c.plc_write_errs = c.last_error = 0

    def _frame_guard(self, motion: bool) -> None:
        f = self.model.frame
        if motion and not self.cfg.commands.allow_frame_motion:
            raise CommandError(-7, "frame motion commands disabled in config (commands.allow_frame_motion)")
        if self.cfg.commands.respect_local_control:
            if int(f.get("DigitalInputs", 0)) & (1 << cat.DI_FRAME_LOCAL):
                raise CommandError(-8, "frame is in LOCAL")
            if int(f.get("WorkStnCtrl", 0)) == 1:
                raise CommandError(-8, "workstation is in control (WorkStnControlCode=1)")

    def _ws_guard(self) -> None:
        if self.cfg.commands.respect_local_control and int(self.model.frame.get("WorkStnCtrl", 0)) == 1:
            raise CommandError(-8, "workstation is in control (WorkStnControlCode=1)")

    def _key(self, key: str) -> str:
        if key not in self.model.item_map:
            raise CommandError(-3, f"item '{key}' not present on the OPC server")
        return key

    def _sensor(self, slot: int):
        for s in self.model.sensors.values():
            if s.slot == slot and s.present:
                return s
        raise CommandError(-2, f"no present sensor in slot {slot}")

    @staticmethod
    def _slope_guard(user: str, value: float) -> None:
        if user.startswith("Slope") and abs(value) < 1e-9:
            raise CommandError(-6, f"{user} = 0 would zero the measurement - refused")

    def _build(self, r: Request, read_setup) -> list[tuple[str, object, str]]:
        F = cat.FRAME_KEY + ".frame."
        c = r.code
        if 1 <= c <= 11:
            self._frame_guard(c in MOTION_CMDS)
            w = []
            if c == 4:
                if r.value_i not in (0, 1):
                    raise CommandError(-6, "standardize type must be 0 (dirty) or 1 (clean)")
                w.append((self._key(F + "SetNextStdzType"), r.value_i, "Character"))
            w.append((self._key(F + "Command"), c, "Long"))
            return w
        frame_set = {20: ("SetScanTime", "Long", 1, 999),
                     21: ("SetNumberOfDataBoxes", "Long", 1, self.cfg.model.profile_len),
                     22: ("SnglPtHeadPosition", "Single", None, None), 23: ("OffSheetHeadPosition", "Single", None, None),
                     24: ("SetSnglPtAvgTime", "Long", 20, 500), 25: ("SetScanMode", "Character", 0, 1),
                     26: ("SetReamSize", "Single", 0, None)}
        if c in frame_set:
            user, dt, lo, hi = frame_set[c]
            self._frame_guard(c in MOTION_CMDS)
            v = r.value_r if dt == "Single" else r.value_i
            if (lo is not None and v < lo) or (hi is not None and v > hi):
                raise CommandError(-6, f"{user} value {v} outside {lo}..{hi}")
            return [(self._key(F + user), v, dt)]
        if c in (27, 28):
            self._frame_guard(True)
            user = "HostCalibrateSample" if c == 27 else "OnSheetEnableFromHost"
            return [(self._key(F + user), 0, "Long")]
        if c in (40, 41, 42, 43, 44):
            self._ws_guard()
            s = self._sensor(r.target)
            base = f"{s.key}.{cat.MEAS}."
            if c in (40, 41):
                return [(self._key(base + ("SetOnline" if c == 40 else "SetOffline")), 0, "Long")]
            if c in (42, 43):
                user = "Slope" if c == 42 else "Offset"
                if s.stype.tt == "ct":
                    if not 0 <= r.tag < 4:
                        raise CommandError(-6, "coat weight component (Tag) must be 0..3")
                    user += CT_COMPONENTS[r.tag]
                self._slope_guard(user, r.value_r)
                return [(self._key(base + user), float(r.value_r), "Single")]
            td = s.stype.tag_by_user("SetSmplChkMode")
            if not td:
                raise CommandError(-3, f"{s.key} has no SetSmplChkMode")
            return [(self._key(base + "SetSmplChkMode"), r.value_i, td.dtype)]
        if c == 60:
            if not self.cfg.commands.allow_generic_write:
                raise CommandError(-7, "generic writes disabled in config")
            return [self._generic(r.target, r.tag, r.value_r, r.value_i)]
        if c == 80:
            w = self._setup_writes(read_setup())
            if not w:
                raise CommandError(-6, "setup table has no enabled items")
            return w
        raise CommandError(-1, f"unknown command code {c}")

    def _generic(self, target: int, yyy: int, vr: float, vi: int, value=None) -> tuple[str, object, str]:
        if target == -1:
            td = cat.frame_writable_by_yyy(yyy)
            if not td:
                raise CommandError(-3, f"frame has no writable tag sp1{yyy:03d}")
            if td.user == "Command":
                self._frame_guard(True)
            key = f"{cat.FRAME_KEY}.frame.{td.user}"
        else:
            s = self._sensor(target)
            td = s.stype.writable_by_yyy(yyy)
            if not td:
                raise CommandError(-3, f"{s.key} has no writable tag yyy={yyy}")
            key = f"{s.key}.{td.section}.{td.user}"
        if value is None:
            value = float(vr) if td.dtype in ("Single", "Double") else int(vi)
        elif td.dtype not in ("Single", "Double"):
            value = int(round(value))
        self._slope_guard(td.user, float(value))
        return (self._key(key), value, td.dtype)

    def _setup_writes(self, items: list[dict]) -> list[tuple[str, object, str]]:
        self._ws_guard()
        out = []
        for i, it in enumerate(items):
            if not int(it.get("Enable", 0) or 0):
                continue
            try:
                out.append(self._generic(int(it.get("Target", 0)), int(it.get("Tag", 0)), 0, 0,
                                         value=float(it.get("Value", 0.0))))
            except CommandError as e:
                raise CommandError(e.result, f"Setup[{i}]: {e}", i + 1) from e
        return out
