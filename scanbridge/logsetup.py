"""
Logging.

* One rotating log file (scanbridge.log); the OPC worker process forwards its
  records through a multiprocessing queue so only one process writes the file.
* WARNING and above optionally go to the Windows Application event log.
* Every significant message carries an ID such as [SB-204] - the
  Troubleshooting guide is indexed by these IDs.
* Rate-limited logging keeps repeating problems from flooding the file.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import time

FMT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(processName)-10s %(threadName)-10s %(name)s: %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"
ROOT = "scanbridge"


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"{ROOT}.{name}")


def setup_main(cfg) -> None:
    lc = cfg.logging
    log_dir = cfg.path(lc.dir)
    os.makedirs(log_dir, exist_ok=True)
    root = logging.getLogger(ROOT)
    root.setLevel(getattr(logging, lc.level.upper(), logging.INFO))
    root.handlers.clear()
    root.propagate = False
    fmt = logging.Formatter(FMT, DATEFMT)
    fh = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "scanbridge.log"), maxBytes=lc.max_mb * 1024 * 1024,
        backupCount=lc.backups, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if lc.console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        root.addHandler(ch)
    if lc.event_log and sys.platform == "win32":
        try:
            eh = logging.handlers.NTEventLogHandler(f"ScanBridge-{cfg.site.name}")
            eh.setLevel(logging.WARNING)
            eh.setFormatter(logging.Formatter("%(name)s: %(message)s"))
            root.addHandler(eh)
        except Exception as e:  # pragma: no cover
            root.warning("[SB-103] Windows event log handler unavailable (%s). "
                         "Run once as administrator to register the event source.", e)


def attach_listener(queue) -> logging.handlers.QueueListener:
    root = logging.getLogger(ROOT)
    lst = logging.handlers.QueueListener(queue, *root.handlers, respect_handler_level=True)
    lst.start()
    return lst


def setup_worker(queue, level: str) -> None:
    root = logging.getLogger(ROOT)
    root.handlers.clear()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(logging.handlers.QueueHandler(queue))
    root.propagate = False


class RateLimited:
    """Log the first occurrence, then at most once per `period` with a repeat count."""

    def __init__(self, logger: logging.Logger, period: float = 60.0):
        self.log = logger
        self.period = period
        self._state: dict[str, list] = {}

    def __call__(self, level: int, key: str, msg: str, *args) -> None:
        now = time.monotonic()
        st = self._state.get(key)
        if st is None:
            self._state[key] = [now, 0]
            self.log.log(level, msg, *args)
            return
        if now - st[0] >= self.period:
            suffix = f" (repeated {st[1]}x in last {self.period:.0f}s)" if st[1] else ""
            self.log.log(level, msg + suffix, *args)
            st[0], st[1] = now, 0
        else:
            st[1] += 1

    def clear(self, key: str) -> bool:
        return self._state.pop(key, None) is not None
