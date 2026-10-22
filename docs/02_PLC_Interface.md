# ScanBridge – PLC Interface Specification

Everything the PLC, the HMI and data-collection software need: data types, the request handshake, command and result codes, quality bits, and example logic. The tables at the end come from `scanbridge/plc_types.py`, which also generates the L5X files (Logix v20).

## 1. Tags

There's one pair of controller-scoped tags **per frame**. The UDTs are shared, so import them once per controller.

| Tag (example) | Type | Written by | Everyone else |
|---|---|---|---|
| `PM8_SP1_ToBridge` | `BRG_ToBridge` | PLC logic / HMI | – |
| `PM8_SP1_FromBridge` | `BRG_FromBridge` | ScanBridge | **read only** (PLC logic, HMI, data collection) |

* Every flag is a DINT; there are no BOOL members.
* Members ending in `Seq` are written **last** in each batch. Copy or latch data when a `Seq` changes and you'll never see a half-written profile.
* Array sizes come from the config: `max_sensors=8`, `max_channels=8`, `profile_len=600`, `setup_items=32`. Changing them means regenerating and re-importing the UDTs, which needs a download.

### For data-collection software

| Data | Read | Changes when |
|---|---|---|
| Rolling profile + average | `Chan[n].LiveProfile[0..599]`, `RollAvg`, `Roll2Sigma` | `Chan[n].LiveSeq` changes (every 5 s) |
| Completed-scan profile | `Chan[n].Profile[0..599]`, `ScanAvg`, `Scan2Sigma`, `FirstBox`, `LastBox` | `Chan[n].ScanSeq` changes |
| Data good? | `Chan[n].Quality` bit 0 (PV) / bit 11 (rolling coverage) | – |

Box *n* (1-based, as the scanner numbers it) is at array index *n−1*. Invalid boxes hold `invalid_fill` (0.0 by default).

## 2. Request handshake

1. Load `Code`, `Target`, `Tag`, `ValueR`, `ValueI`.
2. **Then** increment `ReqID`. ReqID is the trigger, so it goes last.
3. ScanBridge sets `Sts.CmdBusy = ReqID`, executes, writes `Sts.CmdResult` and `Sts.CmdDetail`, and sets **`Sts.CmdAck = ReqID`**.
4. The request is done when `Sts.CmdAck = ReqID`. `CmdResult`: 1 = OK, negative = error (§4).

Rules:

* One request at a time. Use a 20 s timeout in the PLC.
* `ToBridge.WriteEnable` must be 1, or every scanner write is rejected (−7).
* `ToBridge.Heartbeat` must change at least every 10 s. Otherwise requests are ignored (`Sts.PLCHbOK = 0`).
* A ReqID change made while the link has been down for more than 10 s is discarded on reconnect (−9). Requests are never replayed late.
* Frame commands are rejected (−8) when the frame is in LOCAL (`DigitalInputs` bit 7) or an ABB workstation is in control (`WorkStnCtrl = 1`).
* Writing a slope of 0 is refused (−6).
* **Mechanical safety (ABB App Notes):** only offer frame motion commands on stations that can see the frame.

## 3. Command codes (`ToBridge.Code`)

