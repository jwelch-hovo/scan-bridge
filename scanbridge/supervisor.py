"""
Supervisor: wires everything together and keeps it alive.

    main process
      MainThread  supervision loop (1 s): worker liveness + heartbeat, TBM stall,
                  thread watchdog, status.json
      rx          drains worker->core queue into the model / write broker
      plc         PlcService (pylogix connection, commands, outputs)
    child process
      OPC worker (DCOM)  or  simulator

Recovery layers
  1. OPC worker reconnects itself on any COM error (backoff 2..60 s)
  2. Supervisor kills + restarts the worker if its heartbeat stops (hung DCOM)
     or TBM data stays stalled after a reconnect request
  3. PLC thread reconnects itself (backoff 2..30 s)
  4. Internal thread dead/hung -> exit code 3 -> service manager restarts
"""
from __future__ import annotations

import itertools
import multiprocessing as mp
import os
import queue
import signal
import threading
import time

from . import logsetup
from .model import ST_CONNECTING, ST_DEGRADED, ST_DISCOVERING, ST_RUNNING, ScannerModel
from .plc_link import FakeComm
from .plc_service import PlcService
from .store import Store, dump_json

log = logsetup.get("sup")


class WriteBroker:
    def __init__(self, mgr: "WorkerManager"):
        self.mgr = mgr
        self._ids = itertools.count(1)
        self._results: dict[int, list] = {}
        self._open: set[int] = set()
        self.lock = threading.Lock()

    def submit(self, writes: list) -> int:
        t = next(self._ids)
        with self.lock:
            self._open.add(t)
        if not self.mgr.send(("write", t, writes)):
            self.complete(t, [(k, False, "OPC worker not running") for k, _, _ in writes])
        return t

    def complete(self, ticket: int, results: list) -> None:
        with self.lock:
            if ticket in self._open:
                self._open.discard(ticket)
                self._results[ticket] = results

    def poll(self, ticket: int):
        with self.lock:
            return self._results.pop(ticket, None)

    def control(self, msg: tuple) -> None:
        self.mgr.send(msg)

    def fail_all(self, why: str) -> None:
        with self.lock:
            for t in list(self._open):
                self._results[t] = [("?", False, why)]
            self._open.clear()


