"""Builds docs/05_Troubleshooting.md (template + generated reference tables)."""
import os, sys
ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
from scanbridge.hresult import HRESULTS
from scanbridge.plc_types import QUALITY_BITS

CODES = [
 ("SB-101", "INFO", "Start / stop", "–"), ("SB-102", "ERROR", "Config invalid (exits)", "`scanbridge check`"),
 ("SB-103", "WARN", "Event log source not registered", "start once as administrator"),
 ("SB-200", "INFO", "OPC worker exiting", "–"), ("SB-201", "INFO", "Connecting", "–"),
 ("SB-202", "INFO", "Connected / groups ready / online", "–"), ("SB-203", "WARN", "Slow DCOM connect", "§2.1"),
 ("SB-204", "ERROR", "OPC connect failed / lost (HRESULT)", "§2.1"), ("SB-205", "ERROR", "Server not RUNNING", "§2.1"),
 ("SB-206", "INFO/WARN", "Discovery result / items changed", "§2.3"), ("SB-207", "ERROR", "Required item missing / cannot add", "§2.2"),
 ("SB-208", "INFO/WARN", "Unclassified or duplicate items", "§2.3"), ("SB-209", "WARN", "Events unavailable → polling", "§2.2"),
 ("SB-210", "WARN", "TBM snapshot rejected", "§2.2"), ("SB-211", "WARN", "TBM length mismatch", "§2.2"),
 ("SB-212", "WARN", "TBM refreshes missed", "§2.2"), ("SB-213", "ERROR", "TBM trigger stalled", "§2.2"),
 ("SB-214", "INFO", "OPC write OK", "–"), ("SB-215", "ERROR", "OPC write failed", "§3"),
 ("SB-216", "INFO/WARN", "CoInitializeSecurity", "if SB-209 follows, §2.2"),
 ("SB-217", "INFO", "Decoded sensor configuration array", "–"),
 ("SB-218", "INFO/WARN", "Sensor status / stdz / maintenance alarms", "decode bits; ABB maintenance"),
 ("SB-219", "WARN", "Bad trigger quality", "§2.2"), ("SB-299", "ERROR", "Exception in DataChange", "send log"),
 ("SB-300", "INFO", "PLC service start/stop", "–"), ("SB-301", "INFO", "PLC connected", "–"),
 ("SB-302", "WARN", "PLC connect failed / lost", "§3"), ("SB-303", "ERROR", "PLC layout mismatch", "§3"),
 ("SB-304", "WARN", "PLC writes failed", "§3"), ("SB-305", "WARN", "PLC heartbeat stale", "§3"),
 ("SB-306", "INFO", "PLC heartbeat OK", "–"), ("SB-307", "INFO", "Request received", "–"),
 ("SB-308", "INFO", "Request finished", "–"), ("SB-309", "WARN/ERROR", "Request rejected / timeout", "§3"),
 ("SB-310", "WARN", "Stale request discarded", "§3"), ("SB-311", "INFO/ERROR", "Automatic setup", "Operation §4"),
 ("SB-399", "ERROR", "Unexpected PLC-cycle error", "send log"),
 ("SB-401", "DEBUG", "Scan accepted", "–"), ("SB-402", "INFO", "Scan rejected (partial/span/coverage)", "§5"),
 ("SB-403", "INFO", "Scan aborted (mode change)", "normal"), ("SB-404", "ERROR", "NumberOfDataBoxes > profile_len", "raise profile_len, regenerate UDTs"),
 ("SB-405", "INFO/WARN", "Slot assignment / configured item absent", "§2.3"), ("SB-406", "INFO", "Mode / control / stdz complete", "–"),
 ("SB-407", "WARN/INFO", "Alarm / DI change, AEOS error", "check frame"), ("SB-408", "INFO", "Shutter open/closed", "–"),
 ("SB-450", "WARN", "SQLite write failed", "disk / permissions"),
 ("SB-501", "INFO", "Worker started", "–"), ("SB-502", "ERROR", "Worker exited", "repeated → support"),
 ("SB-503", "WARN", "Worker stopped (hung / shutdown)", "repeated → §2"), ("SB-504", "WARN", "TBM stalled → reconnect", "§2.2"),
 ("SB-505", "CRITICAL", "Internal thread died/hung", "send log"), ("SB-506", "CRITICAL", "Exit for service restart", "automatic"),
 ("SB-507", "ERROR", "Worker restart scheduled", "–"), ("SB-508", "WARN", "status.json not writable", "permissions"),
 ("SB-599", "ERROR", "Error handling worker message", "send log"),
]

def main():
    out = [open(os.path.join(ROOT, "tools", "templates", "05_head.md"), encoding="utf-8").read()]
    out.append("| ID | Level | Meaning | Action |\n|---|---|---|---|")
    out += [f"| {a} | {b} | {c} | {d} |" for a, b, c, d in CODES]
    out.append("\n## 5. Quality bits\n\n| Bit | Meaning when SET |\n|---|---|")
    out += [f"| {b} | {m} |" for b, m in QUALITY_BITS.items()]
    out.append("""
* **Bit 6 clear while scanning (rolling PV):** the rolling profile doesn't cover enough of the sheet yet. After start-up or a mode change this takes up to one full scan. Before the first complete scan, 80 % of NumberOfDataBoxes must be fresh.
* **Bit 4 clear:** run `scanbridge decode --type wt --status 0x....`.
* **Bit 5 clear:** shutter closed (normal off sheet / standardize).
* **Bit 9 set:** last completed scan rejected; see the SB-402 reason. `partial` once after start-up is normal.

## 6. ABB-side checks (AOS Troubleshooting Guide)

* Use **OPC Inspector** on the AOS PC to prove the AOS works without ScanBridge.
* AOS event-log messages to look for:
  * "Connected to '<ip>' using port 1827": frame link up.
  * "Waiting 10 seconds for tag reassignment": the AOS lost the frame.
  * `SP_ERR_BAD_TAG`: the Data Dictionary has a tag the frame doesn't know.
* The ASI dump file (`DefineDumpfile.reg`) is for short periods only, and needs an AOS restart.

## 7. HRESULT / OPC error reference
""")
    out.append("| Code | Name | Meaning / fix |\n|---|---|---|")
    out += [f"| 0x{c:08X} | {n} | {h} |" for c, (n, h) in HRESULTS.items()]
    out.append("""
## 8. Escalation package

1. `scanbridge status` output
2. `logs\\scanbridge.log` (+ rotated files)
3. `item_map.discovered.csv` and `item_map.csv`
4. The site `scanbridge.toml`
5. `scanbridge scans --channel wt1.TBM -n 20 --csv scans.csv`
6. A screenshot of `<frame>_FromBridge.Sts`

## 9. Bench reproduction

`scanbridge simulate -c config\\simulate.toml --fake-plc` runs everything without hardware.

* `set SB_SIM_HANG_AFTER=30`: the simulated worker hangs after 30 s.
* `set SB_SIM_STALL_AFTER=30`: it stops sending TBM data.

`tests\\test_e2e_sim.py` and `tests\\test_recovery.py` automate both.
""")
    with open(os.path.join(ROOT, "docs", "05_Troubleshooting.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    print("docs/05_Troubleshooting.md written")

if __name__ == "__main__":
    main()