| Code | Name | Target | Arguments / effect | Motion |
|---|---|---|---|---|
| 1 | OffSheet | – |  | yes |
| 2 | SinglePoint | – |  | yes |
| 3 | Scan | – |  | yes |
| 4 | Standardize | – | ValueI 0 dirty / 1 clean → SetNextStdzType, then Command 4 | yes |
| 5 | AEOS Enable | – |  |  |
| 6 | AEOS Disable | – |  |  |
| 7 | Inherent Sheet Break Enable | – |  |  |
| 8 | Inherent Sheet Break Disable | – |  |  |
| 9 | Override Sheet Break | – |  | yes |
| 10 | Allow Sheet Break | – |  |  |
| 11 | Enable Scan | – |  | yes |
| 20 | Set Scan Time | – | ValueI s (1–999) → SetScanTime |  |
| 21 | Set Number Of Data Boxes | – | ValueI 1–profile_len → SetNumberOfDataBoxes |  |
| 22 | Set Single Point Position | – | ValueR → SnglPtHeadPosition | yes |
| 23 | Set Off Sheet Position | – | ValueR → OffSheetHeadPosition | yes |
| 24 | Set Single Point Avg Time | – | ValueI 20–500 ms → SetSnglPtAvgTime |  |
| 25 | Set Scan Mode | – | ValueI 0 continuous / 1 wait at EOS → SetScanMode |  |
| 26 | Set Ream Size | – | ValueR, 0 = g/m² → SetReamSize |  |
| 27 | Host Calibrate Sample | – | → HostCalibrateSample | yes |
| 28 | On Sheet Enable From Host | – | → OnSheetEnableFromHost | yes |
| 40 | Sensor Online | sensor slot | → SetOnline |  |
| 41 | Sensor Offline | sensor slot | → SetOffline |  |
| 42 | Set Slope | sensor slot | ValueR (≠ 0); Tag = coat-weight component 0–3, otherwise ignored |  |
| 43 | Set Offset | sensor slot | ValueR; Tag as for 42 |  |
| 44 | Set Sample Check Mode | sensor slot | ValueI → SetSmplChkMode |  |
| 60 | Generic Write | −1 frame / sensor slot | Tag = yyy of a writable device tag; ValueR for Single, ValueI otherwise |  |
| 80 | Send Setup Table | – | writes every Setup[] row with Enable = 1 |  |
| 81 | Rediscover OPC items | – | re-browse the OPC server |  |
| 82 | Reset Profiles | – | clear in-progress scans and the rolling profile |  |
| 83 | Reconnect OPC | – | drop and rebuild the OPC connection |  |
| 84 | Clear Counters | – | zero TBMRejected / TBMGaps / PLCWriteErrs / LastError |  |

Codes 1–11 are written to the frame's `Command` item (sp1001) unchanged.

## 4. Result codes (`Sts.CmdResult`)

| Result | Meaning | Typical fix |
|---|---|---|
| 0 | Idle |  |
| 1 | OK |  |
| -1 | Unknown command code | check Code |
| -2 | Bad Target (sensor slot / frame) | sensor not present in that slot – Sensor[n].Present |
| -3 | Tag not found or not writable | item not on the OPC server or not writable |
| -4 | OPC not connected | scanner link down – Sts.OPCConnected, log SB-204 |
| -5 | OPC write failed | server refused – CmdDetail = failed row, log SB-215 |
| -6 | Value out of range | fix the argument |
| -7 | Disabled (WriteEnable=0 or config) | set ToBridge.WriteEnable = 1 / config switch |
| -8 | Frame in Local / workstation in control | frame in LOCAL or ABB workstation in control |
| -9 | Expired (issued while bridge/PLC link was down) | re-issue the request |
| -10 | Timeout waiting for OPC worker | OPC worker busy or hung – log SB-309/SB-503 |
| -11 | Busy |  |

## 5. Channel quality (`Chan[n].Quality`)

| Bit | Meaning when SET |
|---|---|
| 0 | PV valid (bits 1-6 all set) |
| 1 | OPC connected |
| 2 | TBM data fresh |
| 3 | Frame scanning |
| 4 | Sensor status OK |
| 5 | Shutter open / not applicable |
| 6 | PV fresh (rolling: enough fresh boxes; scan: last scan < 3 scan times old) |
| 7 | Sensor standardizing |
| 8 | Sensor sample checking |
| 9 | Last scan rejected |
| 10 | Frame in single point |
| 11 | Rolling profile coverage OK |

**Use bit 0 as the PV-good signal for every loop.** It's set only when all of these hold:

* the OPC link is up and TBM data is fresh (< 13.5 s at TBM5S)
* the frame is scanning
* the sensor status has no bad bits
* the shutter is open
* the PV itself is fresh (rolling: enough fresh boxes; scan: last scan younger than three scan times)

## 6. Sensor TypeCode

| TypeCode | Item token | Sensor |
|---|---|---|
| 1 | WTx | Basis Weight |
| 2 | MOx | IR Moisture |
| 3 | MWx | Microwave Moisture |
| 4 | CAx | Caliper |
| 5 | ASx | Ash |
| 6 | XRx | X-Ray Weight |
| 7 | STx | Sheet Temperature |
| 8 | CTx | Coat Weight |
| 9 | RTx | IR Ratio |
| 10 | CWx | Conditioned Weight |
| 11 | APx | Apparent Density |
| 12 | ACx | Air Column |
| 13 | GLx | Gloss |
| 14 | SMx | Smoothness |
| 15 | FAx | Fiber Orientation Angle |
| 16 | FRx | Fiber Orientation Ratio |
| 17 | BRx | Brightness (OptiPak) |
| 18 | OPx | Opacity (OptiPak) |
| 19 | FOx | Formation (OptiPak) |
| 20 | COx | Color |
| 21 | UQx | Unique Sensor |

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

