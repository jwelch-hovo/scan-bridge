"""
Generate Studio 5000 L5X import files for the BRG_* data types (v20 compatible).

Output (import in numeric order - leaves first):
    01_BRG_SetupItem.L5X ... 07_BRG_FromBridge.L5X  +  BRG_Tags.md
"""
from __future__ import annotations

import os
import time
from xml.sax.saxutils import escape

from .plc_types import UDT, build_udts

RADIX = {"DINT": "Decimal", "REAL": "Float", "INT": "Decimal", "SINT": "Decimal"}


def _member_xml(m) -> str:
    radix = RADIX.get(m.dtype, "NullType")
    desc = f"\n<Description>\n<![CDATA[{m.desc}]]>\n</Description>\n" if m.desc else ""
    return (f'<Member Name="{escape(m.name)}" DataType="{m.dtype}" Dimension="{m.dim}" Radix="{radix}" '
            f'Hidden="false" ExternalAccess="Read/Write">{desc}</Member>')


def udt_l5x(u: UDT, software_revision: str = "20.01", controller: str = "ScanBridge") -> str:
    stamp = time.strftime("%a %b %d %H:%M:%S %Y")
    members = "\n".join(_member_xml(m) for m in u.members)
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="{software_revision}" TargetName="{u.name}" TargetType="DataType" ContainsContext="true" ExportDate="{stamp}" ExportOptions="References NoRawData L5KData DecoratedData Context Dependencies ForceProtectedEncoding AllProjDocTrans">
<Controller Use="Context" Name="{controller}">
<DataTypes Use="Context">
<DataType Use="Target" Name="{u.name}" Family="NoFamily" Class="User">
<Description>
<![CDATA[{u.desc}]]>
</Description>
<Members>
{members}
</Members>
</DataType>
</DataTypes>
</Controller>
</RSLogix5000Content>
"""


def _udts(cfg) -> list[UDT]:
    m = cfg.model
    return build_udts(m.max_sensors, m.max_channels, m.profile_len, m.setup_items)


def generate(cfg, out_dir: str, software_revision: str = "20.01") -> list[str]:
    m = cfg.model
    udts = _udts(cfg)
    os.makedirs(out_dir, exist_ok=True)
    files = []
    for i, u in enumerate(udts, 1):
        p = os.path.join(out_dir, f"{i:02d}_{u.name}.L5X")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(udt_l5x(u, software_revision))
        files.append(p)
    size = _approx_size(udts)
    tags = os.path.join(out_dir, "BRG_Tags.md")
    with open(tags, "w", encoding="utf-8") as fh:
        fh.write(f"""# Controller tags to create (scope: Controller)

| Tag name | Data type | External access | Written by |
|---|---|---|---|
| `{cfg.plc.to_bridge_tag}` | BRG_ToBridge | Read/Write | PLC logic / HMI |
| `{cfg.plc.from_bridge_tag}` | BRG_FromBridge | Read/Write | ScanBridge only - PLC logic and data collection read it |

One pair per scanner FRAME. The UDTs are shared: import them once per controller.

Layout generated for: max_sensors={m.max_sensors}, max_channels={m.max_channels},
profile_len={m.profile_len}, setup_items={m.setup_items}.
Approximate size of {cfg.plc.from_bridge_tag}: {size['BRG_FromBridge']:,} bytes.

Import order: {', '.join(os.path.basename(f) for f in files)}
""")
    files.append(tags)
    return files


def _approx_size(udts: list[UDT]) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for u in udts:
        total = 0
        for m in u.members:
            s = {"DINT": 4, "REAL": 4}.get(m.dtype, sizes.get(m.dtype, 0))
            total += s * (m.dim or 1)
        sizes[u.name] = total
    return sizes


def markdown_tables(cfg) -> str:
    out = []
    for u in _udts(cfg):
        out.append(f"### {u.name}\n\n{u.desc}\n\n| Member | Type | Description |\n|---|---|---|")
        for mb in u.members:
            t = f"{mb.dtype}[{mb.dim}]" if mb.dim else mb.dtype
            out.append(f"| `{mb.name}` | {t} | {mb.desc} |")
        out.append("")
    return "\n".join(out)