class WorkerManager:
    def __init__(self, cfg, model: ScannerModel):
        self.cfg = cfg
        self.model = model
        self.ctx = mp.get_context("spawn")
        self.proc = None
        self.hb = None
        self.rx_q = None
        self.tx_q = None
        self.log_q = None
        self.listener = None
        self.restarts = 0
        self.backoff = cfg.supervisor.restart_min_s
        self.next_start = 0.0
        self.started_at = 0.0
        self.reconnect_asked_at = 0.0
        self.lock = threading.Lock()

    @property
    def target(self):
        if self.cfg.opc.simulate:
            from .sim_worker import main
        else:
            from .opc_worker import main
        return main

    def start(self) -> None:
        with self.lock:
            self.rx_q, self.tx_q, self.log_q = self.ctx.Queue(), self.ctx.Queue(), self.ctx.Queue()
            self.hb = self.ctx.RawValue("d", time.time())
            if self.listener:
                self.listener.stop()
            self.listener = logsetup.attach_listener(self.log_q)
            self.proc = self.ctx.Process(target=self.target, name="opcworker",
                                         args=(self.cfg.to_dict(), self.rx_q, self.tx_q, self.hb, self.log_q),
                                         daemon=True)
            self.proc.start()
            self.started_at = time.monotonic()
            self.reconnect_asked_at = 0.0
        log.info("[SB-501] %s worker started (pid %s)", "SIMULATOR" if self.cfg.opc.simulate else "OPC", self.proc.pid)

    def send(self, msg) -> bool:
        with self.lock:
            if self.proc is None or not self.proc.is_alive():
                return False
            try:
                self.tx_q.put(msg)
                return True
            except Exception:
                return False

    def kill(self, why: str) -> None:
        with self.lock:
            p = self.proc
            if p is None:
                return
            if p.is_alive():
                try:
                    self.tx_q.put(("stop",))
                except Exception:
                    pass
                p.join(2.0)
                if p.is_alive():
                    p.terminate()
                    p.join(3.0)
                if p.is_alive() and hasattr(p, "kill"):
                    p.kill()
            self.proc = None
        log.warning("[SB-503] OPC worker stopped: %s", why)

    def check(self, now_m: float) -> None:
        sc = self.cfg.supervisor
        if self.proc is None:
            if now_m >= self.next_start:
                self.start()
            return
        if not self.proc.is_alive():
            code = self.proc.exitcode
            self.proc = None
            self._schedule_restart(now_m, f"OPC worker exited unexpectedly (exit code {code}) (SB-502)")
            return
        age = time.time() - self.hb.value
        if age > sc.hang_timeout_s:
            self.kill(f"heartbeat silent for {age:.0f} s (hung DCOM call?)")
            self._schedule_restart(now_m, "killed hung worker")
            return
        if self.model.opc_connected and self.model.stall_age() > self.cfg.model.stall_s:
            if not self.reconnect_asked_at:
                log.warning("[SB-504] no TBM data for %.0f s while connected - asking worker to reconnect",
                            self.model.stall_age())
                self.reconnect_asked_at = now_m
                self.send(("reconnect",))
            elif now_m - self.reconnect_asked_at > max(60.0, 2 * self.cfg.model.stall_s):
                self.kill("TBM still stalled after reconnect request (SB-504)")
                self._schedule_restart(now_m, "stall")
        elif self.model.stall_age() < self.cfg.model.stall_s:
            self.reconnect_asked_at = 0.0
        if now_m - self.started_at > sc.stable_reset_s:
            self.backoff = sc.restart_min_s

    def _schedule_restart(self, now_m: float, why: str) -> None:
        self.restarts += 1
        self.model.counters.worker_restarts = self.restarts
        self.model.set_connection(False)
        self.next_start = now_m + self.backoff
        log.error("[SB-507] %s - worker restart #%d in %.0f s", why, self.restarts, self.backoff)
        self.model.counters.last_error = 502
        self.backoff = min(self.backoff * 2, self.cfg.supervisor.restart_max_s)

    def stop(self) -> None:
        self.kill("shutdown")
        if self.listener:
            self.listener.stop()
            self.listener = None


