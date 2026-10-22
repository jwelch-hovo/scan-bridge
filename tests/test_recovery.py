"""
Recovery bench test (simulator + fake PLC):
  1. OPC worker heartbeat stops (hung DCOM call) -> supervisor kills + restarts it
  2. PLC link drops > stale_command_s            -> reconnect, request made meanwhile expires (-9)
    python tests/test_recovery.py config/simulate.toml
"""
import os, sys, threading, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scanbridge import config, logsetup
from scanbridge.plc_link import FakeComm
from scanbridge.supervisor import Bridge
S = FakeComm.store


def wait(cond, timeout, what):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return time.time() - t0
        time.sleep(0.2)
    raise AssertionError(f"timeout waiting for {what}")


def main(path):
    os.environ["SB_SIM_HANG_AFTER"] = "8"
    cfg = config.load(path)
    cfg.opc.simulate = cfg.plc.simulate = True
    cfg.logging.console = False
    cfg.supervisor.hang_timeout_s = 6
    cfg.supervisor.restart_min_s = 2
    cfg.plc.stale_command_s = 3
    TB, FB = cfg.plc.to_bridge_tag, cfg.plc.from_bridge_tag
    logsetup.setup_main(cfg)
    S.clear(); S.update({f"{TB}.WriteEnable": 1, f"{TB}.ReqID": 0})
    b = Bridge(cfg)
    stop = threading.Event()

    def plc():
        hb = 0
        while not stop.is_set():
            hb += 1; S[f"{TB}.Heartbeat"] = hb; time.sleep(1)
    threading.Thread(target=plc, daemon=True).start()
    threading.Thread(target=b.run, kwargs={"duration": 120}, daemon=True).start()
    wait(lambda: S.get(f"{FB}.Sts.OPCConnected") == 1, 20, "connect")
    os.environ["SB_SIM_HANG_AFTER"] = "0"          # respawned worker behaves
    print(f"hung worker killed/restarted after {wait(lambda: (S.get(f'{FB}.Sts.WorkerRestarts') or 0) >= 1, 40, 'restart'):.1f}s")
    f0 = S[f"{FB}.Sts.TBMFrames"]
    wait(lambda: S.get(f"{FB}.Sts.OPCConnected") == 1 and S[f"{FB}.Sts.TBMFrames"] > f0 + 2, 45, "data resumes")
    print("data flowing again, TBMFrames", S[f"{FB}.Sts.TBMFrames"])
    FakeComm.online = False
    time.sleep(1)
    S[f"{TB}.Code"], S[f"{TB}.ReqID"] = 3, 1
    time.sleep(5)
    FakeComm.online = True
    wait(lambda: S.get(f"{FB}.Sts.CmdAck") == 1, 20, "expired ack")
    print("request issued during outage -> result", S[f"{FB}.Sts.CmdResult"])
    assert S[f"{FB}.Sts.CmdResult"] == -9
    stop.set(); b.stop_evt.set(); time.sleep(3)
    print("RECOVERY PASS")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "config/simulate.toml")
