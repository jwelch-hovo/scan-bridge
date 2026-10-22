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
