"""HRESULT / OPC error decoding so log lines say *what* failed, not just a number."""
from __future__ import annotations

HRESULTS = {
    0x80070005: ("E_ACCESSDENIED", "DCOM launch/access permission or account mismatch (win_dcom_user must exist on both PCs with the same password; DCOMCNFG Access/Launch permissions)"),
    0x800706BA: ("RPC_S_SERVER_UNAVAILABLE", "Server PC unreachable, firewall blocking TCP 135 / dynamic RPC ports, or AOS service stopped"),
    0x800706BE: ("RPC_S_CALL_FAILED", "Remote call failed - network drop or server process crashed"),
    0x800706BF: ("RPC_S_CALL_FAILED_DNE", "Remote call failed and did not execute - network drop"),
    0x80010108: ("RPC_E_DISCONNECTED", "Object invoked has disconnected from its clients (server restarted)"),
    0x8001FFFF: ("RPC_E_UNEXPECTED", "Internal RPC error - usually a dropped connection"),
    0x80080005: ("CO_E_SERVER_EXEC_FAILURE", "Server execution failed - AOS could not start (identity/licence); see Event Viewer on server"),
    0x80040154: ("REGDB_E_CLASSNOTREG", "Class not registered - OPC Automation wrapper (opcdaauto.dll) / OPC Core Components not installed for 32-bit"),
    0x800401F3: ("CO_E_CLASSSTRING", "Invalid ProgID - check opc.prog_id / opc.automation_progid"),
    0x800706F7: ("RPC_X_BAD_STUB_DATA", "Stub received bad data - proxy/stub (opcproxy.dll) mismatch"),
    0x80004005: ("E_FAIL", "Unspecified failure"),
    0x80070057: ("E_INVALIDARG", "Invalid argument"),
    0x8000FFFF: ("E_UNEXPECTED", "Catastrophic failure"),
    0x80004002: ("E_NOINTERFACE", "No such interface - proxy/stub not registered"),
    0xC0040004: ("OPC_E_BADTYPE", "Server cannot convert the written value to the item type"),
    0xC0040006: ("OPC_E_BADRIGHTS", "Item is not writable / not readable"),
    0xC0040007: ("OPC_E_UNKNOWNITEMID", "Item ID not in server address space (Data Dictionary)"),
    0xC0040008: ("OPC_E_INVALIDITEMID", "Item ID syntax invalid"),
    0xC004000C: ("OPC_E_DUPLICATENAME", "Duplicate group name"),
    0xC0040009: ("OPC_E_INVALIDFILTER", "Invalid filter"),
    0x0004000E: ("OPC_S_CLAMP", "Value written was clamped"),
    0xC004000D: ("OPC_E_RANGE", "Value out of range"),
}

OPC_QUALITY = {0: "BAD", 4: "BAD config error", 8: "BAD not connected", 12: "BAD device failure",
               16: "BAD sensor failure", 20: "BAD last known", 24: "BAD comm failure",
               28: "BAD out of service", 64: "UNCERTAIN", 192: "GOOD", 216: "GOOD local override"}

OPC_SERVER_STATE = {1: "RUNNING", 2: "FAILED", 3: "NOCONFIG", 4: "SUSPENDED", 5: "TEST", 6: "DISCONNECTED"}


def u32(code) -> int:
    try:
        return int(code) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return 0


def describe(code) -> str:
    c = u32(code)
    name, hint = HRESULTS.get(c, ("", ""))
    return f"0x{c:08X} {name}: {hint}" if name else f"0x{c:08X}"


def describe_com_error(exc: BaseException) -> str:
    args = getattr(exc, "args", ())
    parts = []
    if args:
        parts.append(describe(args[0]))
        if len(args) > 1 and args[1]:
            parts.append(str(args[1]))
        if len(args) > 2 and args[2]:
            try:
                scode = args[2][5]
                if scode:
                    parts.append("inner " + describe(scode))
                if args[2][2]:
                    parts.append(str(args[2][2]))
            except (TypeError, IndexError):
                pass
    return " | ".join(parts) or repr(exc)


def quality_text(q) -> str:
    try:
        q = int(q)
    except (TypeError, ValueError):
        return str(q)
    return OPC_QUALITY.get(q & 0xFC, f"q={q}")
