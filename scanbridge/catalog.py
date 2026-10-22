"""
Smart Platform tag catalog and enumerations.

Source documents (ABB, used as the authority for every number in this file):
  * Quality Control Object Server - Scanning Platform Device Tags  (3BUS 208 011)
  * Quality Control Object Server - Application Notes              (3BUS 208 008)

Device tags are of the form  TTxyyy
    TT  = tag type (wt = basis weight, mo = IR moisture, sp = frame ...)
    x   = instance number (1 = top / first, 2 = bottom / second)
    yyy = data identifier:   000-099 measurement, 100-199 standardize,
                             200-299 sample check, 300-399 calibrate sample

NOTE: the device tag instance of the FRAME is always sp1 - even for the second
frame of a machine (e.g. item PM10.SP2.TBM1SDataBoxNumber has device tag sp1006).
Frames are told apart by the item-ID prefix (opc.item_prefix), never by device tag.

Canonical keys used inside ScanBridge:  "<sensor>.<section>.<UserTag>"
    wt1.meas.TBM5S      mo1.stdz.Status      sp1.frame.Mode
"""
from __future__ import annotations

from dataclasses import dataclass, field

INVALID_MEASUREMENT = -1000000.0          # App Notes: invalid TBM element

MEAS, STDZ, SMPLCHK, CALSMPL, FRAME = "meas", "stdz", "smplchk", "calsmpl", "frame"
SECTIONS = (MEAS, STDZ, SMPLCHK, CALSMPL, FRAME)

SP_EVENTS = {
    0: "No Event", 2: "Time", 4: "On Demand", 8: "Standardize Complete",
    16: "Sample Check Complete", 32: "Calibrate Sample Complete", 64: "Edge-of-Sheet",
    256: "Alarm Mask Change", 512: "Digital Input Change", 1024: "Color Data Ready",
    2048: "1 Second TBM Data Available", 4096: "5 Second TBM Data Available",
}


@dataclass(frozen=True)
class TagDef:
    user: str                 # OPC User Tag name (leaf)
    yyy: int                  # data identifier
    dtype: str                # Character | Short | Long | Single | Boolean | Double
    access: str               # R | W | RW
    section: str = MEAS
    length: int = 1           # >1 = array
    mstar: bool = False       # (M*) must be supplied by host on connect / grade change
    subscribe: bool = True    # put into the state group (readable scalars)
    tt: str | None = None     # override tag-type prefix (doc oddities, e.g. X-Ray)

    @property
    def readable(self) -> bool:
        return "R" in self.access

    @property
    def writable(self) -> bool:
        return "W" in self.access


@dataclass
class SensorType:
    tt: str
    name: str
    type_code: int                        # value written to PLC  Sensor[n].TypeCode
    channels: dict[str, int]              # channel suffix -> TBM yyy
    tags: list[TagDef] = field(default_factory=list)
    status_bits: dict[int, str] = field(default_factory=dict)
    bad_mask: int = 0xC007
    shutter_closed_value: int | None = None
    name_patterns: tuple[str, ...] = ()

    def tag_by_user(self, user: str, section: str = MEAS) -> TagDef | None:
        for t in self.tags:
            if t.user == user and t.section == section:
                return t
        return None

    def writable_by_yyy(self, yyy: int) -> TagDef | None:
        for t in self.tags:
            if t.yyy == yyy and t.writable:
                return t
        return None

    def user_tags(self) -> set[str]:
        s = {t.user for t in self.tags}
        for suffix in self.channels:
            for rate in ("1S", "5S"):
                s.add(tbm_user_tag(suffix, rate))
        return s


# Status word bit tables (App Notes).  "(verify)" = ambiguous cell in the manual.
_COMMON_STATUS = {
    0: "Input Failure", 1: "No Initial Standardize", 2: "Asleep",
    3: "CD Unsynchronized", 4: "CD Position Invalid", 8: "Standardizing",
    9: "Sample Checking", 14: "Not Measuring", 15: "Standardize Failure",
}


def _status(**extra: str) -> dict[int, str]:
    d = dict(_COMMON_STATUS)
    for k, v in extra.items():
        d[int(k[1:])] = v
    return d


