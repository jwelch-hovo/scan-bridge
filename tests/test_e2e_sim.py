"""
End-to-end bench test: simulator worker process (TBM5S) + in-memory PLC.
Acts as the PLC program (heartbeat, WriteEnable, setup table) and issues requests.

    python tests/test_e2e_sim.py config/simulate.toml
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scanbridge import config, logsetup  # noqa: E402
from scanbridge.plc_link import FakeComm  # noqa: E402
from scanbridge.supervisor import Bridge  # noqa: E402

S = FakeComm.store


def wait(cond, timeout, what):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return time.time() - t0
        time.sleep(0.2)
    raise AssertionError(f"timeout waiting for {what}")


def main(cfg_path):
    cfg = config.load(cfg_path)
    cfg.opc.simulate = cfg.plc.simulate = True
    cfg.commands.auto_setup_on_connect = True
    cfg.logging.console = False
    TB, FB = cfg.plc.to_bridge_tag, cfg.plc.from_bridge_tag
    logsetup.setup_main(cfg)

    def request(req, code, target=0, tag=0, vr=0.0, vi=0, timeout=20):
        S[f"{TB}.Code"], S[f"{TB}.Target"], S[f"{TB}.Tag"] = code, target, tag
        S[f"{TB}.ValueR"], S[f"{TB}.ValueI"] = vr, vi
        S[f"{TB}.ReqID"] = req
        wait(lambda: S.get(f"{FB}.Sts.CmdAck") == req, timeout, f"ack of req {req}")
        return S.get(f"{FB}.Sts.CmdResult")

    S.clear()
    S.update({f"{TB}.WriteEnable": 1, f"{TB}.ReqID": 0, f"{TB}.Heartbeat": 0, f"{FB}.Sts.CmdAck": 0})
    for i, (en, tg, tag, val) in enumerate([(1, -1, 3, 20.0), (1, 0, 3, 0.25)]):
        S[f"{TB}.Setup[{i}].Enable"], S[f"{TB}.Setup[{i}].Target"] = en, tg
        S[f"{TB}.Setup[{i}].Tag"], S[f"{TB}.Setup[{i}].Value"] = tag, val

    bridge = Bridge(cfg)
    stop = threading.Event()

    def plc_program():
        hb = 0
        while not stop.is_set():
            hb += 1
            S[f"{TB}.Heartbeat"] = hb
            time.sleep(1.0)
    threading.Thread(target=plc_program, daemon=True).start()
    bt = threading.Thread(target=bridge.run, kwargs={"duration": 200}, daemon=True)
    bt.start()
    C = f"{FB}.Chan[0]"
    r = {}
    try:
        print(f"connected after {wait(lambda: S.get(f'{FB}.Sts.OPCConnected') == 1, 20, 'OPC'):.1f}s, "
              f"TBMRate={S.get(f'{FB}.Sts.TBMRate')}")
        wait(lambda: S.get(f"{FB}.Sts.NeedsSetup") == 0 and S.get(f"{FB}.Sensor[0].Offset") == 0.25, 15, "auto setup")
        print("auto setup applied: wt1 offset readback", S[f"{FB}.Sensor[0].Offset"])
        t = wait(lambda: (S.get(f"{C}.PVSeq") or 0) >= 2, 90, "rolling PV")
        print(f"rolling PV valid after {t:.1f}s: PV={S[f'{C}.PV']:.3f} RollAvg={S[f'{C}.RollAvg']:.3f} "
              f"RollBoxes={S[f'{C}.RollBoxes']} Quality=0x{S[f'{C}.Quality']:03X} LiveSeq={S[f'{C}.LiveSeq']}")
        assert S[f"{C}.Quality"] & 1, "PV should be valid"
        live = [S.get(f"{C}.LiveProfile[{i}]", 0) for i in range(600)]
        print(f"LiveProfile: {sum(1 for x in live if x)} non-zero boxes")
        assert sum(1 for x in live if x) > 450
        pv1, seq1 = S[f"{C}.PV"], S[f"{C}.PVSeq"]
        wait(lambda: S[f"{C}.PVSeq"] > seq1, 12, "next PV update (5 s)")
        print(f"PV updated every TBM refresh: {pv1:.3f} -> {S[f'{C}.PV']:.3f}")
        t = wait(lambda: (S.get(f"{C}.ScanSeq") or 0) >= 1, 90, "first accepted scan")
        print(f"complete scan: ScanAvg={S[f'{C}.ScanAvg']:.3f} boxes {S[f'{C}.FirstBox']}-{S[f'{C}.LastBox']}")
        r["set_offset"] = request(1, 43, target=0, vr=1.0)
        wait(lambda: S.get(f"{FB}.Sensor[0].Offset") == 1.0, 5, "offset readback")
        r["slope_zero"] = request(2, 42, target=0, vr=0.0)
        r["unknown"] = request(3, 99)
        r["bad_slot"] = request(4, 40, target=5)
        r["offsheet"] = request(5, 1)
        wait(lambda: S.get(f"{FB}.Frame.Mode") == 0, 5, "mode off sheet")
        time.sleep(2)
        assert not S[f"{C}.Quality"] & 1, "PV must be invalid off sheet"
        r["scan"] = request(6, 3)
        S[f"{TB}.WriteEnable"] = 0
        r["write_disabled"] = request(7, 3)
        S[f"{TB}.WriteEnable"] = 1
        r["generic_scan_time"] = request(8, 60, target=-1, tag=3, vi=25)
        r["reset_profiles"] = request(9, 82)
        stop.set()
        wait(lambda: S.get(f"{FB}.Sts.PLCHbOK") == 0, 20, "PLC heartbeat loss")
        S[f"{TB}.ReqID"] = 10
        time.sleep(2)
        r["ignored_while_plc_hb_lost"] = S.get(f"{FB}.Sts.CmdAck") != 10
        print("results:", r)
        exp = {"set_offset": 1, "slope_zero": -6, "unknown": -1, "bad_slot": -2, "offsheet": 1, "scan": 1,
               "write_disabled": -7, "generic_scan_time": 1, "reset_profiles": 1, "ignored_while_plc_hb_lost": True}
        for k, v in exp.items():
            assert r[k] == v, f"{k}: expected {v} got {r[k]}"
        print("E2E PASS")
    finally:
        bridge.stop_evt.set()
        bt.join(15)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "config/simulate.toml")
