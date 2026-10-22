"""Regenerates docs/02_PLC_Interface.md from plc_types.py (run after changing types/codes)."""
import os, sys
ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
from scanbridge import catalog as cat
from scanbridge.config import Config
from scanbridge.l5x import markdown_tables
from scanbridge.plc_types import CMD, MOTION_CMDS, QUALITY_BITS, RESULT

ARGS = {
    4: ("–", "ValueI 0 dirty / 1 clean → SetNextStdzType, then Command 4"),
    20: ("–", "ValueI s (1–999) → SetScanTime"), 21: ("–", "ValueI 1–profile_len → SetNumberOfDataBoxes"),
    22: ("–", "ValueR → SnglPtHeadPosition"), 23: ("–", "ValueR → OffSheetHeadPosition"),
    24: ("–", "ValueI 20–500 ms → SetSnglPtAvgTime"), 25: ("–", "ValueI 0 continuous / 1 wait at EOS → SetScanMode"),
    26: ("–", "ValueR, 0 = g/m² → SetReamSize"), 27: ("–", "→ HostCalibrateSample"), 28: ("–", "→ OnSheetEnableFromHost"),
    40: ("sensor slot", "→ SetOnline"), 41: ("sensor slot", "→ SetOffline"),
    42: ("sensor slot", "ValueR (≠ 0); Tag = coat-weight component 0–3, otherwise ignored"),
    43: ("sensor slot", "ValueR; Tag as for 42"), 44: ("sensor slot", "ValueI → SetSmplChkMode"),
    60: ("−1 frame / sensor slot", "Tag = yyy of a writable device tag; ValueR for Single, ValueI otherwise"),
    80: ("–", "writes every Setup[] row with Enable = 1"), 81: ("–", "re-browse the OPC server"),
    82: ("–", "clear in-progress scans and the rolling profile"), 83: ("–", "drop and rebuild the OPC connection"),
    84: ("–", "zero TBMRejected / TBMGaps / PLCWriteErrs / LastError"),
}
FIX = {-2: "sensor not present in that slot – Sensor[n].Present", -3: "item not on the OPC server or not writable",
       -4: "scanner link down – Sts.OPCConnected, log SB-204", -5: "server refused – CmdDetail = failed row, log SB-215",
       -6: "fix the argument", -7: "set ToBridge.WriteEnable = 1 / config switch",
       -8: "frame in LOCAL or ABB workstation in control", -9: "re-issue the request",
       -10: "OPC worker busy or hung – log SB-309/SB-503", -1: "check Code"}