def _results() -> list[TagDef]:
    out = []
    for sec, base in ((STDZ, 100), (SMPLCHK, 200), (CALSMPL, 300)):
        out.append(TagDef("Status", base + 1, "Short", "R", sec))
        out.append(TagDef("Time", base + 0, "Character", "R", sec, length=20))
    return out


def _std_meas(*, slope_offset: bool = True, extra: list[TagDef] | None = None) -> list[TagDef]:
    t = [
        TagDef("Status", 1, "Short", "R"),
        TagDef("SetOffline", 4, "Long", "W", subscribe=False),
        TagDef("SetOnline", 5, "Long", "W", subscribe=False),
        TagDef("StdzWaitTime", 6, "Long", "R"),
    ]
    if slope_offset:
        t += [TagDef("Slope", 2, "Single", "RW", mstar=True),
              TagDef("Offset", 3, "Single", "RW", mstar=True)]
    return t + (extra or []) + _results()


def _w(user, yyy, dtype, mstar=False, access="W"):
    return TagDef(user, yyy, dtype, access, mstar=mstar, subscribe="R" in access)


def _r(user, yyy, dtype, length=1):
    return TagDef(user, yyy, dtype, "R", length=length)


_STD_CH = {"TBM": 0}
SENSOR_TYPES: dict[str, SensorType] = {}


def _add(st: SensorType) -> None:
    SENSOR_TYPES[st.tt] = st


_add(SensorType("wt", "Basis Weight", 1, _STD_CH, _std_meas(extra=[
    _r("ShutterState", 7, "Character"),
    _r("InherentSheetBreakDetectorState", 8, "Character"),
    _r("InternalChkSmplValue", 9, "Single"),
    _w("SetSmplChkMode", 10, "Long"),
    _r("SmplChkMode", 20, "Short"),
]), _status(b5="Flag Inserted", b6="Shutter Closed", b7="Measuring APC",
            b11="Unsynchronized Compensation"),
    bad_mask=0xC047, shutter_closed_value=1,
    name_patterns=(r"basis\s*weight", r"\bweight\b(?!.*(coat|cond|mylar))", r"nucleonic", r"\bbeta\b")))

_add(SensorType("mo", "IR Moisture", 2, _STD_CH, _std_meas(extra=[
    TagDef("SensorMode", 7, "Long", "RW", mstar=True),
    _w("SetSmplChkWeightComp", 8, "Single"),
    _w("SetSmplChkAshComp", 9, "Single"),
    _w("SetGradeWeightComp", 10, "Single", mstar=True),
    _w("SetGradeAshComp", 11, "Single", mstar=True),
    _w("SetSmplChkMode", 12, "Long"),
    _w("Set2FilterLinearizationConstant", 13, "Single", mstar=True),
    _r("SmplChkWtComp", 18, "Single"), _r("SmplChkAshComp", 19, "Single"),
    _r("GradeCodeWtComp", 20, "Single"), _r("GradeCodeAshComp", 21, "Single"),
    _r("SmplChkMode", 22, "Short"), _r("LinearizationConstant", 23, "Single"),
]), _status(b11="Unsynchronized Compensation"),
    name_patterns=(r"ir\s*moist", r"infra.?red", r"^moisture\b(?!.*micro)")))

_add(SensorType("mw", "Microwave Moisture", 3, _STD_CH, _std_meas(extra=[
    _w("SetGradeWtComp", 7, "Single", mstar=True),
    _w("SetGradeTempComp", 8, "Single", mstar=True),
    _w("SetSmplChkWtComp", 9, "Single"), _w("SetSmplChkTempComp", 10, "Single"),
    _w("SetSmplChkMode", 11, "Long"),
    _r("GradeCodeWtComp", 17, "Single"), _r("GradeCodeTempComp", 18, "Single"),
    _r("SmplChkWtComp", 19, "Single"), _r("SmplChkTempComp", 20, "Single"),
    _r("SmplChkMode", 21, "Short"),
]), _status(b11="Unsynchronized Compensation"), name_patterns=(r"microwave",)))

_add(SensorType("ca", "Caliper", 4, _STD_CH, _std_meas(extra=[
    _w("SetCustomerConversionUnits", 7, "Single", mstar=True),
    _r("CustomerConversionUnits", 8, "Single"),
    _r("MaintenanceAlarms", 9, "Character", length=16),
]), _status(b5="Local (verify)", b6="Proximeter Failure", b11="Sheet Missing",
            b12="Pressure Loss", b13="Vacuum Loss"), name_patterns=(r"caliper",)))