### BRG_SetupItem

One grade/connection setup value written to the scanner by command 80

| Member | Type | Description |
|---|---|---|
| `Enable` | DINT | 1 = send this item |
| `Target` | DINT | -1 = frame (sp1), 0..n = Sensor slot |
| `Tag` | DINT | Device tag data identifier yyy (e.g. 3 = Offset, 2 = Slope; frame 3 = SetScanTime) |
| `Value` | REAL | Value (REAL for Single items, rounded for integer items) |

### BRG_Status

Bridge health and command handshake

| Member | Type | Description |
|---|---|---|
| `Heartbeat` | DINT | Increments every write cycle (1 s) |
| `State` | DINT | 0 stopped 1 starting 2 connecting 3 discovering 4 running 5 degraded 6 reconnecting |
| `OPCConnected` | DINT | 1 = connected to AccuRay Object Server |
| `OPCServerState` | DINT | 1 RUNNING 2 FAILED 3 NOCONFIG 4 SUSPENDED 5 TEST |
| `PLCHbOK` | DINT | 1 = PLC heartbeat seen within timeout |
| `CmdAck` | DINT | ReqID of the last completed request |
| `CmdBusy` | DINT | ReqID currently executing (0 = idle) |
| `CmdResult` | DINT | Result of last request: 1 OK, <0 error |
| `CmdDetail` | DINT | Extra info (failed setup row, ...) |
| `NeedsSetup` | DINT | 1 = scanner (re)connected and setup table not sent since |
| `TBMRate` | DINT | TBM refresh period in seconds (1 or 5) |
| `TBMFrames` | DINT | TBM frames accepted (rolls over) |
| `TBMRejected` | DINT | TBM snapshots rejected |
| `TBMGaps` | DINT | Missed TBM refreshes |
| `TBMAgeMs` | DINT | Age of last TBM frame, ms |
| `OPCReconnects` | DINT | OPC (re)connections since start |
| `WorkerRestarts` | DINT | OPC worker process restarts since start |
| `PLCWriteErrs` | DINT | Failed PLC writes since start |
| `LastError` | DINT | Last log message ID number (e.g. 204 for SB-204) |
| `Version` | DINT | Bridge version code |
| `UptimeS` | DINT | Seconds since bridge start |
| `Epoch` | DINT | Bridge PC clock, Unix seconds |

### BRG_Frame

Smart Platform frame state

| Member | Type | Description |
|---|---|---|
| `Mode` | DINT | 0 OffSheet 1 SnglPtReq 2 ScanReq 4 StdzReq 5 SinglePoint 6 Scan 7 AtEOS 8 Standardize 9 Local 10 ReadyToScan |
| `Scanning` | DINT | 1 = Mode 6 or 7 |
| `ScanTime` | DINT | Scan period s |
| `NumBoxes` | DINT | Number of data boxes |
| `DataBoxWidth` | REAL | Customer units |
| `HeadPos` | REAL | Last head position from TBM |
| `Direction` | DINT | +1 forward, -1 reverse, 0 not scanning |
| `HomeEOS` | REAL |  |
| `FarEOS` | REAL |  |
| `HomeScanLimit` | REAL |  |
| `FarScanLimit` | REAL |  |
| `HomeMeasLimit` | REAL |  |
| `FarMeasLimit` | REAL |  |
| `SnglPtPos` | REAL |  |
| `OffSheetPos` | REAL |  |
| `ActiveAlarms` | DINT | sp1004 bit field |
| `SafetyAlarms` | DINT | sp1052 bit field |
| `DigitalInputs` | DINT | sp1041 (bit0 shutter open, bit2 sheet break, bit7 frame local) |
| `WorkStnCtrl` | DINT | 0 host in control, 1 workstation in control |
| `AEOSStatus` | DINT | 1 enabled |
| `AEOSError` | DINT | sp1057 |
| `SheetBreak` | DINT | Sheet break indicator |
| `CalSmplStatus` | DINT |  |
| `StdzEpoch` | DINT | Unix s of last completed standardize |
| `MinSinceStdz` | REAL | Minutes since last standardize |
| `ScanCount` | DINT | Completed scans since start |
| `ConfigArray` | DINT[20] | sp1044 sensor configuration array (raw) |
| `ChangeSeq` | DINT | Increments when any frame value changes (written last) |

### BRG_Sensor

One scanner sensor (wt1, mo1 ...)

