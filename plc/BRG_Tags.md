# Controller tags to create (scope: Controller)

| Tag name | Data type | External access | Written by |
|---|---|---|---|
| `PM8_SP1_ToBridge` | BRG_ToBridge | Read/Write | PLC logic / HMI |
| `PM8_SP1_FromBridge` | BRG_FromBridge | Read/Write | ScanBridge only - PLC logic and data collection read it |

One pair per scanner FRAME. The UDTs are shared: import them once per controller.

Layout generated for: max_sensors=8, max_channels=8,
profile_len=600, setup_items=32.
Approximate size of PM8_SP1_FromBridge: 40,020 bytes.

Import order: 01_BRG_SetupItem.L5X, 02_BRG_Status.L5X, 03_BRG_Frame.L5X, 04_BRG_Sensor.L5X, 05_BRG_Channel.L5X, 06_BRG_ToBridge.L5X, 07_BRG_FromBridge.L5X