_add(SensorType("as", "Ash", 5, _STD_CH, _std_meas(extra=[
    _w("SetSmplChkWtComp", 7, "Single"), _w("SetSmplChkMoistComp", 8, "Single"),
    _w("SetGradeWtComp", 9, "Single", mstar=True), _w("SetGradeMoistComp", 10, "Single", mstar=True),
    _w("SetSmplChkMode", 11, "Long"), _w("SetGradeFlagState", 12, "Long", mstar=True),
    _w("SetSmplChkFlagState", 13, "Long"),
    TagDef("Units", 14, "Long", "RW", mstar=True),
    _r("ShutterState", 15, "Character"),
    _r("SmplChkWtComp", 17, "Single"), _r("SmplChkMoistureComp", 18, "Single"),
    _r("GradeCodeWtComp", 19, "Single"), _r("GradeCodeMoistureComp", 20, "Single"),
    _r("SmplChkMode", 21, "Short"), _r("GradeCodeStateFlag", 22, "Short"),
    _r("SmplChkFlagState", 23, "Short"),
]), _status(b5="Flag Inserted", b6="Shutter Closed", b7="Measuring APC",
            b11="Unsynchronized Compensation"),
    bad_mask=0xC047, shutter_closed_value=1, name_patterns=(r"^ash\b", r"\bash\b(?!.*x-?ray)")))

_add(SensorType("xr", "X-Ray Weight", 6, _STD_CH, [
    TagDef("Status", 1, "Short", "R", tt="as"),
    TagDef("Slope", 2, "Single", "RW", mstar=True, tt="as"),
    TagDef("Offset", 3, "Single", "RW", mstar=True, tt="as"),
    TagDef("SetOffline", 4, "Long", "W", subscribe=False),
    TagDef("SetOnline", 5, "Long", "W", subscribe=False),
    TagDef("StdzWaitTime", 6, "Long", "R", tt="as"),
    _w("SetSmplChkMode", 11, "Long"), _w("SetGradeFlagState", 12, "Long", mstar=True),
    _w("SetSmplChkFlagState", 13, "Long"),
    TagDef("ShutterState", 15, "Character", "R", tt="as"),
] + _results(), _status(b6="Shutter Closed"), bad_mask=0xC047, shutter_closed_value=1,
    name_patterns=(r"x-?ray",)))

_add(SensorType("st", "Sheet Temperature", 7, _STD_CH, _std_meas(), _status(), name_patterns=(r"temp",)))

_ct_tags = [
    TagDef("Status", 4, "Short", "R"),
    TagDef("SlopeMoisture", 5, "Single", "RW", mstar=True),
    TagDef("SlopeChalk", 6, "Single", "RW", mstar=True),
    TagDef("SlopeLatex", 7, "Single", "RW", mstar=True),
    TagDef("SlopeClay", 8, "Single", "RW", mstar=True),
    TagDef("OffsetMoisture", 9, "Single", "RW", mstar=True),
    TagDef("OffsetChalk", 10, "Single", "RW", mstar=True),
    TagDef("OffsetLatex", 11, "Single", "RW", mstar=True),
    TagDef("OffsetClay", 12, "Single", "RW", mstar=True),
    TagDef("SetOffline", 13, "Long", "W", subscribe=False),
    TagDef("SetOnline", 14, "Long", "W", subscribe=False),
    TagDef("StdzWaitTime", 15, "Single", "R"),
    _w("SetSmplChkMode", 16, "Long"),
    _r("SmplChkMode", 20, "Short"),
] + _results()
_add(SensorType("ct", "Coat Weight", 8, {"Moist": 0, "Chalk": 1, "Latex": 2, "Clay": 3}, _ct_tags,
                _status(b5="Flag Inserted", b6="Signal Too Small", b7="Measuring APC",
                        b10="Unsynchronized Data", b11="Unsynchronized Compensation",
                        b12="Input Out of Range", b13="Invalid Data"),
                name_patterns=(r"coat\s*weight",)))