class Bridge:
    def __init__(self, cfg, comm_factory=None):
        self.cfg = cfg
        self.model = ScannerModel(cfg)
        self.store = Store(cfg) if cfg.storage.db else None
        self.mgr = WorkerManager(cfg, self.model)
        self.broker = WriteBroker(self.mgr)
        if comm_factory is None and cfg.plc.simulate:
            comm_factory = FakeComm
        self.plc = PlcService(cfg, self.model, self.broker, self.store, comm_factory)
        self.stop_evt = threading.Event()
        self.rx_beat = time.monotonic()
        self.rx = threading.Thread(target=self._rx_loop, name="rx", daemon=True)
        self.last_status = 0.0

    def run(self, duration: float | None = None) -> int:
        log.info("[SB-101] ScanBridge starting - site '%s', frame %s on %s%s, PLC %s%s, TBM%s, PV=%s",
                 self.cfg.site.name, self.cfg.opc.item_prefix or "?",
                 "local AOS" if self.cfg.opc.is_local else self.cfg.opc.node,
                 " (SIMULATED)" if self.cfg.opc.simulate else "", self.cfg.plc.ip,
                 " (SIMULATED)" if self.cfg.plc.simulate else "", self.cfg.opc.tbm_rate, self.cfg.model.pv_source)
        self._signals()
        self.mgr.start()
        self.rx.start()
        self.plc.start()
        t_end = time.monotonic() + duration if duration else None
        rc = 0
        try:
            while not self.stop_evt.is_set():
                now_m = time.monotonic()
                self.mgr.check(now_m)
                self.model.check_stale(time.time())
                rc = self._watchdog(now_m)
                if rc:
                    break
                self._degraded()
                if now_m - self.last_status >= self.cfg.supervisor.status_period_s:
                    self.last_status = now_m
                    self._write_status()
                if t_end and now_m >= t_end:
                    break
                self.stop_evt.wait(1.0)
        finally:
            self.shutdown()
        return rc

    def shutdown(self) -> None:
        log.info("[SB-101] ScanBridge stopping")
        self.stop_evt.set()
        self.plc.stop()
        self.mgr.stop()
        self.plc.join(5)
        self._write_status()
        if self.store:
            self.store.close()

    def _signals(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return

        def handler(signum, frame):
            log.info("signal %s received", signum)
            self.stop_evt.set()
        for s in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
            if s is not None:
                try:
                    signal.signal(s, handler)
                except (ValueError, OSError):
                    pass

    def _watchdog(self, now_m: float) -> int:
        limit = self.cfg.supervisor.thread_watchdog_s
        for name, beat, alive in (("plc", self.plc.beat, self.plc.is_alive()), ("rx", self.rx_beat, self.rx.is_alive())):
            if not alive or now_m - beat > limit:
                log.critical("[SB-505] internal thread '%s' %s (last beat %.0f s ago)", name,
                             "died" if not alive else "is hung", now_m - beat)
                if self.cfg.supervisor.fatal_exit_on_hung_thread:
                    log.critical("[SB-506] exiting with code 3 so the service manager restarts ScanBridge")
                    return 3
        return 0

    def _degraded(self) -> None:
        m = self.model
        with m.lock:
            if m.opc_connected:
                ok = m.tbm_age() < m.stale_s and self.plc.connected
                m.state = ST_RUNNING if ok else ST_DEGRADED

    def _write_status(self) -> None:
        try:
            s = self.model.summary()
            s.update({"site": self.cfg.site.name, "frame": self.cfg.opc.item_prefix,
                      "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                      "plc_connected": self.plc.connected, "plc_heartbeat_ok": self.plc.plc_hb_ok,
                      "worker_pid": self.mgr.proc.pid if self.mgr.proc else None,
                      "worker_restarts": self.mgr.restarts, "pid": os.getpid()})
            dump_json(self.cfg.path(self.cfg.supervisor.status_file), s)
        except Exception as e:
            log.warning("[SB-508] cannot write status file: %s", e)

    def _rx_loop(self) -> None:
        while not self.stop_evt.is_set():
            self.rx_beat = time.monotonic()
            q = self.mgr.rx_q
            if q is None:
                time.sleep(0.2)
                continue
            try:
                msg = q.get(timeout=0.5)
            except queue.Empty:
                continue
            except (EOFError, OSError):
                time.sleep(0.5)
                continue
            try:
                self._dispatch(msg)
            except Exception:
                log.exception("[SB-599] error handling worker message %s", msg[0] if msg else msg)

    def _dispatch(self, msg) -> None:
        kind = msg[0]
        m = self.model
        if kind == "tbm":
            scans = m.apply_tbm(msg[1])
            if self.store:
                for sc in scans:
                    self.store.save_scan(sc)
        elif kind == "values":
            m.apply_values(msg[1])
        elif kind == "write_result":
            self.broker.complete(msg[1], msg[2])
        elif kind == "tbm_reject":
            m.note_rejected_snapshot()
        elif kind == "connecting":
            with m.lock:
                m.state = ST_CONNECTING
        elif kind == "discovering":
            with m.lock:
                m.state = ST_DISCOVERING
        elif kind == "discovered":
            m.configure(msg[1])
            if self.store and msg[2].get("browsed"):
                diff = self.store.save_discovery(msg[2]["browsed"])
                if diff:
                    log.warning("[SB-206] OPC items changed since last discovery: %s", diff)
        elif kind == "connected":
            m.set_connection(True, int(msg[1].get("server_state", 1)))
            log.info("[SB-202] scanner online (%s)", msg[1])
        elif kind == "disconnected":
            m.set_connection(False)
            self.broker.fail_all("OPC disconnected")
            m.counters.last_error = 204