BODY = """
## 5. Channel quality (`Chan[n].Quality`)

{qtable}

**Use bit 0 as the PV-good signal for every loop.** It's set only when all of these hold:

* the OPC link is up and TBM data is fresh (< 13.5 s at TBM5S)
* the frame is scanning
* the sensor status has no bad bits
* the shutter is open
* the PV itself is fresh (rolling: enough fresh boxes; scan: last scan younger than three scan times)

## 6. Sensor TypeCode

{ttable}

`Sensor[n].StatusBad = Status AND bad_mask`. The default mask is 0xC007; for weight and ash it's 0xC047 (adds shutter closed). Decode any status word with `python -m scanbridge decode --type wt --status 0x4040`.

## 7. Setup table – who sets slopes and offsets

Per the ABB App Notes, the *host* should send the (M*) items on every connection and grade change. Your current software doesn't do this and the scanners measure correctly, so the frames (or the ABB workstation) already hold valid values. To avoid overwriting good settings with wrong ones, **ScanBridge writes nothing at connect by default** (`commands.auto_setup_on_connect = false`). It reads the values back: `Sensor[n].Slope` and `Sensor[n].Offset`.

When you want the PLC to own grade data:

1. Record the current `Sensor[n].Slope` / `Offset` readbacks for each grade.
2. Load them into `ToBridge.Setup[]` rows.
3. Issue code 80 on grade change. Optionally set `auto_setup_on_connect = true` so they're re-sent after every reconnect (`Sts.NeedsSetup` 1 → 0).

| Example row | Enable | Target | Tag | Value | Writes |
|---|---|---|---|---|---|
| Setup[0] | 1 | −1 | 3 | 20 | frame SetScanTime = 20 s |
| Setup[1] | 1 | −1 | 35 | 600 | frame SetNumberOfDataBoxes |
| Setup[2] | 1 | 0 | 2 | 1.000 | WT1 Slope (slot 0) |
| Setup[3] | 1 | 0 | 3 | 0.35 | WT1 Offset |
| Setup[4] | 1 | 1 | 10 | 45.0 | MO1 SetGradeWeightComp |

`Tag` is the `yyy` of the device tag (`TTxyyy`) in the *Scanning Platform Device Tags* manual.

## 8. Example logic (Structured Text)

### 8.1 Heartbeat, bridge watchdog, request helper

```iecst
// heartbeat to ScanBridge (1 s)
HB_TON.PRE := 1000; HB_TON.TimerEnable := NOT HB_TON.DN; TONR(HB_TON);
IF HB_TON.DN THEN PM8_SP1_ToBridge.Heartbeat := PM8_SP1_ToBridge.Heartbeat + 1; END_IF;

// ScanBridge alive? (Sts.Heartbeat changes every second)
IF PM8_SP1_FromBridge.Sts.Heartbeat <> BRG_LastHb THEN
    BRG_LastHb := PM8_SP1_FromBridge.Sts.Heartbeat; BRG_WD.Reset := 1;
ELSE BRG_WD.Reset := 0; END_IF;
BRG_WD.PRE := 5000; BRG_WD.TimerEnable := 1; TONR(BRG_WD);
BRG_Fault := BRG_WD.DN OR (PM8_SP1_FromBridge.Sts.OPCConnected = 0);

PM8_SP1_ToBridge.WriteEnable := ScannerWritesAllowed;

// request helper: HMI loads HMI_Code/Target/Tag/ValueR/ValueI then sets HMI_Go
IF HMI_Go AND NOT Req_Pending THEN
    PM8_SP1_ToBridge.Code   := HMI_Code;   PM8_SP1_ToBridge.Target := HMI_Target;
    PM8_SP1_ToBridge.Tag    := HMI_Tag;    PM8_SP1_ToBridge.ValueR := HMI_ValueR;
    PM8_SP1_ToBridge.ValueI := HMI_ValueI;
    IF PM8_SP1_ToBridge.ReqID >= 2147483000 THEN PM8_SP1_ToBridge.ReqID := 1;
    ELSE PM8_SP1_ToBridge.ReqID := PM8_SP1_ToBridge.ReqID + 1; END_IF;      // LAST
    Req_Pending := 1; HMI_Go := 0; Req_TO.Reset := 1;
END_IF;
Req_TO.PRE := 20000; Req_TO.TimerEnable := Req_Pending; TONR(Req_TO); Req_TO.Reset := 0;
IF Req_Pending AND PM8_SP1_FromBridge.Sts.CmdAck = PM8_SP1_ToBridge.ReqID THEN
    Req_Pending := 0; HMI_Result := PM8_SP1_FromBridge.Sts.CmdResult;
ELSIF Req_Pending AND Req_TO.DN THEN
    Req_Pending := 0; HMI_Result := -100;
END_IF;
```

### 8.2 Weight PV – new value every 5 s

```iecst
WT_Good := ((PM8_SP1_FromBridge.Chan[0].Quality AND 1) = 1) AND NOT BRG_Fault;
WT_New  := 0;
IF PM8_SP1_FromBridge.Chan[0].PVSeq <> WT_LastSeq THEN
    WT_LastSeq := PM8_SP1_FromBridge.Chan[0].PVSeq;
    IF WT_Good THEN
        WT_PV  := PM8_SP1_FromBridge.Chan[0].PV;   // rolling full-width average, g/m2
        WT_New := 1;                               // one-shot: execute the loop now
    END_IF;
END_IF;
// WT_Good = 0 -> hold the loop (Manual / freeze output per plant standard)
```

Run the PID **once per `WT_New`** (5 s), not every PLC scan. The rolling average contains boxes up to two scan times old, which is real process dead time. Tune for it: a slow integral, or use a dead-time compensator (PIDE plus the deadtime instruction, or a Smith predictor). If you use **PIDE**, its Real Time Sampling timing mode is meant for irregular analyzer-style updates; check `TimingMode` / `RTSTime` / `RTSTimeStamp` in Rockwell's *Process Control and Drives Instructions* manual for v20 before relying on it.

### 8.3 Periodic standardize (host responsibility, App Notes: 60 min default)

```iecst
IF StdzAutoEnable AND (PM8_SP1_FromBridge.Frame.MinSinceStdz >= StdzInterval_Min)
   AND (PM8_SP1_FromBridge.Frame.Scanning = 1) AND NOT Req_Pending AND NOT StdzRequested THEN
    HMI_Code := 4; HMI_Target := 0; HMI_Tag := 0; HMI_ValueI := 0; HMI_Go := 1; StdzRequested := 1;
END_IF;
IF PM8_SP1_FromBridge.Frame.MinSinceStdz < 1.0 THEN StdzRequested := 0; END_IF;
```

Check first whether the ABB workstation or the frame already standardizes periodically. If it does, leave `StdzAutoEnable` off. `MinSinceStdz` resets on *any* completed standardize.

### 8.4 HMI requirements (ABB App Notes)

* Any station with frame controls must show the basis weight / ash **shutter state** (`Sensor[n].Shutter`, `Frame.DigitalInputs` bit 0).
* Expose motion commands only on stations that can see the frame.

## 9. Data types
"""

def main():
    cfg = Config()
    out = [open(os.path.join(ROOT, "tools", "templates", "02_head.md"), encoding="utf-8").read()]
    out.append("| Code | Name | Target | Arguments / effect | Motion |\n|---|---|---|---|---|")
    for c, name in CMD.items():
        t, a = ARGS.get(c, ("–", ""))
        out.append(f"| {c} | {name.split(' (')[0]} | {t} | {a} | {'yes' if c in MOTION_CMDS else ''} |")
    out.append("\nCodes 1–11 are written to the frame's `Command` item (sp1001) unchanged.\n")
    out.append("## 4. Result codes (`Sts.CmdResult`)\n\n| Result | Meaning | Typical fix |\n|---|---|---|")
    out += [f"| {r} | {m} | {FIX.get(r, '')} |" for r, m in RESULT.items()]
    q = "| Bit | Meaning when SET |\n|---|---|\n" + "\n".join(f"| {b} | {m} |" for b, m in QUALITY_BITS.items())
    t = "| TypeCode | Item token | Sensor |\n|---|---|---|\n" + "\n".join(
        f"| {st.type_code} | {tt.upper()}x | {st.name} |" for tt, st in sorted(cat.SENSOR_TYPES.items(), key=lambda kv: kv[1].type_code))
    out.append(BODY.replace("{qtable}", q).replace("{ttable}", t))
    out.append(markdown_tables(cfg))
    with open(os.path.join(ROOT, "docs", "02_PLC_Interface.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    print("docs/02_PLC_Interface.md written")

if __name__ == "__main__":
    main()