_rt_tags = [
    TagDef("Status", 0, "Short", "R"),
    TagDef("SetOffline", 5, "Long", "W", subscribe=False),
    TagDef("SetOnline", 6, "Long", "W", subscribe=False),
    TagDef("StdzWaitTime", 7, "Long", "R"),
] + _results()
_add(SensorType("rt", "IR Ratio", 9, {"A1R1": 1, "A2R1": 2, "A3R1": 3, "A2R2": 4}, _rt_tags,
                _status(), name_patterns=(r"\bratio\b(?!.*fiber)",)))

_add(SensorType("cw", "Conditioned Weight", 10, _STD_CH, _std_meas(extra=[
    _w("SetConditionedWeightFactor", 7, "Single", mstar=True),
    _r("ConditionedWeightFactor", 8, "Single"),
]), {0: "BW Invalid", 1: "Moisture Invalid", 2: "CD Unsynchronized", 14: "Not Measuring"},
    bad_mask=0x4003, name_patterns=(r"cond\w*\s*weight", r"bone\s*dry")))

_add(SensorType("ap", "Apparent Density", 11, _STD_CH, _std_meas(),
                {0: "BW Invalid", 1: "Caliper Invalid", 2: "CD Unsynchronized", 14: "Not Measuring"},
                bad_mask=0x4003, name_patterns=(r"apparent\s*density",)))

_add(SensorType("ac", "Air Column", 12, _STD_CH, _std_meas(slope_offset=False),
                _status(b12="Delta Gap Too Big", b13="AI Too Big"), name_patterns=(r"air\s*column",)))

_add(SensorType("gl", "Gloss", 13, _STD_CH, _std_meas(extra=[
    _r("MaintenanceAlarms", 7, "Character", length=16)]),
    _status(b13="Insufficient Vacuum (verify)"), name_patterns=(r"gloss",)))

_add(SensorType("sm", "Smoothness", 14, _STD_CH, _std_meas(), _status(), name_patterns=(r"smooth",)))

_fiber_extra = [
    _w("SetSmplChkMode", 7, "Long"), _w("SetIntChkSmpl", 8, "Long"),
    _r("MaintenanceAlarms", 9, "Character", length=16),
    _r("SmplChkMode", 20, "Short"), _r("TypeOfIntChkSmpl", 21, "Short"),
]
_add(SensorType("fa", "Fiber Orientation Angle", 15, _STD_CH, _std_meas(extra=list(_fiber_extra)),
                _status(), name_patterns=(r"fiber.*angle",)))
_add(SensorType("fr", "Fiber Orientation Ratio", 16, _STD_CH, _std_meas(extra=list(_fiber_extra)),
                _status(), name_patterns=(r"fiber.*ratio",)))
_add(SensorType("br", "Brightness (OptiPak)", 17, _STD_CH, _std_meas(), _status(), name_patterns=(r"bright",)))
_add(SensorType("op", "Opacity (OptiPak)", 18, _STD_CH, _std_meas(), _status(), name_patterns=(r"opacity",)))
_add(SensorType("fo", "Formation (OptiPak)", 19, _STD_CH, _std_meas(), _status(), name_patterns=(r"formation",)))
_add(SensorType("co", "Color", 20, {}, [
    TagDef("Status", 1, "Short", "R"),
    TagDef("SetOffline", 4, "Long", "W", subscribe=False),
    TagDef("SetOnline", 5, "Long", "W", subscribe=False),
    TagDef("SetSmplChkMode", 51, "Short", "W", subscribe=False),
] + _results(), _status(), name_patterns=(r"colou?r",)))
_add(SensorType("uq", "Unique Sensor", 21, _STD_CH, _std_meas(), _status(), name_patterns=(r"unique",)))

TYPE_CODE_TO_TT = {st.type_code: tt for tt, st in SENSOR_TYPES.items()}


# Frame (Smart Platform) tags - sp1yyy
def _f(user, yyy, dtype, access="R", length=1, mstar=False, subscribe=None):
    if subscribe is None:
        subscribe = "R" in access
    return TagDef(user, yyy, dtype, access, FRAME, length, mstar, subscribe)


