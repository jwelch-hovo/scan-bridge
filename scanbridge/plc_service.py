"""
PLC thread: owns the pylogix connection.

Every cmd_poll_ms:   read <ToBridge> scalars -> PLC heartbeat check -> CommandProcessor.step()
Every scalar_period_ms:
    take completed + rolling profiles and snapshot under ONE model lock ->
    write profile arrays -> write changed scalars, *Seq members last
Every full_refresh_s / after reconnect: rewrite every scalar (PLC download safe)
"""
from __future__ import annotations

import threading
import time

from . import logsetup
from .commands import CommandProcessor
from .logsetup import RateLimited
from .plc_link import PlcConnectionError, PlcError, PlcLink
from .plc_types import VERSION_CODE

log = logsetup.get("plc")
TB_SCALARS = ["Heartbeat", "WriteEnable", "ReqID", "Code", "Target", "Tag", "ValueR", "ValueI"]


class PlcService(threading.Thread):
    def __init__(self, cfg, model, broker, store=None, comm_factory=None):
        super().__init__(name="plc", daemon=True)
        self.cfg = cfg
        self.pc = cfg.plc
        self.model = model
        self.link = PlcLink(cfg.plc, comm_factory)
        self.cmd = CommandProcessor(cfg, model, broker, store)
        self.stop_evt = threading.Event()
        self.beat = time.monotonic()
        self.rl = RateLimited(log, 60)
        self.tb = self.pc.to_bridge_tag
        self.fb = self.pc.from_bridge_tag
        self._cache: dict[str, object] = {}
        self._hb = 0
        self._plc_hb_val = None
        self._plc_hb_t = 0.0
        self.plc_hb_ok = False
        self._last_scalar = 0.0
        self._last_full = 0.0
        self._down_since: float | None = time.monotonic()
        self._ever_connected = False
        self._backoff = self.pc.reconnect_min_s
        self.connected = False

    def stop(self) -> None:
        self.stop_evt.set()

    def run(self) -> None:
        log.info("[SB-300] PLC service starting: %s slot %d, tags %s / %s, connection size %s",
                 self.pc.ip, self.pc.slot, self.tb, self.fb, self.pc.connection_size or "auto")
        while not self.stop_evt.is_set():
            self.beat = time.monotonic()
            if not self.link.connected:
                if not self._connect():
                    self.stop_evt.wait(self._backoff)
                    self._backoff = min(self._backoff * 2, self.pc.reconnect_max_s)
                    continue
            t0 = time.monotonic()
            try:
                self._cycle()
            except PlcConnectionError as e:
                self._lost(str(e))
                continue
            except Exception:
                log.exception("[SB-399] unexpected error in PLC cycle")
                self._lost("unexpected error")
                continue
            dt = time.monotonic() - t0
            self.stop_evt.wait(max(0.01, self.pc.cmd_poll_ms / 1000.0 - dt))
        self.link.close()
        log.info("[SB-300] PLC service stopped")

    def _connect(self) -> bool:
        try:
            self.link.open()
        except PlcError as e:
            self.rl(30, "plc-connect", "[SB-302] PLC connect failed: %s (retry in %.0f s)", e, self._backoff)
            if not isinstance(e, PlcConnectionError):
                self.model.counters.last_error = 303
            return False
        self.rl.clear("plc-connect")
        self._backoff = self.pc.reconnect_min_s
        outage = time.monotonic() - self._down_since if self._down_since else 0
        expire = (not self._ever_connected) or outage > self.pc.stale_command_s
        try:
            r = self.link.read([f"{self.tb}.ReqID", f"{self.fb}.Sts.CmdAck"])
            self.cmd.adopt(int(r.get(f"{self.tb}.ReqID", 0) or 0), int(r.get(f"{self.fb}.Sts.CmdAck", 0) or 0), expire)
            self._verify_layout()
        except PlcConnectionError as e:
            self._lost(str(e))
            return False
        log.info("[SB-301] PLC connected (%s, connection size %d, array chunk %d)%s", self.pc.ip,
                 self.link.conn_size, self.link.array_chunk,
                 f" after {outage:.0f} s outage" if self._ever_connected else "")
        self._ever_connected = True
        self._down_since = None
        self._cache.clear()
        self.connected = True
        return True

    def _verify_layout(self) -> None:
        m = self.cfg.model
        probes = [f"{self.fb}.Chan[{m.max_channels-1}].Profile[{m.profile_len-1}]",
                  f"{self.fb}.Chan[{m.max_channels-1}].LiveProfile[{m.profile_len-1}]",
                  f"{self.fb}.Sensor[{m.max_sensors-1}].ChangeSeq",
                  f"{self.tb}.Setup[{m.setup_items-1}].Value"]
        got = self.link.read(probes)
        missing = [p for p in probes if p not in got]
        if missing:
            log.error("[SB-303] PLC tag layout does not match config (cannot read %s). Regenerate/import the "
                      "UDTs with the same model.max_sensors / max_channels / profile_len / setup_items.", missing)
            self.model.counters.last_error = 303

    def _lost(self, why: str) -> None:
        if self.connected:
            log.warning("[SB-302] PLC connection lost: %s", why)
        self.connected = False
        self.link.close()
        self._down_since = self._down_since or time.monotonic()
        self.plc_hb_ok = False

    def _cycle(self) -> None:
        now_m = time.monotonic()
        vals = self.link.read([f"{self.tb}.{m}" for m in TB_SCALARS])
        tb = {t.rsplit(".", 1)[1]: v for t, v in vals.items()}
        self._check_plc_heartbeat(tb.get("Heartbeat"), now_m)
        if self.plc_hb_ok:
            self.cmd.step(tb, self._read_setup, time.time())
        if now_m - self._last_scalar >= self.pc.scalar_period_ms / 1000.0:
            self._last_scalar = now_m
            if now_m - self._last_full >= self.pc.full_refresh_s:
                self._last_full = now_m
                self._cache.clear()
            self._write_outputs()

    def _check_plc_heartbeat(self, v, now_m: float) -> None:
        if v is None:
            return
        if v != self._plc_hb_val:
            self._plc_hb_val, self._plc_hb_t = v, now_m
            if not self.plc_hb_ok:
                log.info("[SB-306] PLC heartbeat OK")
            self.plc_hb_ok = True
        elif self.plc_hb_ok and now_m - self._plc_hb_t > self.pc.plc_heartbeat_timeout_s:
            self.plc_hb_ok = False
            log.warning("[SB-305] PLC heartbeat (%s.Heartbeat) has not changed for %.0f s - PLC in program "
                        "mode or bridge logic not running. Commands are ignored until it resumes.",
                        self.tb, now_m - self._plc_hb_t)

    def _read_setup(self) -> list[dict]:
        n = self.cfg.model.setup_items
        names = ("Enable", "Target", "Tag", "Value")
        vals = self.link.read([f"{self.tb}.Setup[{i}].{m}" for i in range(n) for m in names])
        return [{m: vals.get(f"{self.tb}.Setup[{i}].{m}", 0) for m in names} for i in range(n)]

    def _write_outputs(self) -> None:
        with self.model.lock:
            profiles = self.model.take_profiles()
            lives = self.model.take_live_profiles() if self.pc.write_live_profile else []
            snap = self.model.snapshot()
        errs = []
        for slot, seq, prof in profiles:
            errs += self.link.write_array(f"{self.fb}.Chan[{slot}].Profile", prof)
        for slot, prof in lives:
            errs += self.link.write_array(f"{self.fb}.Chan[{slot}].LiveProfile", prof)
        cfg_arr = snap["Frame"].pop("ConfigArray")
        if self._cache.get("__cfgarr") != cfg_arr:
            errs += self.link.write_array(f"{self.fb}.Frame.ConfigArray", cfg_arr)
            self._cache["__cfgarr"] = list(cfg_arr)
        self._hb = (self._hb + 1) & 0x7FFFFFFF
        sts = snap["Sts"]
        sts.update(self.cmd.status())
        sts.update({"Heartbeat": self._hb, "PLCHbOK": int(self.plc_hb_ok), "Version": VERSION_CODE})
        flat: list[tuple[str, object]] = [(f"{self.fb}.Sts.{k}", v) for k, v in sts.items()]
        flat += [(f"{self.fb}.Frame.{k}", v) for k, v in snap["Frame"].items()]
        for slot, d in snap["Sensor"].items():
            flat += [(f"{self.fb}.Sensor[{slot}].{k}", v) for k, v in d.items()]
        for slot, d in snap["Chan"].items():
            flat += [(f"{self.fb}.Chan[{slot}].{k}", v) for k, v in d.items()]
        changed = [(t, _plc_val(v)) for t, v in flat if self._cache.get(t) != v or t.endswith(".Heartbeat")]
        changed.sort(key=lambda p: p[0].endswith("Seq"))          # stable: Seq members last
        if changed:
            errs += self.link.write(changed)
            failed = {t for t, _ in errs}
            for t, v in changed:
                if t not in failed:
                    self._cache[t] = v
        if errs:
            self.model.counters.plc_write_errs += len(errs)
            self.model.counters.last_error = 304
            self.rl(30, "plc-write", "[SB-304] %d PLC write(s) failed, e.g. %s = %s", len(errs), errs[0][0], errs[0][1])


def _plc_val(v):
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return max(-2147483648, min(2147483647, v))
    return v
