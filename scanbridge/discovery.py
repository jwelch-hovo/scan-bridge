"""
Discovery: turn OPC item IDs into canonical keys.

Your AccuRay Data Dictionary uses dotted item IDs:

    PM8.SP1.ActiveAlarms               MACHINE.FRAME.<frame user tag>        -> sp1.frame.ActiveAlarms
    PM8.SP1.Command                                                           -> sp1.frame.Command
    PM10.SP2.TBM1SDataBoxNumber                                               -> sp1.frame.TBM1SDataBoxNumber
    PM9.SP1.WT1.MEAS.TBM1S             MACHINE.FRAME.SENSOR.SECTION.<tag>    -> wt1.meas.TBM1S

Only items under opc.item_prefix (e.g. "PM8.SP1") belong to this bridge.  The
frame is always canonical "sp1" internally, because the ABB device tag of every
frame is sp1yyy (PM10.SP2.TBM1SDataBoxNumber = sp1006).

Classification order (first that succeeds wins):
  1. item_map.csv override rows (canonical "-" = ignore item)
  2. dotted item-ID parse  (the scheme above)
  3. device tag found in OPC item properties (e.g. "wt1000")  [optional]
  4. path heuristics on component names (other naming schemes)

The full browse result is always written to item_map.discovered.csv.
"""
from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field

from . import catalog as cat

DEVICE_TAG_RE = re.compile(r"^(ac|ap|as|br|ca|co|ct|cw|fa|fo|fr|gl|mo|mw|op|rt|sm|sp|st|wt|xr|uq)(\d)(\d{3})$", re.I)
SENSOR_TOKEN_RE = re.compile(r"^([A-Za-z]{2})(\d)$")

# Section tokens in item IDs.  MEAS is confirmed; the others are best guesses -
# unknown tokens are reported in the discovery CSV (how = unclassified).
SECTION_TOKENS = {
    "MEAS": cat.MEAS, "MEASURE": cat.MEAS, "MEASUREMENT": cat.MEAS,
    "STDZ": cat.STDZ, "STD": cat.STDZ, "STANDARDIZE": cat.STDZ, "STANDARDISE": cat.STDZ,
    "SAMP": cat.SMPLCHK, "SMPLCHK": cat.SMPLCHK, "SAMPLECHECK": cat.SMPLCHK, "SMPCHK": cat.SMPLCHK, "SC": cat.SMPLCHK,
    "CALB": cat.CALSMPL, "CALSMPL": cat.CALSMPL, "CALIBRATESAMPLE": cat.CALSMPL, "CALSAMPLE": cat.CALSMPL, "CS": cat.CALSMPL
}

TYPE_PRIORITY = ["ct", "mw", "xr", "cw", "ap", "ac", "fa", "fr", "rt", "co", "gl", "sm",
                 "br", "op", "fo", "uq", "ca", "st", "mo", "as", "wt"]
FRAME_RE = re.compile(r"smart\s*platform|scanning\s*platform|\bframe\b|\bplatform\b", re.I)
SECTION_RES = [
    (cat.CALSMPL, re.compile(r"calibrat\w*\s*sample", re.I)),
    (cat.SMPLCHK, re.compile(r"sample\s*check|check\s*sample", re.I)),
    (cat.STDZ, re.compile(r"standardi[sz]", re.I)),
]
CT_QUALIFIERS = {"moisture": "Moisture", "chalk": "Chalk", "latex": "Latex", "clay": "Clay"}
CT_TBM_SUFFIX = {"Moisture": "Moist", "Chalk": "Chalk", "Latex": "Latex", "Clay": "Clay"}
_FRAME_UPPER = {u.upper(): u for u in cat.FRAME_USER_TAGS}
_TYPE_UPPER = {tt: {u.upper(): u for u in st.user_tags()} for tt, st in cat.SENSOR_TYPES.items()}


@dataclass
class BrowsedItem:
    item_id: str
    path: list[str] = field(default_factory=list)
    leaf: str = ""
    properties: dict = field(default_factory=dict)
    canonical: str = ""
    device_tag: str = ""
    how: str = ""                                    # override | itemid | property | path | unclassified | ignored


def in_scope(item_id: str, prefix: str) -> bool:
    return not prefix or item_id.upper().startswith(prefix.upper() + ".")


def parse_item_id(item_id: str, prefix: str) -> str:
    """Dotted-scheme parser. Returns canonical key or ''."""
    if prefix:
        if not in_scope(item_id, prefix):
            return ""
        rest = item_id[len(prefix) + 1:].split(".")
    else:
        parts = item_id.split(".")
        if len(parts) < 3:
            return ""
        rest = parts[2:]                               # assume MACHINE.FRAME.
    if len(rest) == 1:
        user = _FRAME_UPPER.get(rest[0].upper())
        return f"{cat.FRAME_KEY}.frame.{user}" if user else ""
    if len(rest) in (2, 3):
        m = SENSOR_TOKEN_RE.match(rest[0])
        if not m:
            return ""
        tt, inst = m.group(1).lower(), m.group(2)
        if tt not in cat.SENSOR_TYPES:
            return ""
        section = cat.MEAS
        if len(rest) == 3:
            section = SECTION_TOKENS.get(rest[1].upper().replace("_", ""))
            if section is None:
                return ""
        leaf = _TYPE_UPPER[tt].get(rest[-1].upper(), rest[-1])
        return f"{tt}{inst}.{section}.{leaf}"
    return ""