FRAME_TAGS: list[TagDef] = [
    _f("Command", 1, "Long", "W"),
    _f("Mode", 2, "Short"),
    _f("SetScanTime", 3, "Long", "W", mstar=True),
    _f("ActiveAlarms", 4, "Long"),
    _f("TBM1SHeadPosition", 5, "Single", length=500, subscribe=False),
    _f("TBM5SHeadPosition", 5, "Single", length=500, subscribe=False),
    _f("TBM1SDataBoxNumber", 6, "Short", length=500, subscribe=False),
    _f("TBM5SDataBoxNumber", 6, "Short", length=500, subscribe=False),
    _f("HomeEOSPosition", 8, "Single"),
    _f("FarEOSPosition", 9, "Single"),
    _f("SetNextStdzType", 10, "Character", "W"),
    _f("OffSheetHeadPosition", 11, "Single", "RW"),
    _f("StdzHeadPosition", 12, "Single"),
    _f("SetStdzHeadPosition", 13, "Single", "W"),
    _f("SnglPtHeadPosition", 14, "Single", "RW"),
    _f("HomePhysicalLimit", 15, "Single"),
    _f("HomeMeasureLimit", 16, "Single"),
    _f("HomeScanLimit", 17, "Single"),
    _f("HomeScanLimitNotAEOS", 18, "Single"),
    _f("SetHomeScanLimitNotInAEOS", 19, "Single", "W"),
    _f("FarScanLimitNotAEOS", 20, "Single"),
    _f("SetFarScanLimitNotInAEOS", 21, "Single", "W"),
    _f("FarScanLimit", 22, "Single"),
    _f("FarMeasureLimit", 23, "Single"),
    _f("FarPhysicalLimit", 24, "Single"),
    _f("CurrentHeadPosition", 29, "Single"),
    _f("DataBoxWidth", 30, "Single"),
    _f("HomeTrackingDistance", 31, "Single", "RW"),
    _f("FarTrackingDistance", 32, "Single", "RW"),
    _f("AEOSStatus", 33, "Character"),
    _f("SheetBreakDetectorState", 34, "Character"),
    _f("SetNumberOfDataBoxes", 35, "Long", "W", mstar=True),
    _f("NumberOfDataBoxes", 36, "Short"),
    _f("SheetBreakIndicatorStatus", 37, "Character"),
    _f("SetReamSize", 38, "Single", "W"),
    _f("WorkStnControlCode", 40, "Short"),
    _f("DigitalInputs", 41, "Character"),
    _f("SetScanMode", 42, "Character", "W"),
    _f("SetSnglPtAvgTime", 43, "Long", "W"),
    _f("ConfigurationArray", 44, "Long", length=20),
    _f("SetHomeMeasureLimit", 45, "Single", "W"),
    _f("SetFarMeasureLimit", 46, "Single", "W"),
    _f("ScanTime", 47, "Character"),
    _f("AirProfileMode", 48, "Character", "RW"),
    _f("TBM1SecDataReadyTrigger", 49, "Long", subscribe=False),
    _f("TBM5SecDataReadyTrigger", 49, "Long", subscribe=False),
    _f("EdgeOfSheetTrigger", 49, "Long"),
    _f("StandardizeCompTrigger", 49, "Long"),
    _f("SampleCheckCompTrigger", 49, "Long"),
    _f("CalibrateSampleCompTrigger", 49, "Long"),
    _f("AlarmMaskChangeTrigger", 49, "Long"),
    _f("DigitalInputChangeTrigger", 49, "Long"),
    _f("HostCalibrateSample", 50, "Long", "W"),
    _f("CalibrateSampleStatus", 51, "Long"),
    _f("SafetyAlarms", 52, "Short"),
    _f("SetCalibrateSampleMinTime", 53, "Long", "W"),
    _f("CalibrateSampleMinTime", 54, "Long"),
    _f("OnSheetEnableFromHost", 55, "Long", "W"),
    _f("ReamSize", 56, "Single"),
    _f("AEOSErrorStatus", 57, "Character"),
]
FRAME_KEY = "sp1"
FRAME_USER_TAGS = {t.user for t in FRAME_TAGS}


def frame_tag(user: str) -> TagDef | None:
    for t in FRAME_TAGS:
        if t.user == user:
            return t
    return None


def frame_writable_by_yyy(yyy: int) -> TagDef | None:
    for t in FRAME_TAGS:
        if t.yyy == yyy and t.writable:
            return t
    return None


