# ScanBridge – Architecture and Program Flow

ScanBridge moves data between **one ABB AccuRay Smart Platform frame** (through the AccuRay Object Server, OPC DA over DCOM) and a **ControlLogix PLC** (EtherNet/IP through pylogix). The scanner model lives inside ScanBridge, and the PLC is the only way in. HMIs and data-collection software talk to the PLC, never to ScanBridge.

## 0. Terms used by ABB, and what they mean here

| ABB term | What it is | In this system |
|---|---|---|
| **Smart Platform** (Scanning Platform) | The scanner frame itself: the O-frame, the measuring head and its sensors, and the frame's own controller (ASPC). It talks to the AOS over ASI, TCP 1827. | Each `PMx.SPy` branch in the OPC server is one Smart Platform |
| **AccuRay Object Server (AOS)** | Windows OPC DA server. It translates OPC items into ASI messages for one or more frames. It's a gateway, not a controller. | `AccuRay.Object.Server.1`, today remote, later on your cloud VM |
| **Host / controlling host** | The supervisory system that commands the frame through the AOS (scan, standardize, grade data). ABB's own QCS workstation fills this role in a full ABB system. | **ScanBridge, on behalf of the PLC.** The PLC decides, ScanBridge executes |
| **Workstation in control** (`WorkStnControlCode = 1`) | An ABB operator workstation has taken control of the frame | ScanBridge refuses host commands (result −8) |
| **(M\*) items** | Values the host should supply on connection and on grade change (scan time, box count, slopes/offsets…) | `ToBridge.Setup[]` table, written only when you ask (§ PLC Interface 7) |
| **TBM** (time-based measurement) | Array snapshots published every 1 s (TBM1S) or 5 s (TBM5S) | **TBM5S** by default |

## 1. System context

```mermaid
flowchart LR
    subgraph QCS["QCS LAN"]
        F1["PM8 SP1 frame"]
        F2["PM8 SP2 frame"]
    end
    subgraph AOSPC["AOS PC (remote today, cloud VM later)"]
        AOS["AccuRay Object Server<br/>items PM8.SP1.* / PM8.SP2.*"]
    end
    subgraph SB["ScanBridge PC – services run as .\win_dcom_user"]
        S1["ScanBridge-PM8-SP1"]
        S2["ScanBridge-PM8-SP2"]
    end
    subgraph PLC["ControlLogix"]
        T1["PM8_SP1_ToBridge / FromBridge"]
        T2["PM8_SP2_ToBridge / FromBridge"]
    end
    DC["Data collection software"]
    HMI["HMI"]
    F1 -->|"ASI TCP 1827"| AOS
    F2 -->|"ASI TCP 1827"| AOS
    AOS <-->|"OPC DA / DCOM"| S1
    AOS <-->|"OPC DA / DCOM"| S2
    S1 <-->|"pylogix"| T1
    S2 <-->|"pylogix"| T2
    T1 --> DC
    T2 --> DC
    HMI <--> PLC
```

**One ScanBridge instance per frame.** Each instance has its own config (`opc.item_prefix = "PM8.SP1"`), its own Windows service, its own PLC tag pair, and its own logs. A fault on one frame never affects another.

## 2. Processes and threads (one instance)

```mermaid
flowchart TB
    subgraph MAIN["Main process"]
        SUP["MainThread – Supervisor (1 s)<br/>worker heartbeat · TBM stall · thread watchdog · status.json"]
        RX["rx thread<br/>worker → model"]
        PLCT["plc thread – PlcService<br/>read ToBridge 250 ms · commands · write FromBridge 1 s"]
        MODEL[("ScannerModel<br/>frame · sensors · channels<br/>ProfileBuilders")]
        BROKER["WriteBroker"]
    end
    subgraph CHILD["Child process (killable)"]
        OPCW["OpcWorker – DCOM client<br/>SB_TRIG · SB_TBM · SB_STATE · SB_WRITE"]
    end
    OPCW -->|"tbm / values / write_result"| RX
    BROKER -->|"write / rediscover / reconnect"| OPCW
    OPCW -.->|"heartbeat"| SUP
    RX --> MODEL
    PLCT <--> MODEL
    PLCT --> BROKER
    SUP -->|"kill / respawn"| CHILD
```

A DCOM call to an unreachable node can block for minutes, and you can't interrupt it from Python. So DCOM lives in a child process that the supervisor can kill. MQTT is gone: the in-memory model is the buffer, and `status.json` plus the PLC `Sts` counters show that data is moving.

## 3. Discovery (item IDs → canonical keys)

```mermaid
flowchart LR
    A["Browse server<br/>MoveDown PM8 → SP1"] --> B{"in scope?<br/>starts with PM8.SP1."}
    B -->|no| X["ignored"]
    B -->|yes| C{"override in<br/>item_map.csv?"}
    C -->|yes| K["canonical from CSV"]
    C -->|no| D{"dotted parse"}
    D -->|"PM8.SP1.Mode"| K1["sp1.frame.Mode"]
    D -->|"PM8.SP1.WT1.MEAS.TBM5S"| K2["wt1.meas.TBM5S"]
    D -->|no match| E["path heuristics / unclassified"]
```

The frame is always `sp1` internally, because ABB's device tag for every frame is `sp1yyy`: `PM10.SP2.TBM1SDataBoxNumber` is device tag `sp1006`. The item-ID prefix is what keeps frames apart.

## 4. TBM data path (every 5 s)

