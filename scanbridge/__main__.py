"""
Command line.

  python -m scanbridge run        -c site.toml             run the bridge (what the service executes)
  python -m scanbridge simulate   -c site.toml [--fake-plc] run against the built-in scanner simulator
  python -m scanbridge discover   -c site.toml             no item_prefix: list frames (PM8.SP1 ...)
                                                           with item_prefix: browse + classify that frame
  python -m scanbridge test-plc   -c site.toml             check PLC reachability + tag layout
  python -m scanbridge gen-l5x    -c site.toml -o plc      write the UDT import files
  python -m scanbridge gen-docs   -c site.toml             print UDT tables (markdown)
  python -m scanbridge status     -c site.toml             show status.json of the running bridge
  python -m scanbridge scans      -c site.toml --channel wt1.TBM [-n 5] [--csv out.csv]
  python -m scanbridge decode     --type wt --status 0x4040
  python -m scanbridge check      -c site.toml             validate the config file
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import sys
import time

from . import __version__
from . import catalog as cat
from . import config as config_mod
from . import logsetup


def _load(args, for_run=False):
    try:
        cfg = config_mod.load(args.config)
        if for_run:
            config_mod.validate(cfg, for_run=True)
        return cfg
    except config_mod.ConfigError as e:
        print(f"[SB-102] configuration error: {e}", file=sys.stderr)
        sys.exit(2)


def cmd_run(args, simulate=False) -> int:
    cfg = _load(args)
    if simulate:
        cfg.opc.simulate = True
        if args.fake_plc:
            cfg.plc.simulate = True
    try:
        config_mod.validate(cfg, for_run=True)
    except config_mod.ConfigError as e:
        print(f"[SB-102] configuration error: {e}", file=sys.stderr)
        return 2
    logsetup.setup_main(cfg)
    from .supervisor import Bridge
    return Bridge(cfg).run(duration=args.duration)


def cmd_discover(args) -> int:
    cfg = _load(args)
    cfg.logging.console = True
    logsetup.setup_main(cfg)
    if sys.platform != "win32":
        print("discover needs Windows + pywin32 (DCOM)")
        return 1
    import queue as q
    import types
    from .opc_worker import OpcWorker, init_com
    init_com(cfg)
    w = OpcWorker(cfg, q.Queue(), q.Queue(), types.SimpleNamespace(value=0.0))
    try:
        w.connect()
        if not w.prefix:
            frames = w.list_frames()
            print("\nFrames on this server (set [opc] item_prefix to one of these, one config per frame):")
            for f in frames:
                print(f"   {f}")
            return 0
        w.discover()
    finally:
        w.teardown()
    print(f"\n{len(w.item_map)} canonical items for {w.prefix}. Sensors: {sorted({k.split('.')[0] for k in w.item_map})}")
    chans = sorted({c for k in w.item_map if (c := cat.channel_key_from_canonical(k, cfg.opc.tbm_rate))})
    print(f"TBM{cfg.opc.tbm_rate} channels: {chans}")
    return 0


def cmd_test_plc(args) -> int:
    cfg = _load(args)
    logsetup.setup_main(cfg)
    from .plc_link import FakeComm, PlcError, PlcLink
    link = PlcLink(cfg.plc, FakeComm if cfg.plc.simulate else None)
    try:
        link.open()
    except PlcError as e:
        print(f"FAIL: {e}")
        return 1
    print(f"OK: connected to {cfg.plc.ip} slot {cfg.plc.slot}, connection size {link.conn_size}")
    tb, fb, m = cfg.plc.to_bridge_tag, cfg.plc.from_bridge_tag, cfg.model
    probes = [f"{tb}.Heartbeat", f"{tb}.ReqID", f"{tb}.Setup[{m.setup_items-1}].Value", f"{fb}.Sts.Heartbeat",
              f"{fb}.Sensor[{m.max_sensors-1}].ChangeSeq", f"{fb}.Chan[{m.max_channels-1}].Profile[{m.profile_len-1}]",
              f"{fb}.Chan[{m.max_channels-1}].LiveProfile[{m.profile_len-1}]"]
    got = link.read(probes)
    for p in probes:
        print(f"  {'OK     ' if p in got else 'MISSING'} {p} = {got.get(p, '-')}")
    t0 = time.time()
    fails = link.write_array(f"{fb}.Chan[{m.max_channels-1}].LiveProfile", [0.0] * m.profile_len)
    print(f"  profile write ({m.profile_len} REAL, chunk {link.array_chunk}): "
          f"{'OK' if not fails else fails[0]} in {1000*(time.time()-t0):.0f} ms")
    link.close()
    return 0 if len(got) == len(probes) and not fails else 1


def cmd_gen_l5x(args) -> int:
    cfg = _load(args)
    from .l5x import generate
    for f in generate(cfg, args.out, args.revision):
        print(f)
    return 0


def cmd_gen_docs(args) -> int:
    from .l5x import markdown_tables
    print(markdown_tables(_load(args)))
    return 0


def cmd_status(args) -> int:
    cfg = _load(args)
    p = cfg.path(cfg.supervisor.status_file)
    if not os.path.exists(p):
        print(f"no status file at {p} - is the bridge running?")
        return 1
    age = time.time() - os.path.getmtime(p)
    with open(p, encoding="utf-8") as fh:
        print(json.dumps(json.load(fh), indent=2))
    if age > 3 * cfg.supervisor.status_period_s:
        print(f"\nWARNING: status file is {age:.0f} s old - bridge may be stopped or hung")
        return 1
    return 0


def cmd_scans(args) -> int:
    cfg = _load(args)
    from .store import Store
    rows = Store(cfg).recent_scans(args.channel, args.n)
    if args.csv:
        import csv
        with open(args.csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["seq", "time", "avg", "2sigma", "valid", "first", "last", "dir"] +
                       [f"b{i+1}" for i in range(cfg.model.profile_len)])
            for r in rows:
                w.writerow([r["seq"], time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["epoch"])), r["avg"],
                            r["sigma2"], r["valid"], r["first_box"], r["last_box"], r["direction"]] + r["profile"])
        print(f"wrote {len(rows)} scans to {args.csv}")
    else:
        for r in rows:
            print(f"#{r['seq']:5d} {time.strftime('%H:%M:%S', time.localtime(r['epoch']))} avg {r['avg']:.4f} "
                  f"2s {r['sigma2']:.4f} boxes {r['first_box']}-{r['last_box']} valid {r['valid']} dir {r['direction']:+d}")
    return 0


def cmd_decode(args) -> int:
    v = int(args.status, 0)
    tables = {"frame-alarms": cat.ACTIVE_ALARM_BITS, "safety": cat.SAFETY_ALARM_BITS, "di": cat.DIGITAL_INPUT_BITS}
    if args.type in tables:
        print(cat.bits_to_names(v, tables[args.type]))
    elif args.type == "config":
        for e in cat.decode_config_array([v]):
            print(e, e.class_name, e.sensor_keys)
    elif args.type in cat.SENSOR_TYPES:
        st = cat.SENSOR_TYPES[args.type]
        print(f"{st.name}: {cat.bits_to_names(v, st.status_bits, 16)}  bad={bool(v & st.bad_mask)}")
    else:
        print("type must be a sensor prefix (wt, mo, ca, ac ...) or frame-alarms | safety | di | config")
        return 1
    return 0


def cmd_check(args) -> int:
    cfg = _load(args)
    try:
        config_mod.validate(cfg, for_run=True)
    except config_mod.ConfigError as e:
        print(f"WARNING (run would fail): {e}")
    print(f"OK - site '{cfg.site.name}', frame {cfg.opc.item_prefix or '(not set)'} on "
          f"{'local AOS' if cfg.opc.is_local else cfg.opc.node}, PLC {cfg.plc.ip}, TBM{cfg.opc.tbm_rate}, "
          f"PV={cfg.model.pv_source}, {len(cfg.sensor)} sensor slot(s), {len(cfg.channel)} channel slot(s)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="scanbridge", description=f"ScanBridge {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        p = sub.add_parser(name, help=help_)
        p.add_argument("-c", "--config", default="scanbridge.toml")
        p.set_defaults(fn=fn)
        return p
    p = add("run", cmd_run, "run the bridge")
    p.add_argument("--duration", type=float, default=None, help=argparse.SUPPRESS)
    p = add("simulate", lambda a: cmd_run(a, True), "run against the built-in scanner simulator")
    p.add_argument("--fake-plc", action="store_true", help="also use an in-memory PLC")
    p.add_argument("--duration", type=float, default=None)
    add("discover", cmd_discover, "list frames / browse and classify one frame")
    add("test-plc", cmd_test_plc, "check PLC connection and tag layout")
    p = add("gen-l5x", cmd_gen_l5x, "generate UDT L5X files")
    p.add_argument("-o", "--out", default="plc")
    p.add_argument("--revision", default="20.01", help="L5X SoftwareRevision attribute")
    add("gen-docs", cmd_gen_docs, "print UDT tables as markdown")
    add("status", cmd_status, "print status.json of the running bridge")
    p = add("scans", cmd_scans, "show stored scans")
    p.add_argument("--channel", required=True)
    p.add_argument("-n", type=int, default=10)
    p.add_argument("--csv")
    p = sub.add_parser("decode", help="decode status words")
    p.add_argument("--type", required=True)
    p.add_argument("--status", required=True)
    p.set_defaults(fn=cmd_decode)
    add("check", cmd_check, "validate config")
    args = ap.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
