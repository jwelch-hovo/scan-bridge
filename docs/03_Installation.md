# ScanBridge – Installation Manual

Expect 1–2 hours for the first frame and about 20 minutes for each one after. Every step ends with a check.

## 1. Prerequisites

| Item | Requirement |
|---|---|
| PC | Windows 10/11 or Server 2016+, reaching the AOS (DCOM) **and** the PLC (EtherNet/IP) |
| Python | **32-bit** Python 3.14 (x86). The OPC DA Automation wrapper is 32-bit |
| OPC | OPC Foundation Core Components (x86): `opcdaauto.dll`, `opcproxy.dll`, `opccomn_ps.dll` |
| Packages | `pywin32`, `pylogix` (`requirements.txt`) |
| Account | Local account **`.\win_dcom_user`**. For a remote AOS, the same name and password must exist on the AOS PC. All ScanBridge services run under it |
| Service wrapper | NSSM (recommended) or Task Scheduler |
| PLC | ControlLogix v20+, 1756-EN2T or ENBT |
| Firewall (remote AOS) | Bridge ↔ AOS: TCP 135 plus dynamic RPC, **both directions** (OPC callbacks). Bridge → PLC: TCP 44818 |

**Remote vs local AOS.**
* `opc.node = ""` (or `localhost`) connects to an AOS on the same PC. No remote DCOM is involved, so §4 steps 2–3 don't apply.
* When the AOS moves to your cloud VM, install ScanBridge on that VM, clear `node`, and keep everything else.
* Check before you move: the VM must reach the PLC on TCP 44818 with steady, low latency, because PLC heartbeats and quality flags depend on it.

## 2. Folder layout (one folder per frame)

```
C:\ScanBridge\                     application
C:\ScanBridge\sites\PM8-SP1\       scanbridge.toml, item_map.csv (optional), logs\, data\, plc\
C:\ScanBridge\sites\PM8-SP2\
C:\ScanBridge\sites\PM9-SP1\
```

## 3. Install Python and packages

```bat
py -3.14-32 -m pip install -r C:\ScanBridge\requirements.txt
py -3.14-32 -m win32com.client.makepy "OPC DA Automation Wrapper 2.02"
```

**Check:** `py -3.14-32 -c "import win32com.client as w; w.Dispatch('OPC.Automation.1'); print('OPC OK')"`. If you get `REGDB_E_CLASSNOTREG`, install the x86 OPC Core Components.

## 4. DCOM and account

1. Create `win_dcom_user` on the bridge PC (same password as on the AOS PC). Grant *Log on as a service*.
2. *(Remote AOS)* On the AOS PC, confirm `win_dcom_user` (or its group) has Access and Launch rights on *AccuRay Object Server* in `dcomcnfg`.
3. *(Remote AOS)* On the bridge PC, open `dcomcnfg` → My Computer → COM Security → Access Permissions → *Edit Limits*, and allow the AOS's account **Remote Access**. That's what lets data-change callbacks reach ScanBridge.
4. **Reboot** after any account or DCOM change (ABB Troubleshooting Guide).

**Check:** log on as `win_dcom_user` and confirm an OPC client (ABB OPC Inspector) shows live, good-quality items.

## 5. Find the frames and configure

```bat
runas /user:.\win_dcom_user "cmd /k cd /d C:\ScanBridge"
copy config\scanbridge.example.toml sites\PM8-SP1\scanbridge.toml
:: edit node, PLC ip; leave item_prefix = "" for now
py -3.14-32 -m scanbridge discover -c sites\PM8-SP1\scanbridge.toml
```

With `item_prefix` empty, `discover` lists the frames on the server (`PM8.SP1`, `PM8.SP2`, `PM9.SP1` …). Then:

1. Set `item_prefix = "PM8.SP1"`, `site.name = "PM8-SP1"`, and the PLC tags `PM8_SP1_ToBridge` / `PM8_SP1_FromBridge`.
2. Run `discover` again. It writes `item_map.discovered.csv` and prints the sensors (`wt1`, `mo1`, `ca1`, `ac1` …) and TBM5S channels.
3. Pin each sensor and channel slot in the `[[sensor]]` / `[[channel]]` sections. Configured sensors that the frame doesn't have are ignored with a warning.
4. Anything misclassified: add a row to `item_map.csv` (`item_id,canonical`, or `-` to ignore it).

**Check:** `py -3.14-32 -m scanbridge check -c sites\PM8-SP1\scanbridge.toml` prints no WARNING.

## 6. PLC

```bat
py -3.14-32 -m scanbridge gen-l5x -c sites\PM8-SP1\scanbridge.toml -o sites\PM8-SP1\plc
```

1. Import `01_…` through `07_…L5X` **in order**, once per controller.
2. Create the frame's two controller tags with External Access **Read/Write**.
3. Add the heartbeat and WriteEnable logic (PLC Interface §8.1), then download.

**Check:** `py -3.14-32 -m scanbridge test-plc -c sites\PM8-SP1\scanbridge.toml` reports every probe OK.

## 7. Bench test (optional, no hardware)

```bat
py -3.14-32 -m scanbridge simulate -c config\simulate.toml --fake-plc
py -3.14-32 tests\test_e2e_sim.py config\simulate.toml
```

## 8. First live run

```bat
py -3.14-32 -m scanbridge run -c sites\PM8-SP1\scanbridge.toml
```

Look for `SB-202 scanner online` and `SB-301 PLC connected`. Within about two scan times, `Chan[0].PVSeq` starts counting every 5 s. Stop with Ctrl+C.

## 9. Install as a service

```bat
tools\install_service.cmd PM8-SP1 "C:\ScanBridge\sites\PM8-SP1\scanbridge.toml" "<win_dcom_user password>"
```

This creates `ScanBridge-PM8-SP1`, which runs as `.\win_dcom_user`, starts automatically, and restarts on exit after 5 s. Repeat for each frame.

**Check:** `sc query ScanBridge-PM8-SP1` shows RUNNING, and `scanbridge status -c …` shows `"state": "running"`.

## 10. Retire the old transporter

Once a frame has run cleanly through a shift, stop the old transporter and its MQTT broker for that frame.
