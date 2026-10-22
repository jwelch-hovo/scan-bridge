"""Unit tests - run with:  python -m pytest tests/test_units.py -q"""
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scanbridge import catalog as cat  # noqa: E402
from scanbridge import discovery as disc  # noqa: E402
from scanbridge.config import Config  # noqa: E402
from scanbridge.l5x import generate, udt_l5x  # noqa: E402
from scanbridge.model import maint_bits, parse_abb_time, to_int, to_text  # noqa: E402
from scanbridge.plc_types import build_udts  # noqa: E402
from scanbridge.profile import ProfileBuilder  # noqa: E402
from scanbridge.sim_worker import build_item_map  # noqa: E402
from scanbridge.tbm import SnapshotError, build_frame  # noqa: E402

INV = cat.INVALID_MEASUREMENT


def _scan(pb, boxes, value=lambda b: 50.0 + b / 100, ts=None):
    return pb.feed(boxes, [value(b) if b > 0 else INV for b in boxes], ts)


# ---------------------------------------------------------------- item IDs (your scheme)
def test_parse_user_examples():
    p = disc.parse_item_id
    assert p("PM8.SP1.ActiveAlarms", "PM8.SP1") == "sp1.frame.ActiveAlarms"
    assert p("PM8.SP1.Command", "PM8.SP1") == "sp1.frame.Command"
    assert p("PM10.SP2.TBM1SDataBoxNumber", "PM10.SP2") == "sp1.frame.TBM1SDataBoxNumber"
    assert p("PM9.SP1.WT1.MEAS.TBM1S", "PM9.SP1") == "wt1.meas.TBM1S"
    assert p("PM9.SP1.WT1.MEAS.TBM5S", "PM9.SP1") == "wt1.meas.TBM5S"
    assert p("PM9.SP1.MO1.MEAS.Status", "PM9.SP1") == "mo1.meas.Status"
    assert p("PM9.SP1.AC1.MEAS.TBM5S", "PM9.SP1") == "ac1.meas.TBM5S"
    assert p("PM9.SP1.CA1.STDZ.Status", "PM9.SP1") == "ca1.stdz.Status"
    assert p("pm9.sp1.wt1.meas.tbm5s", "PM9.SP1") == "wt1.meas.TBM5S"      # case-insensitive


def test_prefix_scoping_keeps_frames_apart():
    p = disc.parse_item_id
    assert p("PM8.SP2.Mode", "PM8.SP1") == ""
    assert p("PM80.SP1.Mode", "PM8.SP1") == ""                             # no prefix collision
    c = disc.Classifier(item_prefix="PM8.SP1")
    assert c.classify(disc.BrowsedItem("PM8.SP2.Mode")).how == "out-of-scope"
    assert disc.frames_in(["PM8.SP1.Mode", "PM8.SP2.Mode", "PM10.SP1.WT1.MEAS.TBM5S"]) == ["PM10.SP1", "PM8.SP1", "PM8.SP2"]


def test_simulator_ids_all_classified():
    m, browsed = build_item_map()
    assert all(c for _, c, _ in browsed)
    assert "sp1.frame.TBM5SecDataReadyTrigger" in m and "wt1.meas.TBM5S" in m and "mo1.stdz.Time" in m


# ---------------------------------------------------------------- profile
def test_full_scan_forward_with_eos_marker():
    pb = ProfileBuilder("wt1.TBM", 600)
    _scan(pb, [-5, -3, -1])
    res = _scan(pb, list(range(10, 591)) + [0, -2])
    sc = res[0].accepted
    assert (sc.first_box, sc.last_box, sc.valid_boxes, sc.direction) == (10, 590, 581, 1)
    assert abs(sc.avg - 53.0) < 1e-6


def test_partial_first_scan_rejected():
    pb = ProfileBuilder("wt1.TBM", 600)
    res = _scan(pb, list(range(300, 591)) + [0])
    assert res[0].accepted is None and "partial" in res[0].rejected_reason


