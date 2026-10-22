"""
ControlLogix data types - the SINGLE source of truth.

Drives: the L5X import files (l5x.py), the tag paths the bridge writes
(plc_service.py) and the tables in docs/02_PLC_Interface.md.

Rules
  * Two controller tags per FRAME:  <base>_ToBridge (PLC writes) and
    <base>_FromBridge (bridge writes, everyone else reads - including external
    data-collection software).
  * DINT for every flag (no BOOL members).
  * Members ending in "Seq" are written LAST in a batch: readers copy/latch data
    when a Seq changes and never see half-written data.
"""
from __future__ import annotations

from dataclasses import dataclass, field

VERSION_CODE = 10100      # 1.01.00


@dataclass
class Member:
    name: str
    dtype: str
    dim: int = 0
    desc: str = ""


@dataclass
class UDT:
    name: str
    desc: str
    members: list[Member] = field(default_factory=list)

    def member(self, name: str) -> Member:
        for m in self.members:
            if m.name == name:
                return m
        raise KeyError(name)


def build_udts(max_sensors: int = 8, max_channels: int = 8, profile_len: int = 600,
               setup_items: int = 32) -> list[UDT]:
    D, R = "DINT", "REAL"
    setup = UDT("BRG_SetupItem", "One grade/connection setup value written to the scanner by command 80", [
        Member("Enable", D, 0, "1 = send this item"),
        Member("Target", D, 0, "-1 = frame (sp1), 0..n = Sensor slot"),
        Member("Tag", D, 0, "Device tag data identifier yyy (e.g. 3 = Offset, 2 = Slope; frame 3 = SetScanTime)"),
        Member("Value", R, 0, "Value (REAL for Single items, rounded for integer items)"),
    ])
    to_bridge = UDT("BRG_ToBridge", "PLC/HMI -> ScanBridge. Requests execute only when ReqID changes.", [
        Member("Heartbeat", D, 0, "PLC increments every second"),
        Member("WriteEnable", D, 0, "1 = bridge may write to the scanner; 0 = every command rejected (-7)"),
        Member("ReqID", D, 0, "Increment (after loading Code/Target/Tag/Value) to request a command"),
        Member("Code", D, 0, "Command code - see PLC Interface spec"),
        Member("Target", D, 0, "-1 = frame, 0..n = Sensor slot"),
        Member("Tag", D, 0, "Command qualifier: yyy for generic write, component for slope/offset"),
        Member("ValueR", R, 0, "REAL argument"),
        Member("ValueI", D, 0, "Integer argument"),
        Member("Setup", "BRG_SetupItem", setup_items, "Grade/connection setup table"),
    ])
    status = UDT("BRG_Status", "Bridge health and command handshake", [
        Member("Heartbeat", D, 0, "Increments every write cycle (1 s)"),
        Member("State", D, 0, "0 stopped 1 starting 2 connecting 3 discovering 4 running 5 degraded 6 reconnecting"),
        Member("OPCConnected", D, 0, "1 = connected to AccuRay Object Server"),
        Member("OPCServerState", D, 0, "1 RUNNING 2 FAILED 3 NOCONFIG 4 SUSPENDED 5 TEST"),
        Member("PLCHbOK", D, 0, "1 = PLC heartbeat seen within timeout"),
        Member("CmdAck", D, 0, "ReqID of the last completed request"),
        Member("CmdBusy", D, 0, "ReqID currently executing (0 = idle)"),
        Member("CmdResult", D, 0, "Result of last request: 1 OK, <0 error"),
        Member("CmdDetail", D, 0, "Extra info (failed setup row, ...)"),
        Member("NeedsSetup", D, 0, "1 = scanner (re)connected and setup table not sent since"),
        Member("TBMRate", D, 0, "TBM refresh period in seconds (1 or 5)"),
        Member("TBMFrames", D, 0, "TBM frames accepted (rolls over)"),
        Member("TBMRejected", D, 0, "TBM snapshots rejected"),
        Member("TBMGaps", D, 0, "Missed TBM refreshes"),
        Member("TBMAgeMs", D, 0, "Age of last TBM frame, ms"),
        Member("OPCReconnects", D, 0, "OPC (re)connections since start"),
        Member("WorkerRestarts", D, 0, "OPC worker process restarts since start"),
        Member("PLCWriteErrs", D, 0, "Failed PLC writes since start"),
        Member("LastError", D, 0, "Last log message ID number (e.g. 204 for SB-204)"),
        Member("Version", D, 0, "Bridge version code"),
        Member("UptimeS", D, 0, "Seconds since bridge start"),
        Member("Epoch", D, 0, "Bridge PC clock, Unix seconds"),
    ])
    frame = UDT("BRG_Frame", "Smart Platform frame state", [
        Member("Mode", D, 0, "0 OffSheet 1 SnglPtReq 2 ScanReq 4 StdzReq 5 SinglePoint 6 Scan 7 AtEOS 8 Standardize 9 Local 10 ReadyToScan"),
        Member("Scanning", D, 0, "1 = Mode 6 or 7"),
        Member("ScanTime", D, 0, "Scan period s"),
        Member("NumBoxes", D, 0, "Number of data boxes"),
        Member("DataBoxWidth", R, 0, "Customer units"),
        Member("HeadPos", R, 0, "Last head position from TBM"),
        Member("Direction", D, 0, "+1 forward, -1 reverse, 0 not scanning"),
        Member("HomeEOS", R, 0, ""), Member("FarEOS", R, 0, ""),
        Member("HomeScanLimit", R, 0, ""), Member("FarScanLimit", R, 0, ""),
        Member("HomeMeasLimit", R, 0, ""), Member("FarMeasLimit", R, 0, ""),
        Member("SnglPtPos", R, 0, ""), Member("OffSheetPos", R, 0, ""),
        Member("ActiveAlarms", D, 0, "sp1004 bit field"),
        Member("SafetyAlarms", D, 0, "sp1052 bit field"),
        Member("DigitalInputs", D, 0, "sp1041 (bit0 shutter open, bit2 sheet break, bit7 frame local)"),
        Member("WorkStnCtrl", D, 0, "0 host in control, 1 workstation in control"),
        Member("AEOSStatus", D, 0, "1 enabled"), Member("AEOSError", D, 0, "sp1057"),
        Member("SheetBreak", D, 0, "Sheet break indicator"),
        Member("CalSmplStatus", D, 0, ""),
        Member("StdzEpoch", D, 0, "Unix s of last completed standardize"),
        Member("MinSinceStdz", R, 0, "Minutes since last standardize"),
        Member("ScanCount", D, 0, "Completed scans since start"),
        Member("ConfigArray", D, 20, "sp1044 sensor configuration array (raw)"),
        Member("ChangeSeq", D, 0, "Increments when any frame value changes (written last)"),
    ])
    sensor = UDT("BRG_Sensor", "One scanner sensor (wt1, mo1 ...)", [
        Member("Present", D, 0, "1 = discovered on the OPC server"),
        Member("TypeCode", D, 0, "1 wt 2 mo 3 mw 4 ca 5 as ... 12 ac (see spec)"),
        Member("Instance", D, 0, "Instance x of TTxyyy"),
        Member("Status", D, 0, "Raw sensor status word"),
        Member("StatusBad", D, 0, "Status AND bad-mask (non-zero = PV not trustworthy)"),
        Member("Shutter", D, 0, "0 n/a, 1 open, 2 closed"),
        Member("Slope", R, 0, "Slope read back"), Member("Offset", R, 0, "Offset read back"),
        Member("StdzWaitTime", D, 0, "Minutes before sensor requests standardize"),
        Member("StdzStatus", D, 0, "Last standardize status (0 = PASSED)"),
        Member("StdzEpoch", D, 0, "Last standardize time, Unix s"),
        Member("SmplChkStatus", D, 0, ""), Member("SmplChkEpoch", D, 0, ""),
        Member("CalSmplStatus", D, 0, ""), Member("CalSmplEpoch", D, 0, ""),
        Member("MaintAlarms", D, 0, "Maintenance alarm codes as bits (bit n = code 0x40+n)"),
        Member("ChangeSeq", D, 0, "Increments on any change (written last)"),
    ])
    channel = UDT("BRG_Channel", "One measurement channel (one TBM array) and its CD profiles", [
        Member("Present", D, 0, "1 = TBM array discovered"),
        Member("SensorSlot", D, 0, "Sensor[] slot this channel belongs to"),
        Member("Quality", D, 0, "Bit field - bit0 = PV valid (see spec)"),
        Member("PV", R, 0, "Process variable for PID (RollAvg or ScanAvg per config pv_source)"),
        Member("Live", R, 0, "Mean of valid elements of the latest TBM refresh (partial width)"),
        Member("RollAvg", R, 0, "Full-width average of the rolling profile - updated every TBM refresh"),
        Member("Roll2Sigma", R, 0, "2 sigma of the rolling profile"),
        Member("RollMin", R, 0, ""), Member("RollMax", R, 0, ""),
        Member("RollBoxes", D, 0, "Valid (fresh) boxes in the rolling profile"),
        Member("ScanAvg", R, 0, "Average of the last accepted complete scan"),
        Member("Scan2Sigma", R, 0, ""), Member("ScanMin", R, 0, ""), Member("ScanMax", R, 0, ""),
        Member("ValidBoxes", D, 0, "Valid boxes in last scan"),
        Member("FirstBox", D, 0, "First box (1-based) of last scan"),
        Member("LastBox", D, 0, "Last box (1-based) of last scan"),
        Member("Direction", D, 0, "+1 / -1 direction of last scan"),
        Member("Coverage", R, 0, "Valid / spanned boxes of last scan (0..1)"),
        Member("ScanTime", R, 0, "Duration of last scan, s"),
        Member("ScanEpoch", D, 0, "Completion time of last scan, Unix s"),
        Member("Rejected", D, 0, "Scans rejected for partial/low coverage/span"),
        Member("Profile", R, profile_len, "Last accepted complete-scan profile, box n at index n-1"),
        Member("LiveProfile", R, profile_len, "Rolling profile (latest fresh value per box), updated every TBM refresh"),
        Member("LiveSeq", D, 0, "Increments every TBM refresh: Live/Roll*/LiveProfile updated (written last)"),
        Member("PVSeq", D, 0, "Increments when PV updates (written last)"),
        Member("ScanSeq", D, 0, "Increments when ScanAvg/Profile update (written last)"),
    ])
    from_bridge = UDT("BRG_FromBridge", "ScanBridge -> PLC/HMI/data collection. Read only for everyone but the bridge.", [
        Member("Sts", "BRG_Status", 0, "Bridge status / handshake"),
        Member("Frame", "BRG_Frame", 0, "Frame state"),
        Member("Sensor", "BRG_Sensor", max_sensors, "Sensors by slot"),
        Member("Chan", "BRG_Channel", max_channels, "Measurement channels by slot"),
    ])
    return [setup, status, frame, sensor, channel, to_bridge, from_bridge]