```mermaid
sequenceDiagram
    autonumber
    participant A as AOS
    participant W as OPC worker
    participant M as Model / ProfileBuilder
    participant P as PLC thread
    participant L as PLC
    A-->>W: DataChange: TBM5SecDataReadyTrigger changed
    W->>A: SyncRead CACHE (TBM5SHeadPosition, TBM5SDataBoxNumber, every *.MEAS.TBM5S)
    alt same length, same timestamp, GOOD quality
        W-->>M: TBMFrame
        M->>M: update rolling profile + RollAvg  (every frame)
        M->>M: box 0 / reversal → complete scan → ScanAvg + Profile
    else mismatch
        W->>A: retry (snapshot_retries), then reject (SB-210)
    end
    P->>M: take profiles + snapshot (one lock)
    P->>L: write LiveProfile[600], Profile[600] (if new scan)
    P->>L: write changed scalars – LiveSeq / PVSeq / ScanSeq LAST
```

## 5. Two averages, two profiles

| PLC member | Updated | Built from | Use |
|---|---|---|---|
| `LiveProfile[600]`, `RollAvg`, `Roll2Sigma` | every TBM refresh (5 s) | the latest value of every box, dropping boxes older than `live_max_age_scans` × scan time (2 × by default) | **PID PV** (default `pv_source = "rolling"`), trending, data collection |
| `Profile[600]`, `ScanAvg`, `Scan2Sigma` | once per completed scan | one edge-to-edge pass | Reports, CD analysis, `pv_source = "scan"` |
| `Live` | every refresh | only the boxes crossed in the last 5 s | Diagnostics. Not representative of the full width |

`PV` equals whichever average `pv_source` selects, and `PVSeq` counts its updates.

## 6. Profile builder

```mermaid
stateDiagram-v2
    [*] --> Idle
    Idle --> Collecting: box > 0 (edge_start = previous element ≤ 0 or reversal)
    Collecting --> Collecting: store valid value at box
    Collecting --> Evaluate: box 0 (EOS) or reversal
    Collecting --> Idle: mode leaves scanning / stale / OPC lost (SB-403)
    Evaluate --> Accepted: edge_start AND span ≥ min_span AND coverage ≥ min_coverage
    Evaluate --> Rejected: otherwise (SB-402)
    Accepted --> Idle: ScanSeq++
    Rejected --> Idle: Rejected++
```

The rolling profile is updated from **every** valid positive box, whatever this state machine is doing. Invalid elements (−1000000) never enter either profile.

## 7. Request handshake

```mermaid
sequenceDiagram
    autonumber
    participant L as PLC logic
    participant P as PLC thread
    participant W as OPC worker
    participant A as AOS
    L->>L: load Code/Target/Tag/Value, then ReqID := ReqID + 1
    P->>L: read ToBridge (250 ms)
    P->>P: validate (code, slot, range, local/workstation guards)
    alt rejected
        P->>L: CmdResult = −n, CmdAck = ReqID
    else accepted
        P->>L: CmdBusy = ReqID
        P->>W: write batch
        W->>A: SyncWrite e.g. PM8.SP1.Command = 4
        W-->>P: results
        P->>L: CmdResult = 1 / −5, CmdAck = ReqID
    end
```

## 8. OPC worker connection states

```mermaid
stateDiagram-v2
    [*] --> Connecting
    Connecting --> Discovering: Connect OK (local or remote node), RUNNING
    Connecting --> Backoff: COM error (SB-204)
    Discovering --> Building: item map for PMx.SPy
    Building --> Serving: groups ready
    Building --> Backoff: trigger / frame TBM items missing (SB-207)
    Serving --> Backoff: COM error · state ≠ RUNNING · no trigger for stall_s (SB-213)
    Serving --> Connecting: rediscover / reconnect request
    Serving --> Serving: events, or polling if callbacks are blocked (SB-209)
    Backoff --> Connecting: 2 → 4 → … 60 s
```

## 9. Recovery layers

```mermaid
flowchart TB
    E1["COM error / AOS restart / network blip"] --> R1["worker reconnects itself (2…60 s)"]
    E2["DCOM call hangs"] --> R2["supervisor kills + respawns worker (SB-503/507)"]
    E3["connected but TBM silent > stall_s"] --> R3["reconnect, then respawn (SB-504)"]
    E4["PLC unreachable"] --> R4["PLC thread reconnects (2…30 s); stale requests expire (−9)"]
    E5["internal thread dead/hung"] --> R5["exit code 3 → service manager restarts (SB-506)"]
```

While any of these is recovering, `Chan[n].Quality` bit 0 drops within `stale_s` (13.5 s at TBM5S), so PLC loops hold.

## 10. Source map

| Module | Responsibility |
|---|---|
| `catalog.py` | Device tags, status bits, enumerations from the ABB manuals |
| `discovery.py` | Item IDs → canonical keys, frame scoping, CSV overrides |
| `opc_worker.py` | DCOM client: local/remote connect, scoped browse, groups, snapshots, writes |
| `sim_worker.py` | Simulator using your item-ID scheme (bench testing) |
| `tbm.py` / `profile.py` | TBM frame validation; rolling and completed-scan profiles |
| `model.py` | Scanner model, PV selection, quality bits, PLC snapshot |
| `plc_types.py` | **Single source of truth** for UDTs and codes |
| `l5x.py` | L5X generator (v20) |
| `plc_link.py` / `plc_service.py` | pylogix wrapper (ENBT/EN2T chunking) and the PLC thread |
| `commands.py` | Request validation, guards, setup table |
| `supervisor.py` | Process/thread supervision, status |
| `store.py` | SQLite history and audit |
