"""
OPC worker process - the ONLY code that touches DCOM.

Runs in a child process so a DCOM call that hangs (dead remote node) can be
killed and restarted by the supervisor without disturbing the model or PLC link.

Protocol (multiprocessing queues, tuples):
  worker -> core
    ("connecting", node)  ("discovering", node)  ("connected", info)  ("disconnected", reason)
    ("discovered", item_map, summary)            ("values", {canonical: (value, quality, ts)})
    ("tbm", TBMFrame)  ("tbm_reject", reason)    ("write_result", ticket, [(canonical, ok, err)])
  core -> worker
    ("write", ticket, [(canonical, value, dtype)])  ("rediscover",)  ("reconnect",)  ("stop",)

Groups on the AccuRay Object Server (one frame = opc.item_prefix):
  SB_TRIG   TBM5SecDataReadyTrigger (or 1S)  subscribed
  SB_TBM    head position, box numbers, every TBM5S array - active, NOT subscribed;
            read from CACHE as one snapshot when the trigger changes (never DEVICE)
  SB_STATE  status / setup / result scalars  subscribed (or polled)
  SB_WRITE  items added on first write       inactive
"""
from __future__ import annotations

import queue
import sys
import time

from . import catalog as cat
from . import discovery as disc
from . import logsetup
from .hresult import OPC_SERVER_STATE, describe, describe_com_error, quality_text
from .logsetup import RateLimited
from .tbm import SnapshotError, build_frame

log = logsetup.get("opc")
OPC_DS_CACHE = 1
OPC_FLAT = 2


class _Stop(Exception):
    pass


class _Restart(Exception):
    pass


def init_com(cfg) -> None:
    sys.coinit_flags = 2                     # COINIT_APARTMENTTHREADED - before pythoncom import
    import pythoncom
    if cfg.opc.init_security:
        try:
            pythoncom.CoInitializeSecurity(None, None, None, cfg.opc.auth_level, cfg.opc.impersonation_level,
                                           None, 0, None)
            log.info("[SB-216] CoInitializeSecurity authn=%d imp=%d", cfg.opc.auth_level, cfg.opc.impersonation_level)
        except Exception as e:
            log.warning("[SB-216] CoInitializeSecurity failed (%s) - OPC callbacks may be refused; "
                        "the worker falls back to polling if no events arrive", describe_com_error(e))


def main(cfg_dict: dict, to_core, to_worker, hb, log_q) -> None:
    from .config import Config
    cfg = Config.from_dict(cfg_dict)
    logsetup.setup_worker(log_q, cfg.logging.level)
    init_com(cfg)
    OpcWorker(cfg, to_core, to_worker, hb).run()


class _GroupEvents:
    worker = None
    kind = ""

    def OnDataChange(self, TransactionID, NumItems, ClientHandles, ItemValues, Qualities, TimeStamps):
        try:
            self.worker._on_change(self.kind, ClientHandles, ItemValues, Qualities, TimeStamps)
        except Exception:
            log.exception("[SB-299] error in DataChange handler")


def _epoch(ts) -> float:
    try:
        return ts.timestamp()
    except Exception:
        return 0.0


def _plain(v):
    if isinstance(v, tuple):
        return [_plain(x) for x in v]
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    try:
        return float(v)
    except Exception:
        return str(v)