def test_reverse_scan_and_reversal_without_marker():
    pb = ProfileBuilder("wt1.TBM", 600, min_span=0.1)
    _scan(pb, [-1])
    assert not _scan(pb, list(range(500, 100, -1)))
    res = _scan(pb, list(range(101, 400)))
    assert res and res[0].accepted and res[0].accepted.direction == -1
    assert _scan(pb, [0])[0].accepted.direction == 1


def test_low_coverage_rejected():
    pb = ProfileBuilder("wt1.TBM", 600)
    _scan(pb, [-1])
    boxes = list(range(1, 601)) + [0]
    res = pb.feed(boxes, [50.0 if (b > 0 and b % 2) else INV for b in boxes])
    assert res[0].accepted is None and "coverage" in res[0].rejected_reason


def test_rolling_profile_ages_out():
    pb = ProfileBuilder("wt1.TBM", 600)
    _scan(pb, list(range(1, 301)), ts=1000.0)
    _scan(pb, list(range(301, 601)), ts=1030.0)
    r = pb.rolling(1031.0, 40.0)
    assert r.n == 600 and abs(r.avg - (50 + 300.5 / 100)) < 1e-6
    r = pb.rolling(1050.0, 40.0)                     # first half older than 40 s
    assert r.n == 300 and r.profile[0] is None and r.profile[400] is not None


def test_abort_and_point_value():
    pb = ProfileBuilder("wt1.TBM", 600)
    _scan(pb, [-1] + list(range(1, 200)))
    assert pb.abort("mode") == 199 and not pb.collecting
    pb.feed([-1], [47.5])
    assert pb.point_value == 47.5


# ---------------------------------------------------------------- tbm / catalog
def test_build_frame_checks():
    f = build_frame([1, 2, 3], [1.0, 2.0, 3.0], {"wt1.TBM": [1, 2, 3], "mo1.TBM": [1, 2]},
                    {"a": 100.0, "b": 100.1}, 5, 0, 0.25)
    assert "wt1.TBM" in f.channels and f.dropped[0].startswith("mo1.TBM")
    for args in (([1, 2], [1.0], {}, {}), ([1], [1.0], {}, {"a": 100.0, "b": 101.0})):
        try:
            build_frame(*args, 1, 0, 0.25)
            raise AssertionError
        except SnapshotError:
            pass


def test_catalog_helpers():
    e = cat.decode_config_array([0, 0x10010402])[0]
    assert (e.slot, e.instance, e.sensor_class, e.sensor_type) == (1, 1, 4, 2) and e.sensor_keys == ["wt1"]
    assert cat.channel_key_from_canonical("wt1.meas.TBM5S", "5S") == "wt1.TBM"
    assert cat.channel_key_from_canonical("wt1.meas.TBM1S", "5S") is None
    assert cat.device_tag("wt1", cat.SENSOR_TYPES["wt"].tag_by_user("Offset")) == "wt1003"
    assert cat.frame_writable_by_yyy(35).user == "SetNumberOfDataBoxes"
    assert to_int("\x01") == 1 and to_int(b"\x02") == 2 and to_text([0x31, 0x32, 0]) == "12"
    assert parse_abb_time("10-21-2026 11:12:09") > 0 and maint_bits([0x40, 0x59]) == (1 | 1 << 25)


def test_path_heuristics_still_work():
    c = disc.Classifier()
    it = c.classify(disc.BrowsedItem("X/Coat Weight Top Chalk Measurement/TBM1S",
                                     ["X", "Smart Platform", "Coat Weight Top Chalk Measurement"], "TBM1S"))
    assert it.canonical == "ct1.meas.TBM1SChalk"


# ---------------------------------------------------------------- L5X
def test_l5x_well_formed(tmp_path):
    for u in build_udts():
        assert ET.fromstring(udt_l5x(u).encode()).attrib["TargetName"] == u.name
    ch = build_udts()[4]
    names = [m.name for m in ch.members]
    assert "LiveProfile" in names and "PV" in names and names[-3:] == ["LiveSeq", "PVSeq", "ScanSeq"]
    assert len(generate(Config(), str(tmp_path))) == 8
