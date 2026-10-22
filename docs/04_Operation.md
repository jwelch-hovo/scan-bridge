# ScanBridge – Operation Manual

Each frame (PM8.SP1, PM8.SP2 …) has its own service `ScanBridge-<frame>`, its own PLC tags, and its own logs.

## 1. What "normal" looks like

| Where | Signal | Normal |
|---|---|---|
| PLC | `<frame>_FromBridge.Sts.Heartbeat` | +1 every second |
| PLC | `Sts.State` | 4 (running) |
| PLC | `Sts.OPCConnected`, `Sts.PLCHbOK` | 1, 1 |
| PLC | `Sts.TBMRate` / `Sts.TBMAgeMs` | 5 / below 6000 |
| PLC | `Chan[n].Quality` bit 0 | 1 while scanning on sheet |
| PLC | `Chan[n].PVSeq`, `LiveSeq` | +1 every 5 s |
| PLC | `Chan[n].ScanSeq` | +1 per completed scan |
| PC | `scanbridge status -c …` | `"state": "running"`, `tbm_age_s` < 6 |

`Sts.State`: 0 stopped · 1 starting · 2 connecting · 3 discovering · 4 running · 5 degraded (connected but stale data or no PLC) · 6 reconnecting.

## 2. Start / stop / inspect

| Action | Command |
|---|---|
| Start / stop | `net start ScanBridge-PM8-SP1` · `net stop ScanBridge-PM8-SP1` |
| Status | `py -3.14-32 -m scanbridge status -c C:\ScanBridge\sites\PM8-SP1\scanbridge.toml` |
| Recent scans | `… scans -c … --channel wt1.TBM -n 5` (add `--csv out.csv` for profiles) |

Stopping the service is safe. Loops see Quality bit 0 drop and the heartbeat stop, and hold. No scanner settings change.

## 3. Operating the scanner through the PLC

| Task | Code | Arguments |
|---|---|---|
| Scan / Off sheet | 3 / 1 | – |
| Single point at a position | 22 then 2 | 22: ValueR = position |
| Standardize | 4 | ValueI 0 dirty / 1 clean |
| Sensor online / offline | 40 / 41 | Target = sensor slot |
| Slope / offset | 42 / 43 | Target = slot, ValueR |
| Grade setup | 80 | sends `ToBridge.Setup[]` |
| Re-browse after a Data Dictionary change | 81 | – |

## 4. Who sets slopes and offsets?

In ABB's documents the **host** is whoever commands the frame through the Object Server. Here that's ScanBridge, acting for the PLC. Nobody seems to send slopes and offsets today, and the measurements are right, so the frames keep their own values. That's why ScanBridge **doesn't write them at connect** by default.

* **Today:** read `Sensor[n].Slope` / `Offset` in the PLC. That shows what each frame is using.
* **If grades need different values:** put them in a recipe → `ToBridge.Setup[]` → code 80 on grade change.
* **Only once the recipes are trusted:** set `auto_setup_on_connect = true`.

## 5. What happens when something fails

| Event | ScanBridge does | PLC sees | Action |
|---|---|---|---|
| Network blip to remote AOS | reconnects (2 → 60 s) | OPCConnected 0, Quality bit 0 = 0 | none |
| AOS restarted | reconnect + rediscover | OPCReconnects +1 | none |
| DCOM call hangs | worker killed and respawned after 120 s | WorkerRestarts +1 | investigate if repeated |
| Connected, no TBM | reconnect, then respawn | TBMAgeMs rising | check frame/AOS if it persists |
| PLC unreachable | retries; requests made meanwhile expire | – | re-issue requests |
| PLC in Program | requests ignored | PLCHbOK 0 | – |
| ScanBridge crash | exits; service restarts it | Heartbeat stops briefly | investigate if repeated |

## 6. Maintenance

* Logs rotate at 10 MB × 10 files. The DB keeps 500 scans per channel and 90 days of request audit.
* Config change: run `scanbridge check`, then restart the service.
* New sensor on a frame: it gets the next free slot (log `SB-405`). Pin it in the config.
* Bigger arrays: change `[model]`, regenerate the L5X, re-import, download.
* Moving the AOS to the cloud VM: install ScanBridge there, set `node = ""`, and copy the site folders.

## 7. Safety (ABB Application Notes)

* Stations with frame controls must display the **shutter state**.
* Expose motion commands only where operators can see the frame. `commands.allow_frame_motion = false` blocks them completely.