class Classifier:
    def __init__(self, extra_patterns: dict[str, str] | None = None, item_prefix: str = ""):
        self.prefix = item_prefix.strip().strip(".")
        self.patterns: dict[str, list[re.Pattern]] = {}
        for tt, st in cat.SENSOR_TYPES.items():
            pats = list(st.name_patterns)
            if extra_patterns and tt in extra_patterns:
                pats = [extra_patterns[tt]] + pats
            self.patterns[tt] = [re.compile(p, re.I) for p in pats]

    def classify(self, it: BrowsedItem) -> BrowsedItem:
        if not in_scope(it.item_id, self.prefix):
            it.how = "out-of-scope"
            return it
        c = parse_item_id(it.item_id, self.prefix)
        if c:
            it.canonical, it.how = c, "itemid"
            return it

        if self.prefix:
            it.how = "unclassified"
            return it

        dt = self._device_tag_from_props(it.properties)
        if dt:
            it.device_tag = dt
            c = self._from_device_tag(dt, it)
            if c:
                it.canonical, it.how = c, "property"
                return it
        c = self._from_path(it)
        it.canonical, it.how = (c, "path") if c else ("", "unclassified")
        return it

    @staticmethod
    def _device_tag_from_props(props: dict) -> str:
        for v in (props or {}).values():
            if isinstance(v, str) and DEVICE_TAG_RE.match(v.strip()):
                return v.strip().lower()
        return ""

    def _from_device_tag(self, dt: str, it: BrowsedItem) -> str:
        m = DEVICE_TAG_RE.match(dt)
        tt, inst, yyy = m.group(1).lower(), int(m.group(2)), int(m.group(3))
        leaf = it.leaf
        if tt == "sp":
            return f"sp1.frame.{leaf}"
        section = cat.MEAS if yyy < 100 else (cat.STDZ if yyy < 200 else (cat.SMPLCHK if yyy < 300 else cat.CALSMPL))
        if tt == "ct":
            leaf = self._ct_normalize(leaf, it.path)
        return f"{tt}{inst}.{section}.{leaf}"

    def _from_path(self, it: BrowsedItem) -> str:
        tokens = list(it.path) or re.split(r"[./\\:]", it.item_id)[:-1]
        leaf = it.leaf or re.split(r"[./\\:]", it.item_id)[-1]
        joined = " / ".join(tokens)
        section = cat.MEAS
        for sec, rx in SECTION_RES:
            if rx.search(joined):
                section = sec
                break
        for tok in reversed(tokens):
            for tt in TYPE_PRIORITY:
                if any(p.search(tok) for p in self.patterns[tt]):
                    inst = self._instance(tok, tt)
                    if tt == "ct":
                        leaf = self._ct_normalize(leaf, tokens)
                    return f"{tt}{inst}.{section}.{leaf}"
        if leaf in cat.FRAME_USER_TAGS:
            return f"sp1.frame.{leaf}"
        return ""

    @staticmethod
    def _instance(token: str, tt: str) -> int:
        t = token.lower()
        if re.search(r"\bbottom\b|\bbot\b", t):
            return 2
        if re.search(r"\btop\b", t):
            return 1
        if tt == "sm":
            m = re.search(r"smoothness\s*([ab])\b", t)
            if m:
                return 1 if m.group(1) == "a" else 2
        m = re.search(r"(\d+)\s*$", t) or re.search(r"\b(\d)\b", t)
        return int(m.group(1)) if m else 1

    @staticmethod
    def _ct_normalize(leaf: str, tokens: list[str]) -> str:
        qual = ""
        for tok in tokens:
            for k, v in CT_QUALIFIERS.items():
                if k in tok.lower():
                    qual = v
        if not qual:
            return leaf
        if leaf in ("TBM1S", "TBM5S"):
            return leaf + CT_TBM_SUFFIX[qual]
        if leaf in ("Slope", "Offset"):
            return leaf + qual
        return leaf


CSV_FIELDS = ["item_id", "canonical", "how", "device_tag", "path", "leaf"]


def write_csv(path: str, items: list[BrowsedItem]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, CSV_FIELDS)
        w.writeheader()
        for it in sorted(items, key=lambda i: (i.canonical or "~", i.item_id)):
            w.writerow({"item_id": it.item_id, "canonical": it.canonical, "how": it.how,
                        "device_tag": it.device_tag, "path": " / ".join(it.path), "leaf": it.leaf})


def read_overrides(path: str) -> dict[str, str]:
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            iid = (row.get("item_id") or "").strip()
            can = (row.get("canonical") or "").strip()
            if iid and can:
                out[iid] = can
    return out


def apply_overrides(items: list[BrowsedItem], overrides: dict[str, str]) -> None:
    for it in items:
        if it.item_id in overrides:
            it.canonical = "" if overrides[it.item_id] == "-" else overrides[it.item_id]
            it.how = "override" if it.canonical else "ignored"


def items_from_csv(path: str) -> list[BrowsedItem]:
    items = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            if (row.get("canonical") or "").strip() not in ("", "-"):
                items.append(BrowsedItem(row["item_id"].strip(), (row.get("path") or "").split(" / "),
                                         row.get("leaf", ""), {}, row["canonical"].strip(),
                                         row.get("device_tag", ""), "file"))
    return items


def to_item_map(items: list[BrowsedItem]) -> dict[str, str]:
    m: dict[str, str] = {}
    for it in items:
        if it.canonical and it.canonical not in m:
            m[it.canonical] = it.item_id
    return m


def duplicates(items: list[BrowsedItem]) -> dict[str, list[str]]:
    seen: dict[str, list[str]] = {}
    for it in items:
        if it.canonical:
            seen.setdefault(it.canonical, []).append(it.item_id)
    return {k: v for k, v in seen.items() if len(v) > 1}


def frames_in(item_ids) -> list[str]:
    """Distinct MACHINE.FRAME prefixes in a list of item IDs."""
    out = set()
    for iid in item_ids:
        p = iid.split(".")
        if len(p) >= 3:
            out.add(f"{p[0]}.{p[1]}")
    return sorted(out)
