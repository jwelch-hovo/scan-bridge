"""
SQLite storage (optional history + audit trail).

tables
  scans      completed profiles per channel (ring buffer, storage.history_scans each)
  commands   every PLC request and its result (audit, storage.audit_days)
  discovery  last discovery result, compared after restarts
"""
from __future__ import annotations

import array
import json
import os
import sqlite3
import threading
import time

from . import logsetup

log = logsetup.get("store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans(
  id INTEGER PRIMARY KEY, channel TEXT, seq INTEGER, epoch REAL, duration REAL, direction INTEGER,
  first_box INTEGER, last_box INTEGER, valid INTEGER, coverage REAL, avg REAL, sigma2 REAL,
  vmin REAL, vmax REAL, profile BLOB);
CREATE INDEX IF NOT EXISTS ix_scans_ch ON scans(channel, epoch);
CREATE TABLE IF NOT EXISTS commands(
  id INTEGER PRIMARY KEY, epoch REAL, req_id INTEGER, code INTEGER, target INTEGER, tag INTEGER,
  value_r REAL, value_i INTEGER, result INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS discovery(epoch REAL, item_id TEXT, canonical TEXT, how TEXT);
"""


class Store:
    def __init__(self, cfg):
        self.cfg = cfg.storage
        path = cfg.path(self.cfg.db)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()
        self._n = 0

    def save_scan(self, sc) -> None:
        if self.cfg.history_scans <= 0:
            return
        blob = array.array("f", [float("nan") if v is None else v for v in sc.profile]).tobytes()
        try:
            with self.lock, self.db:
                self.db.execute("INSERT INTO scans(channel,seq,epoch,duration,direction,first_box,last_box,valid,"
                                "coverage,avg,sigma2,vmin,vmax,profile) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (sc.channel, sc.seq, sc.epoch, sc.duration_s, sc.direction, sc.first_box,
                                 sc.last_box, sc.valid_boxes, sc.coverage, sc.avg, sc.sigma2, sc.vmin, sc.vmax, blob))
                self._n += 1
                if self._n % 50 == 0:
                    self.db.execute("DELETE FROM scans WHERE channel=? AND id NOT IN (SELECT id FROM scans WHERE "
                                    "channel=? ORDER BY id DESC LIMIT ?)", (sc.channel, sc.channel, self.cfg.history_scans))
        except sqlite3.Error as e:
            log.warning("[SB-450] could not store scan: %s", e)

    def audit(self, req, result: int, detail: str = "") -> None:
        try:
            with self.lock, self.db:
                self.db.execute("INSERT INTO commands(epoch,req_id,code,target,tag,value_r,value_i,result,detail) "
                                "VALUES(?,?,?,?,?,?,?,?,?)", (time.time(), req.req_id, req.code, req.target, req.tag,
                                                              req.value_r, req.value_i, result, detail))
                self.db.execute("DELETE FROM commands WHERE epoch < ?", (time.time() - self.cfg.audit_days * 86400,))
        except sqlite3.Error as e:
            log.warning("[SB-450] could not store audit record: %s", e)

    def save_discovery(self, items) -> dict[str, str]:
        try:
            with self.lock, self.db:
                prev = {r[0] for r in self.db.execute("SELECT canonical FROM discovery WHERE canonical<>''")}
                self.db.execute("DELETE FROM discovery")
                now = time.time()
                self.db.executemany("INSERT INTO discovery VALUES(?,?,?,?)",
                                    [(now, i, c, h) for i, c, h in items])
            cur = {c for _, c, _ in items if c}
            if not prev:
                return {}
            diff = {k: "added" for k in cur - prev}
            diff.update({k: "removed" for k in prev - cur})
            return diff
        except sqlite3.Error as e:
            log.warning("[SB-450] could not store discovery: %s", e)
            return {}

    def recent_scans(self, channel: str, n: int = 10) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT seq,epoch,duration,direction,first_box,last_box,valid,coverage,avg,"
                                   "sigma2,vmin,vmax,profile FROM scans WHERE channel=? ORDER BY id DESC LIMIT ?",
                                   (channel, n)).fetchall()
        out = []
        for r in rows:
            prof = array.array("f")
            prof.frombytes(r[12])
            out.append({"seq": r[0], "epoch": r[1], "duration": r[2], "direction": r[3], "first_box": r[4],
                        "last_box": r[5], "valid": r[6], "coverage": r[7], "avg": r[8], "sigma2": r[9],
                        "min": r[10], "max": r[11], "profile": list(prof)})
        return out

    def close(self) -> None:
        with self.lock:
            self.db.close()


def dump_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)
    os.replace(tmp, path)
