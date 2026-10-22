"""
Thin, defensive wrapper around pylogix.

* One PlcLink is owned by exactly one thread (pylogix is not thread-safe).
* Multi-tag reads/writes are chunked to fit the CIP connection size, so the same
  code works through a 1756-ENBT (508-byte connection, no Large Forward Open)
  and a 1756-EN2T (4002-byte Large Forward Open).
* Arrays are written in explicit chunks.
* FakeComm is an in-memory stand-in used by the simulator and tests.
"""
from __future__ import annotations

import re
import threading
from typing import Iterable

from . import logsetup

log = logsetup.get("plc")

CONN_FAIL_HINTS = ("connection", "socket", "timed out", "timeout", "forward open", "refused",
                   "unreachable", "reset", "broken pipe", "no route")


class PlcError(Exception):
    pass


class PlcConnectionError(PlcError):
    pass


class PlcLink:
    def __init__(self, cfg, comm_factory=None):
        self.cfg = cfg
        self._factory = comm_factory
        self.comm = None
        self.multi_ok = cfg.multi_write
        self.conn_size = cfg.connection_size or 508
        self.connected = False

    def open(self) -> None:
        self.close()
        if self._factory is not None:
            comm = self._factory()
        else:
            try:
                from pylogix import PLC
            except ImportError as e:   # pragma: no cover
                raise PlcError("pylogix is not installed (pip install pylogix)") from e
            comm = PLC()
        comm.IPAddress = self.cfg.ip
        comm.ProcessorSlot = self.cfg.slot
        if hasattr(comm, "SocketTimeout"):
            comm.SocketTimeout = self.cfg.socket_timeout_s
        if self.cfg.connection_size and hasattr(comm, "ConnectionSize"):
            comm.ConnectionSize = self.cfg.connection_size
        if self.cfg.route and hasattr(comm, "Route"):
            comm.Route = [tuple(int(x) for x in hop.split(",")) for hop in self.cfg.route.split(";") if hop]
        self.comm = comm
        probe = self.cfg.to_bridge_tag + ".ReqID"
        r = comm.Read(probe)
        status = getattr(r, "Status", "?")
        if status != "Success":
            self.close()
            if _is_conn_failure(status):
                raise PlcConnectionError(f"cannot reach PLC {self.cfg.ip} slot {self.cfg.slot}: {status}")
            raise PlcError(f"tag '{probe}' not readable: {status} - import the BRG_* UDTs and create "
                           f"tags {self.cfg.to_bridge_tag} / {self.cfg.from_bridge_tag}")
        cs = getattr(comm, "ConnectionSize", None)
        if isinstance(cs, int) and cs > 0:
            self.conn_size = cs
        self.connected = True

    def close(self) -> None:
        if self.comm is not None:
            try:
                self.comm.Close()
            except Exception:
                pass
        self.comm = None
        self.connected = False

    @property
    def array_chunk(self) -> int:
        if self.cfg.array_chunk:
            return self.cfg.array_chunk
        return 100 if self.conn_size <= 508 else 800

    def _budget(self) -> int:
        return max(200, self.conn_size - 90)

    def read(self, tags: list[str]) -> dict[str, object]:
        if self.comm is None:
            raise PlcConnectionError("not connected")
        out: dict[str, object] = {}
        bad = []
        for chunk in _chunks_by_size(tags, self._budget(), per_item=12):
            try:
                res = self.comm.Read(chunk) if len(chunk) > 1 else [self.comm.Read(chunk[0])]
            except Exception as e:
                raise PlcConnectionError(f"read failed: {e}") from e
            for r in _as_list(res):
                if r.Status == "Success":
                    out[r.TagName] = r.Value
                else:
                    bad.append((r.TagName, r.Status))
        if bad and not out and any(_is_conn_failure(s) for _, s in bad):
            raise PlcConnectionError(f"read failed: {bad[0][1]}")
        if bad:
            log.debug("read failures: %s", bad[:5])
        return out

    def write(self, pairs: list[tuple[str, object]]) -> list[tuple[str, str]]:
        if self.comm is None:
            raise PlcConnectionError("not connected")
        failed: list[tuple[str, str]] = []
        if not pairs:
            return failed
        if self.multi_ok:
            try:
                for chunk in _chunks_by_size(pairs, self._budget(), per_item=20, key=lambda p: p[0]):
                    res = self.comm.Write(chunk) if len(chunk) > 1 else [self.comm.Write(*chunk[0])]
                    for r in _as_list(res):
                        if r.Status != "Success":
                            failed.append((r.TagName, r.Status))
                return self._check(failed, len(pairs))
            except (TypeError, AttributeError, IndexError) as e:
                log.warning("[SB-304] pylogix multi-write unsupported (%s) - falling back to single writes", e)
                self.multi_ok = False
                failed.clear()
            except Exception as e:
                raise PlcConnectionError(f"write failed: {e}") from e
        for tag, val in pairs:
            try:
                r = self.comm.Write(tag, val)
            except Exception as e:
                raise PlcConnectionError(f"write failed: {e}") from e
            if r.Status != "Success":
                failed.append((tag, r.Status))
        return self._check(failed, len(pairs))

    def write_array(self, base: str, values: list, start: int = 0) -> list[tuple[str, str]]:
        failed = []
        n = self.array_chunk
        for i in range(0, len(values), n):
            tag = f"{base}[{start + i}]"
            try:
                r = self.comm.Write(tag, list(values[i:i + n]))
            except Exception as e:
                raise PlcConnectionError(f"array write failed: {e}") from e
            if r.Status != "Success":
                failed.append((tag, r.Status))
        return self._check(failed, max(1, len(values) // n))

    @staticmethod
    def _check(failed, total) -> list:
        if failed and len(failed) >= total and any(_is_conn_failure(s) for _, s in failed):
            raise PlcConnectionError(f"write failed: {failed[0][1]}")
        return failed


def _is_conn_failure(status: str) -> bool:
    s = str(status).lower()
    return any(h in s for h in CONN_FAIL_HINTS)


def _as_list(x):
    return x if isinstance(x, list) else [x]


def _chunks_by_size(items: list, budget: int, per_item: int, key=lambda x: x) -> Iterable[list]:
    chunk, size = [], 0
    for it in items:
        s = len(str(key(it))) + per_item
        if chunk and size + s > budget:
            yield chunk
            chunk, size = [], 0
        chunk.append(it)
        size += s
    if chunk:
        yield chunk


class _Resp:
    def __init__(self, tag, value, status="Success"):
        self.TagName, self.Value, self.Status = tag, value, status


_IDX = re.compile(r"^(.*)\[(\d+)\]$")


class FakeComm:
    """Shared dict-backed 'PLC' for bench tests."""
    store: dict[str, object] = {}
    lock = threading.Lock()
    strict = False
    online = True

    def __init__(self):
        self.IPAddress, self.ProcessorSlot, self.SocketTimeout, self.ConnectionSize = "", 0, 5.0, 508

    def _read1(self, tag, count=None):
        if not FakeComm.online:
            return _Resp(tag, None, "Connection failure")
        with FakeComm.lock:
            if count and count > 1:
                m = _IDX.match(tag)
                base, start = (m.group(1), int(m.group(2))) if m else (tag, 0)
                return _Resp(tag, [FakeComm.store.get(f"{base}[{start+i}]", 0) for i in range(count)])
            if tag not in FakeComm.store and FakeComm.strict:
                return _Resp(tag, None, "Path segment error")
            return _Resp(tag, FakeComm.store.get(tag, 0))

    def Read(self, tag, count=None, datatype=None):
        if isinstance(tag, list):
            return [self._read1(t) for t in tag]
        return self._read1(tag, count)

    def _write1(self, tag, value):
        if not FakeComm.online:
            return _Resp(tag, value, "Connection failure")
        with FakeComm.lock:
            if isinstance(value, list):
                m = _IDX.match(tag)
                base, start = (m.group(1), int(m.group(2))) if m else (tag, 0)
                for i, v in enumerate(value):
                    FakeComm.store[f"{base}[{start+i}]"] = v
            else:
                FakeComm.store[tag] = value
        return _Resp(tag, value)

    def Write(self, tag, value=None, datatype=None):
        if isinstance(tag, list):
            return [self._write1(t, v) for t, v in tag]
        return self._write1(tag, value)

    def Close(self):
        pass