CMD = {
    1: "OffSheet", 2: "SinglePoint", 3: "Scan", 4: "Standardize (ValueI 0 dirty / 1 clean)",
    5: "AEOS Enable", 6: "AEOS Disable", 7: "Inherent Sheet Break Enable",
    8: "Inherent Sheet Break Disable", 9: "Override Sheet Break", 10: "Allow Sheet Break",
    11: "Enable Scan",
    20: "Set Scan Time (ValueI s)", 21: "Set Number Of Data Boxes (ValueI)",
    22: "Set Single Point Position (ValueR)", 23: "Set Off Sheet Position (ValueR)",
    24: "Set Single Point Avg Time (ValueI 20-500 ms)", 25: "Set Scan Mode (ValueI 0 continuous / 1 wait at EOS)",
    26: "Set Ream Size (ValueR, 0 = g/m2)", 27: "Host Calibrate Sample", 28: "On Sheet Enable From Host",
    40: "Sensor Online", 41: "Sensor Offline", 42: "Set Slope (ValueR, Tag = component)",
    43: "Set Offset (ValueR, Tag = component)", 44: "Set Sample Check Mode (ValueI)",
    60: "Generic Write (Target, Tag = yyy, ValueR/ValueI)",
    80: "Send Setup Table", 81: "Rediscover OPC items", 82: "Reset Profiles",
    83: "Reconnect OPC", 84: "Clear Counters",
}
MOTION_CMDS = {1, 2, 3, 4, 9, 11, 22, 23, 27, 28}

RESULT = {
    0: "Idle", 1: "OK", -1: "Unknown command code", -2: "Bad Target (sensor slot / frame)",
    -3: "Tag not found or not writable", -4: "OPC not connected", -5: "OPC write failed",
    -6: "Value out of range", -7: "Disabled (WriteEnable=0 or config)",
    -8: "Frame in Local / workstation in control", -9: "Expired (issued while bridge/PLC link was down)",
    -10: "Timeout waiting for OPC worker", -11: "Busy",
}

QUALITY_BITS = {
    0: "PV valid (bits 1-6 all set)", 1: "OPC connected", 2: "TBM data fresh",
    3: "Frame scanning", 4: "Sensor status OK", 5: "Shutter open / not applicable",
    6: "PV fresh (rolling: enough fresh boxes; scan: last scan < 3 scan times old)",
    7: "Sensor standardizing", 8: "Sensor sample checking",
    9: "Last scan rejected", 10: "Frame in single point", 11: "Rolling profile coverage OK",
}