class OpcWorker:
    def __init__(self, cfg, to_core, to_worker, hb):
        import pythoncom
        import win32com.client
        self.pythoncom = pythoncom
        self.win32 = win32com.client
        self.cfg = cfg
        self.oc = cfg.opc
        self.prefix = self.oc.item_prefix.strip().strip(".")
        self.to_core, self.to_worker, self.hb = to_core, to_worker, hb
        self.rate = self.oc.tbm_rate
        self.rl = RateLimited(log, 60)
        self.opc = None
        self.events_ok = False
        self.info = {}
        self._reset_state()

    def _reset_state(self):
        self.item_map: dict[str, str] = {}
        self.handles: dict[int, str] = {}
        self.sh: dict[str, int] = {}
        self.write_items: dict[str, tuple[int, int]] = {}
        self._next_ch = 1
        self.g_trig = self.g_tbm = self.g_state = self.g_write = None
        self._ev = []
        self.trig_key = f"{cat.FRAME_KEY}.frame.TBM{self.rate[0]}SecDataReadyTrigger"
        self.tbm_keys: list[str] = []
        self.tbm_sh: list[int] = []
        self.state_keys: list[str] = []
        self.last_trig = None
        self.last_trig_t = time.monotonic()
        self.pending_trig = None
        self.last_event_t = 0.0
        self.last_state_poll = 0.0
        self.last_health = time.monotonic()
        self.connected_t = time.monotonic()
        self.poll_probe = None
        self.use_events = False

    def touch(self):
        self.hb.value = time.time()

    def send(self, msg):
        try:
            self.to_core.put(msg)
        except Exception as e:
            log.error("cannot send to core: %s", e)

    def run(self) -> None:
        backoff = self.oc.reconnect_min_s
        while True:
            try:
                self.connect()
                self.discover()
                self.build_groups()
                backoff = self.oc.reconnect_min_s
                self.serve()
            except _Stop:
                break
            except _Restart as e:
                log.info("[SB-204] rebuilding OPC connection: %s", e)
                self.send(("disconnected", str(e)))
                self.teardown()
                continue
            except Exception as e:
                txt = describe_com_error(e) if type(e).__name__ == "com_error" else f"{type(e).__name__}: {e}"
                log.error("[SB-204] OPC connection to %s (%s) failed/lost: %s (retry in %.0f s)",
                          self.oc.node or "local", self.prefix, txt, backoff)
                self.send(("disconnected", txt))
            self.teardown()
            try:
                self.wait(backoff)
            except _Stop:
                break
            backoff = min(backoff * 2, self.oc.reconnect_max_s)
        self.teardown()
        log.info("[SB-200] OPC worker exiting")

    # ------------------------------------------------------------------ #
    def connect(self) -> None:
        self.touch()
        node = "local" if self.oc.is_local else self.oc.node
        self.send(("connecting", node))
        log.info("[SB-201] connecting to %s on %s (frame %s)", self.oc.prog_id, node, self.prefix or "?")
        try:
            self.opc = self.win32.gencache.EnsureDispatch(self.oc.automation_progid)
            self.events_ok = True
        except Exception as e:
            log.warning("[SB-209] makepy wrapper for %s unavailable (%s) - late binding; events disabled, "
                        "polling instead. Run: python -m win32com.client.makepy \"OPC DA Automation Wrapper 2.02\"",
                        self.oc.automation_progid, e)
            self.opc = self.win32.Dispatch(self.oc.automation_progid)
            self.events_ok = False
        t0 = time.monotonic()
        if self.oc.is_local:
            self.opc.Connect(self.oc.prog_id)
        else:
            self.opc.Connect(self.oc.prog_id, self.oc.node)
        self.touch()
        dt = time.monotonic() - t0
        state = int(self.opc.ServerState)
        info = {"node": node, "frame": self.prefix, "server_state": state, "connect_s": round(dt, 1)}
        for attr in ("ServerName", "VendorInfo", "MajorVersion", "MinorVersion", "BuildNumber"):
            try:
                info[attr] = str(getattr(self.opc, attr))
            except Exception:
                pass
        log.info("[SB-202] connected in %.1f s: %s", dt, info)
        if dt > 30:
            log.warning("[SB-203] DCOM connect took %.0f s - typical of multi-homed PCs (RPC binds to the wrong "
                        "NIC). See Troubleshooting: multi-NIC / RPC binding.", dt)
        if state != 1:
            raise RuntimeError(f"[SB-205] server state {state} {OPC_SERVER_STATE.get(state, '')} (expected RUNNING)")
        self.info = info

    # ------------------------------------------------------------------ #
    def discover(self) -> None:
        self.touch()
        self.send(("discovering", self.prefix))
        csv_path = self.cfg.path(self.oc.item_map_file)
        if self.oc.discovery == "file":
            items = disc.items_from_csv(csv_path)
            log.info("[SB-206] discovery mode 'file': %d items from %s", len(items), csv_path)
        else:
            items = self.browse()
            clf = disc.Classifier(self.cfg.discovery_patterns, self.prefix)
            for it in items:
                clf.classify(it)
            items = [i for i in items if i.how != "out-of-scope"]
            ov = disc.read_overrides(csv_path)
            if ov:
                disc.apply_overrides(items, ov)
                log.info("[SB-206] applied %d override row(s) from %s", len(ov), csv_path)
            dump = csv_path[:-4] + ".discovered.csv" if csv_path.endswith(".csv") else csv_path + ".discovered.csv"
            disc.write_csv(dump, items)
            log.info("[SB-206] browse of %s found %d items (%d classified) - list written to %s",
                     self.prefix or "(all)", len(items), sum(1 for i in items if i.canonical), dump)
        unclassified = [i for i in items if not i.canonical and i.how != "ignored"]
        if unclassified:
            log.info("[SB-208] %d items not classified (not needed, or add rows to %s). First: %s",
                     len(unclassified), csv_path, [i.item_id for i in unclassified[:5]])
        for k, ids in disc.duplicates(items).items():
            log.warning("[SB-208] '%s' matched %d items %s - using the first. Fix with an override row.", k, len(ids), ids[:3])
        self.item_map = disc.to_item_map(items)
        sensors = sorted({k.split(".")[0] for k in self.item_map})
        log.info("[SB-206] active components: %s", sensors)
        self.send(("discovered", dict(self.item_map), {"items": len(items), "sensors": sensors,
                                                       "browsed": [(i.item_id, i.canonical, i.how) for i in items]}))

    def browse(self) -> list[disc.BrowsedItem]:
        br = self.opc.CreateBrowser()
        items: list[disc.BrowsedItem] = []
        try:
            org = int(br.Organization)
        except Exception:
            org = 1
        if org == OPC_FLAT:
            br.ShowLeafs(True)
            for i in range(1, br.Count + 1):
                leaf = br.Item(i)
                iid = br.GetItemID(leaf)
                if disc.in_scope(iid, self.prefix):
                    items.append(disc.BrowsedItem(iid, [], iid.split(".")[-1]))
            log.info("[SB-206] flat address space: %d items in scope", len(items))
            return items

        props_on = [self.oc.read_item_properties, 0]

        def walk(path: list[str], depth: int):
            self.touch()
            br.ShowLeafs(False)
            for i in range(1, br.Count + 1):
                leaf = br.Item(i)
                iid = br.GetItemID(leaf)
                it = disc.BrowsedItem(iid, list(path), leaf)
                if props_on[0]:
                    it.properties = self._props(iid)
                    props_on[1] += 1
                    if props_on[1] >= 25 and not any(
                            disc.Classifier._device_tag_from_props(x.properties) for x in items[-24:] + [it]):
                        props_on[0] = False
                items.append(it)
            if depth >= self.oc.browse_max_depth:
                return
            br.ShowBranches()
            for b in [br.Item(i) for i in range(1, br.Count + 1)]:
                br.MoveDown(b)
                walk(path + [b], depth + 1)
                br.MoveUp()

        try:
            br.MoveToRoot()
        except Exception:
            pass
        base: list[str] = []
        if self.prefix:
            try:
                for part in self.prefix.split("."):
                    br.MoveDown(part)
                    base.append(part)
            except Exception as e:
                log.warning("[SB-206] cannot navigate to branch %s (%s) - browsing whole tree and filtering",
                            self.prefix, describe_com_error(e) if type(e).__name__ == "com_error" else e)
                br.MoveToRoot()
                base = []
        walk(base, 0)
        return [i for i in items if disc.in_scope(i.item_id, self.prefix)]

    def list_frames(self) -> list[str]:
        """MACHINE.FRAME branches two levels below the root (for 'scanbridge discover')."""
        br = self.opc.CreateBrowser()
        out = []
        br.MoveToRoot()
        br.ShowBranches()
        for m in [br.Item(i) for i in range(1, br.Count + 1)]:
            br.MoveDown(m)
            br.ShowBranches()
            out += [f"{m}.{br.Item(i)}" for i in range(1, br.Count + 1)]
            br.MoveUp()
        return out

    def _props(self, iid: str) -> dict:
        try:
            count, ids, descs, types = self.opc.QueryAvailableProperties(iid)
            want = [p for p in ids if p in (1, 5, 101) or p >= 5000]
            if not want:
                return {}
            vals, errs = self.opc.GetItemProperties(iid, len(want), [0] + list(want))
            return {int(p): _plain(v) for p, v, e in zip(want, vals, errs) if not e}
        except Exception:
            return {}

    # ------------------------------------------------------------------ #
    def build_groups(self) -> None:
        self.touch()
        groups = self.opc.OPCGroups
        groups.DefaultGroupIsActive = True
        groups.DefaultGroupDeadband = 0
        head = f"{cat.FRAME_KEY}.frame.TBM{self.rate}HeadPosition"
        box = f"{cat.FRAME_KEY}.frame.TBM{self.rate}DataBoxNumber"
        for k in (self.trig_key, head, box):
            if k not in self.item_map:
                raise RuntimeError(f"[SB-207] required item {k} not found under {self.prefix} - "
                                   f"check item_map.discovered.csv / overrides")
        chans = sorted(k for k in self.item_map if cat.channel_key_from_canonical(k, self.rate))
        state_keys = sorted(k for k in self.item_map if self._subscribe(k))
        tag = self.prefix.replace(".", "_") or "X"
        self.g_trig = self._group(groups, f"SB_TRIG_{tag}", self.oc.trigger_update_ms, [self.trig_key])
        self.g_tbm = self._group(groups, f"SB_TBM_{tag}", self.oc.trigger_update_ms, [head, box] + chans, subscribed=False)
        self.g_state = self._group(groups, f"SB_STATE_{tag}", self.oc.state_update_ms, state_keys)
        self.g_write = groups.Add(f"SB_WRITE_{tag}")
        self.g_write.IsActive = False
        self.g_write.IsSubscribed = False
        if self.trig_key not in self.sh or head not in self.sh or box not in self.sh:
            raise RuntimeError("[SB-207] could not add trigger / frame TBM items")
        self.tbm_keys = [k for k in [head, box] + chans if k in self.sh]
        self.tbm_sh = [self.sh[k] for k in self.tbm_keys]
        self.state_keys = [k for k in state_keys if k in self.sh]
        self.use_events = self.oc.mode == "events" and self.events_ok
        if self.use_events:
            for g, kind in ((self.g_trig, "trig"), (self.g_state, "state")):
                ev = self.win32.WithEvents(g, _GroupEvents)
                ev.worker, ev.kind = self, kind
                self._ev.append(ev)
        log.info("[SB-202] groups ready: trigger=%s, %d TBM items (%d channels), %d state items, mode=%s",
                 self.trig_key, len(self.tbm_keys), len(self.tbm_keys) - 2, len(self.state_keys),
                 "events" if self.use_events else "poll")
        self.send(("connected", dict(self.info, mode="events" if self.use_events else "poll")))
        self.last_trig_t = time.monotonic()
        self.connected_t = time.monotonic()
        self._poll_state()

    def _subscribe(self, key: str) -> bool:
        sensor, section, user = key.split(".", 2)
        if sensor == cat.FRAME_KEY:
            t = cat.frame_tag(user)
            return bool(t and t.subscribe)
        st = cat.SENSOR_TYPES.get(sensor[:2])
        t = st.tag_by_user(user, section) if st else None
        return bool(t and t.subscribe and t.readable)

    def _group(self, groups, name, rate, keys, subscribed=True):
        g = groups.Add(name)
        g.UpdateRate = rate
        g.IsActive = True
        g.IsSubscribed = subscribed
        if keys:
            items = g.OPCItems
            items.DefaultIsActive = True
            chs = []
            for k in keys:
                self.handles[self._next_ch] = k
                chs.append(self._next_ch)
                self._next_ch += 1
            ids = [self.item_map[k] for k in keys]
            server_handles, errors = items.AddItems(len(ids), [0] + ids, [0] + chs)
            for k, s, e in zip(keys, server_handles, errors):
                if e:
                    log.error("[SB-207] cannot add item %s (%s): %s", k, self.item_map[k], describe(e))
                else:
                    self.sh[k] = s
        return g

    # ------------------------------------------------------------------ #
    def serve(self) -> None:
        pc = self.pythoncom
        poll_s = self.oc.poll_ms / 1000.0
        stall_s = self.cfg.model.stall_s
        while True:
            self.touch()
            pc.PumpWaitingMessages()
            self._handle_core_msgs()
            now = time.monotonic()
            if not self.use_events:
                self._poll_trigger()
                if now - self.last_state_poll >= self.oc.state_update_ms / 1000.0:
                    self._poll_state()
            if self.pending_trig is not None:
                self._snapshot(*self.pending_trig)
                self.pending_trig = None
            if now - self.last_health >= self.oc.health_check_s:
                self.last_health = now
                st = int(self.opc.ServerState)
                if st != 1:
                    raise RuntimeError(f"[SB-205] server state {st} {OPC_SERVER_STATE.get(st, '')}")
                if self.use_events:
                    self._check_events_alive()
                if now - self.last_trig_t > stall_s:
                    raise RuntimeError(f"[SB-213] no TBM trigger change for {now - self.last_trig_t:.0f} s")
            time.sleep(poll_s if not self.use_events else 0.02)

    def _check_events_alive(self) -> None:
        """Callbacks blocked by DCOM security look like 'connected but silent'."""
        window = max(15.0, 3 * self.cfg.tbm_period_s)
        if time.monotonic() - self.connected_t < window or time.monotonic() - self.last_event_t < window:
            return
        v, e, q, t = self.g_trig.SyncRead(OPC_DS_CACHE, 1, [0, self.sh[self.trig_key]])
        if self.poll_probe is None:
            self.poll_probe = v[0]
            return
        if v[0] != self.poll_probe:
            log.warning("[SB-209] trigger changes but no DataChange callbacks arrive - DCOM callback permissions "
                        "on THIS PC block the server. Switching to polling. (Troubleshooting: events not arriving)")
            self.use_events = False

    def _handle_core_msgs(self) -> None:
        while True:
            try:
                msg = self.to_worker.get_nowait()
            except queue.Empty:
                return
            kind = msg[0]
            if kind == "stop":
                raise _Stop()
            if kind in ("rediscover", "reconnect"):
                raise _Restart(f"{kind} requested")
            if kind == "write":
                self._write(msg[1], msg[2])

    def _on_change(self, kind, handles, values, qualities, stamps) -> None:
        self.last_event_t = time.monotonic()
        if kind == "trig":
            for h, v, q, t in zip(handles, values, qualities, stamps):
                self._trigger(v, q, _epoch(t))
            return
        out = {}
        for h, v, q, t in zip(handles, values, qualities, stamps):
            k = self.handles.get(int(h))
            if k:
                out[k] = (_plain(v), int(q), _epoch(t))
        if out:
            self.send(("values", out))

    def _trigger(self, v, q, ts) -> None:
        if (int(q) & 0xC0) != 0xC0:
            self.rl(30, "trigq", "[SB-219] trigger quality %s", quality_text(q))
            return
        if v != self.last_trig:
            first = self.last_trig is None
            self.last_trig = v
            self.last_trig_t = time.monotonic()
            if not first:
                self.pending_trig = (v, ts)

    def _poll_trigger(self) -> None:
        v, e, q, t = self.g_trig.SyncRead(OPC_DS_CACHE, 1, [0, self.sh[self.trig_key]])
        self._trigger(v[0], q[0], _epoch(t[0]))

    def _poll_state(self) -> None:
        self.last_state_poll = time.monotonic()
        keys = self.state_keys
        if not keys:
            return
        v, e, q, t = self.g_state.SyncRead(OPC_DS_CACHE, len(keys), [0] + [self.sh[k] for k in keys])
        self.send(("values", {k: (_plain(a), int(b), _epoch(c)) for k, a, b, c in zip(keys, v, q, t)}))

    def _snapshot(self, trig_val, trig_ts) -> None:
        tol = self.oc.timestamp_tolerance_ms / 1000.0
        last_err = ""
        for attempt in range(self.oc.snapshot_retries):
            self.touch()
            v, e, q, t = self.g_tbm.SyncRead(OPC_DS_CACHE, len(self.tbm_sh), [0] + self.tbm_sh)
            data = dict(zip(self.tbm_keys, v))
            qual = dict(zip(self.tbm_keys, q))
            ts = {k: _epoch(x) for k, x in zip(self.tbm_keys, t)}
            bad = [k for k, x in qual.items() if (int(x) & 0xC0) != 0xC0]
            stale = [k for k, x in ts.items() if trig_ts and x and x < trig_ts - tol]
            try:
                if bad:
                    raise SnapshotError(f"bad quality on {bad[:3]} ({quality_text(qual[bad[0]])})")
                if stale:
                    raise SnapshotError(f"{len(stale)} item(s) not yet updated for this trigger, e.g. {stale[0]}")
                head, box = self.tbm_keys[0], self.tbm_keys[1]
                chans = {cat.channel_key_from_canonical(k, self.rate): _plain(data[k]) for k in self.tbm_keys[2:]}
                frame = build_frame(_plain(data[box]), _plain(data[head]), chans, ts, int(trig_val or 0),
                                    time.time(), tol)
                self.send(("tbm", frame))
                return
            except SnapshotError as err:
                last_err = str(err)
                end = time.monotonic() + self.oc.snapshot_retry_ms / 1000.0
                while time.monotonic() < end:
                    self.pythoncom.PumpWaitingMessages()
                    time.sleep(0.01)
        self.rl(30, "snap", "[SB-210] TBM snapshot rejected after %d tries: %s", self.oc.snapshot_retries, last_err)
        self.send(("tbm_reject", last_err))

    def _write(self, ticket: int, writes: list) -> None:
        results = []
        for key, value, dtype in writes:
            self.touch()
            try:
                if key not in self.item_map:
                    raise ValueError("item not on server")
                if key not in self.write_items:
                    sh, err = self.g_write.OPCItems.AddItems(1, [0, self.item_map[key]], [0, self._next_ch])
                    self._next_ch += 1
                    if err[0]:
                        raise ValueError(describe(err[0]))
                    item = self.g_write.OPCItems.GetOPCItem(sh[0])
                    self.write_items[key] = (sh[0], int(item.CanonicalDataType))
                sh, vt = self.write_items[key]
                try:
                    errs = self.g_write.SyncWrite(1, [0, sh], [0, self.win32.VARIANT(vt, value)])
                except TypeError:
                    errs = self.g_write.SyncWrite(1, [0, sh], [0, value])
                if errs and errs[0]:
                    raise ValueError(describe(errs[0]))
                results.append((key, True, ""))
            except Exception as e:
                txt = describe_com_error(e) if type(e).__name__ == "com_error" else str(e)
                results.append((key, False, txt))
        self.send(("write_result", ticket, results))

    def wait(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.touch()
            try:
                self.pythoncom.PumpWaitingMessages()
            except Exception:
                pass
            try:
                msg = self.to_worker.get(timeout=0.25)
                if msg[0] == "stop":
                    raise _Stop()
                if msg[0] == "write":
                    self.send(("write_result", msg[1], [(k, False, "OPC not connected") for k, _, _ in msg[2]]))
                if msg[0] in ("reconnect", "rediscover"):
                    return
            except queue.Empty:
                pass

    def teardown(self) -> None:
        self._ev.clear()
        try:
            if self.opc is not None:
                try:
                    self.opc.OPCGroups.RemoveAll()
                except Exception:
                    pass
                self.opc.Disconnect()
        except Exception:
            pass
        self.opc = None
        self._reset_state()