# Enumerations
FRAME_MODE = {
    0: "Off Sheet", 1: "Single Point Requested", 2: "Scan Requested", 3: "(not used)",
    4: "Standardize Requested", 5: "Single Point", 6: "Scan", 7: "At Edge of Sheet",
    8: "Standardize", 9: "Local Control", 10: "Ready To Scan",
}
MODES_ABORT_SCAN = {0, 1, 4, 5, 8, 9, 10}

FRAME_COMMAND = {
    1: "Move head off-sheet", 2: "Move head to single point", 3: "Scan", 4: "Standardize",
    5: "Enable auto edge-of-sheet", 6: "Disable auto edge-of-sheet",
    7: "Enable inherent sheet-break detection", 8: "Disable inherent sheet-break detection",
    9: "Override sheet break", 10: "Allow sheet break", 11: "Enable Scan",
}
ACTIVE_ALARM_BITS = {
    2: "Frame", 3: "Weight", 4: "IR 1", 5: "Caliper", 6: "Optipak", 7: "Gloss", 8: "Ash",
    9: "General Alarm", 10: "Air Column", 11: "Electronics", 12: "Microwave", 13: "Sheet Temp",
    14: "Smoothness", 15: "Unique Sensor", 16: "Apparent Density", 17: "Cond. Weight",
    18: "Mylar Weight", 20: "Top Fiber Or.", 21: "Bottom Fiber Or.", 22: "Top Color",
    23: "Bottom Color", 24: "Top Ratio", 25: "Bottom Ratio", 26: "IR 2", 27: "IR 3",
    28: "Top Coat Wt.", 29: "Bottom Coat Wt.", 30: "Safety", 31: "Power Down",
}
SAFETY_ALARM_BITS = {
    0: "IR Chiller Flow Alarm", 1: "Caliper Probe Lifted (Loss of Process)",
    9: "Ash Head Tracking Error", 10: "Basis Weight Head Tracking Error",
    11: "Basis Weight Alarm Grid Broken", 12: "Ash Inherent Sheet Break",
    13: "Basis Weight Inherent Sheet Break", 14: "Ash Shutter Open When Should Be Closed",
    15: "Basis Weight Shutter Open When Should Be Closed",
}
DIGITAL_INPUT_BITS = {
    0: "Shutter Open", 1: "Reel Turn Up", 2: "Sheet Break Present",
    3: "Off Sheet at frame (remote) / reverse limit (local)",
    4: "On Sheet at frame (remote) / forward limit (local)",
    5: "Sample Check in progress", 6: "Calibrate Sample in progress", 7: "Frame in Local",
}
DI_FRAME_LOCAL = 7
AEOS_ERRORS = {
    8: "Far edge too close to home edge - head latched off sheet",
    9: "Home edge too close to far edge - head latched off sheet",
    10: "Reached far measure limit without seeing far edge - head latched off sheet",
    11: "Reached home measure limit without seeing home edge - head latched off sheet",
    12: "Illegal setup (e.g. homeHeadClearance > homeWindowClearance) - head latched off sheet",
    13: "Tracking distance too large - error logged only",
}
MAINT_ALARM_CODES = {
    "ca": {0x40: "Cal Full Scale Signal", 0x41: "Cal Full Scale Noise", 0x42: "Cal Zero Scale Signal",
           0x43: "Cal Low Air Pressure", 0x44: "Cal Air Bearing Signal", 0x45: "Cal Air Bearing Noise",
           0x46: "Cal Air Bearing Vacuum", 0x48: "Cal Top Pressure", 0x49: "Cal Bottom Pressure",
           0x59: "Cal Sensor Range"},
    "gl": {0x40: "Gloss windows are dirty"},
    "fa": {0x40: "Laser 1 current too low", 0x41: "Laser 2 current too low",
           0x42: "Laser 3 current too low", 0x43: "Input air temperature too high",
           0x44: "Optics block air temperature too high", 0x45: "Air pressure out of spec",
           0x46: "Board temperature too high"},
}
MAINT_ALARM_CODES["fr"] = MAINT_ALARM_CODES["fa"]