| Member | Type | Description |
|---|---|---|
| `Present` | DINT | 1 = discovered on the OPC server |
| `TypeCode` | DINT | 1 wt 2 mo 3 mw 4 ca 5 as ... 12 ac (see spec) |
| `Instance` | DINT | Instance x of TTxyyy |
| `Status` | DINT | Raw sensor status word |
| `StatusBad` | DINT | Status AND bad-mask (non-zero = PV not trustworthy) |
| `Shutter` | DINT | 0 n/a, 1 open, 2 closed |
| `Slope` | REAL | Slope read back |
| `Offset` | REAL | Offset read back |
| `StdzWaitTime` | DINT | Minutes before sensor requests standardize |
| `StdzStatus` | DINT | Last standardize status (0 = PASSED) |
| `StdzEpoch` | DINT | Last standardize time, Unix s |
| `SmplChkStatus` | DINT |  |
| `SmplChkEpoch` | DINT |  |
| `CalSmplStatus` | DINT |  |
| `CalSmplEpoch` | DINT |  |
| `MaintAlarms` | DINT | Maintenance alarm codes as bits (bit n = code 0x40+n) |
| `ChangeSeq` | DINT | Increments on any change (written last) |

### BRG_Channel

One measurement channel (one TBM array) and its CD profiles

| Member | Type | Description |
|---|---|---|
| `Present` | DINT | 1 = TBM array discovered |
| `SensorSlot` | DINT | Sensor[] slot this channel belongs to |
| `Quality` | DINT | Bit field - bit0 = PV valid (see spec) |
| `PV` | REAL | Process variable for PID (RollAvg or ScanAvg per config pv_source) |
| `Live` | REAL | Mean of valid elements of the latest TBM refresh (partial width) |
| `RollAvg` | REAL | Full-width average of the rolling profile - updated every TBM refresh |
| `Roll2Sigma` | REAL | 2 sigma of the rolling profile |
| `RollMin` | REAL |  |
| `RollMax` | REAL |  |
| `RollBoxes` | DINT | Valid (fresh) boxes in the rolling profile |
| `ScanAvg` | REAL | Average of the last accepted complete scan |
| `Scan2Sigma` | REAL |  |
| `ScanMin` | REAL |  |
| `ScanMax` | REAL |  |
| `ValidBoxes` | DINT | Valid boxes in last scan |
| `FirstBox` | DINT | First box (1-based) of last scan |
| `LastBox` | DINT | Last box (1-based) of last scan |
| `Direction` | DINT | +1 / -1 direction of last scan |
| `Coverage` | REAL | Valid / spanned boxes of last scan (0..1) |
| `ScanTime` | REAL | Duration of last scan, s |
| `ScanEpoch` | DINT | Completion time of last scan, Unix s |
| `Rejected` | DINT | Scans rejected for partial/low coverage/span |
| `Profile` | REAL[600] | Last accepted complete-scan profile, box n at index n-1 |
| `LiveProfile` | REAL[600] | Rolling profile (latest fresh value per box), updated every TBM refresh |
| `LiveSeq` | DINT | Increments every TBM refresh: Live/Roll*/LiveProfile updated (written last) |
| `PVSeq` | DINT | Increments when PV updates (written last) |
| `ScanSeq` | DINT | Increments when ScanAvg/Profile update (written last) |

### BRG_ToBridge

PLC/HMI -> ScanBridge. Requests execute only when ReqID changes.

| Member | Type | Description |
|---|---|---|
| `Heartbeat` | DINT | PLC increments every second |
| `WriteEnable` | DINT | 1 = bridge may write to the scanner; 0 = every command rejected (-7) |
| `ReqID` | DINT | Increment (after loading Code/Target/Tag/Value) to request a command |
| `Code` | DINT | Command code - see PLC Interface spec |
| `Target` | DINT | -1 = frame, 0..n = Sensor slot |
| `Tag` | DINT | Command qualifier: yyy for generic write, component for slope/offset |
| `ValueR` | REAL | REAL argument |
| `ValueI` | DINT | Integer argument |
| `Setup` | BRG_SetupItem[32] | Grade/connection setup table |

### BRG_FromBridge

ScanBridge -> PLC/HMI/data collection. Read only for everyone but the bridge.

| Member | Type | Description |
|---|---|---|
| `Sts` | BRG_Status | Bridge status / handshake |
| `Frame` | BRG_Frame | Frame state |
| `Sensor` | BRG_Sensor[8] | Sensors by slot |
| `Chan` | BRG_Channel[8] | Measurement channels by slot |