SENSOR_CLASS = {
    1: "Apparent Density", 2: "Ash w/o Gap", 3: "Ash w/ Gap", 4: "Beta w/o Gap", 5: "Beta w/ Gap",
    6: "Caliper", 7: "Conditioned Weight", 8: "Gap", 9: "Top Gloss", 10: "Bottom Gloss",
    11: "IR 2 Filter", 12: "IR 4 Filter", 13: "Microwave", 14: "Mylar Weight", 15: "Optipak",
    16: "Smoothness A", 17: "Smoothness B", 18: "Temperature", 19: "Unique", 20: "RFU", 21: "RFU",
    22: "Top Color", 23: "Bottom Color", 24: "Ratio", 25: "Ratio #2", 26: "IR2 2 Filter",
    27: "IR2 4 Filter", 28: "IR3 2 Filter", 29: "IR3 4 Filter", 30: "Top Coat Weight",
    31: "Bottom Coat Weight", 33: "Top Fiber Orientation", 34: "Bottom Fiber Orientation",
}
CLASS_TO_SENSOR_KEYS = {
    1: ["ap1"], 2: ["as1"], 3: ["as1"], 4: ["wt1"], 5: ["wt1"], 6: ["ca1"], 7: ["cw1"],
    8: ["ac1"], 9: ["gl1"], 10: ["gl2"], 11: ["mo1"], 12: ["mo1"], 13: ["mw1"],
    15: ["br1", "op1", "fo1"], 16: ["sm1"], 17: ["sm2"], 18: ["st1"], 19: ["uq1"],
    22: ["co1"], 23: ["co2"], 24: ["rt1"], 25: ["rt2"], 26: ["mo2"], 27: ["mo2"],
    28: ["mo3"], 29: ["mo3"], 30: ["ct1"], 31: ["ct2"], 33: ["fa1", "fr1"], 34: ["fa2", "fr2"],
}


@dataclass(frozen=True)
class ConfigEntry:
    index: int
    raw: int
    slot: int
    options: int
    instance: int
    sensor_class: int
    sensor_type: int

    @property
    def class_name(self) -> str:
        return SENSOR_CLASS.get(self.sensor_class, f"Unknown class {self.sensor_class}")

    @property
    def sensor_keys(self) -> list[str]:
        return CLASS_TO_SENSOR_KEYS.get(self.sensor_class, [])


def decode_config_array(values) -> list[ConfigEntry]:
    """sp1044: bits 31-28 ASPC slot, 27-24 options, 23-16 instance, 15-8 class, 7-0 type."""
    out = []
    for i, v in enumerate(values or []):
        try:
            u = int(v) & 0xFFFFFFFF
        except (TypeError, ValueError):
            continue
        if u == 0:
            continue
        out.append(ConfigEntry(i, u, (u >> 28) & 0xF, (u >> 24) & 0xF, (u >> 16) & 0xFF,
                               (u >> 8) & 0xFF, u & 0xFF))
    return out


def bits_to_names(value: int, table: dict[int, str], width: int = 32) -> list[str]:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return []
    return [table.get(b, f"bit{b}") for b in range(width) if v & (1 << b)]


def sensor_key_parts(key: str) -> tuple[str, int]:
    return key[:2], int(key[2:] or 1)


def device_tag(sensor_key: str, tag: TagDef) -> str:
    tt, inst = sensor_key_parts(sensor_key)
    return f"{tag.tt or tt}{inst}{tag.yyy:03d}"


def tbm_user_tag(channel_suffix: str, rate: str = "5S") -> str:
    return f"TBM{rate}" + ("" if channel_suffix == "TBM" else channel_suffix)


def channel_key_from_canonical(canonical: str, rate: str = "5S") -> str | None:
    """'wt1.meas.TBM5S' -> 'wt1.TBM';  'ct1.meas.TBM5SChalk' -> 'ct1.Chalk'."""
    try:
        sensor, section, user = canonical.split(".", 2)
    except ValueError:
        return None
    prefix = f"TBM{rate}"
    if section != MEAS or sensor == FRAME_KEY or not user.startswith(prefix):
        return None
    suffix = user[len(prefix):] or "TBM"
    st = SENSOR_TYPES.get(sensor[:2])
    if st is None or suffix not in st.channels:
        return None
    return f"{sensor}.{suffix}"
