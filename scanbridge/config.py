"""
Configuration loading (TOML, stdlib tomllib).

One config file = one scanner FRAME (e.g. PM8.SP1).  Every setting has a default.
See config/scanbridge.example.toml for the annotated file.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass
class SiteCfg:
    name: str = "PM8-SP1"
    description: str = ""


@dataclass
class OpcCfg:
    prog_id: str = "AccuRay.Object.Server.1"
    node: str = ""                          # "" / localhost = local AOS, else IP / host name of the AOS PC
    item_prefix: str = ""                   # MACHINE.FRAME, e.g. "PM8.SP1" - selects the frame
    automation_progid: str = "OPC.Automation.1"
    mode: str = "events"                    # events | poll
    tbm_rate: str = "5S"                    # 1S | 5S
    trigger_update_ms: int = 250
    state_update_ms: int = 1000
    poll_ms: int = 250
    snapshot_retries: int = 4
    snapshot_retry_ms: int = 150
    timestamp_tolerance_ms: int = 500
    init_security: bool = True
    auth_level: int = 1                     # 1=NONE 2=CONNECT
    impersonation_level: int = 2            # 2=IDENTIFY 3=IMPERSONATE
    discovery: str = "auto"                 # auto | file
    item_map_file: str = "item_map.csv"
    read_item_properties: bool = False      # dotted item IDs are enough; true = also read device-tag properties
    browse_max_depth: int = 6
    simulate: bool = False
    health_check_s: float = 5.0
    reconnect_min_s: float = 2.0
    reconnect_max_s: float = 60.0

    @property
    def is_local(self) -> bool:
        return self.node.strip().lower() in ("", ".", "localhost", "127.0.0.1")


@dataclass
class PlcCfg:
    ip: str = "192.168.1.10"
    slot: int = 0
    route: str = ""
    connection_size: int = 0                # 0 = auto, 508 = ENBT, 4002 = EN2T large
    to_bridge_tag: str = "PM8_SP1_ToBridge"
    from_bridge_tag: str = "PM8_SP1_FromBridge"
    socket_timeout_s: float = 5.0
    cmd_poll_ms: int = 250
    scalar_period_ms: int = 1000
    full_refresh_s: float = 60.0
    write_live_profile: bool = True         # Chan[n].LiveProfile updated every TBM refresh
    plc_heartbeat_timeout_s: float = 10.0
    stale_command_s: float = 10.0
    reconnect_min_s: float = 2.0
    reconnect_max_s: float = 30.0
    array_chunk: int = 0
    multi_write: bool = True
    simulate: bool = False


@dataclass
class ModelCfg:
    profile_len: int = 600
    max_sensors: int = 8
    max_channels: int = 8
    setup_items: int = 32
    pv_source: str = "rolling"              # rolling (updates every TBM refresh) | scan (once per scan)
    live_max_age_scans: float = 2.0         # rolling profile: box older than N scan times = invalid
    min_coverage: float = 0.80
    min_span: float = 0.25
    edge_trim_boxes: int = 0
    invalid_fill: float = 0.0
    reversal_boxes: int = 3
    stale_s: float = 0.0                    # 0 = auto (2.5 x TBM period + 1 s)
    stall_s: float = 30.0


@dataclass
class SensorSlotCfg:
    key: str = ""
    slot: int = -1
    bad_status_mask: int = -1


@dataclass
class ChannelSlotCfg:
    key: str = ""
    slot: int = -1
    scale: float = 1.0
    offset: float = 0.0
    eu: str = ""


@dataclass
class CommandCfg:
    allow_frame_motion: bool = True
    allow_generic_write: bool = True
    auto_setup_on_connect: bool = False     # see Operation manual "Who sets slopes/offsets"
    respect_local_control: bool = True
    timeout_s: float = 15.0


@dataclass
class SupervisorCfg:
    hang_timeout_s: float = 120.0
    restart_min_s: float = 5.0
    restart_max_s: float = 120.0
    stable_reset_s: float = 300.0
    thread_watchdog_s: float = 60.0
    fatal_exit_on_hung_thread: bool = True
    status_file: str = "status.json"
    status_period_s: float = 5.0


@dataclass
class StorageCfg:
    db: str = "data/scanbridge.db"
    history_scans: int = 500
    audit_days: int = 90


@dataclass
class LoggingCfg:
    dir: str = "logs"
    level: str = "INFO"
    console: bool = True
    max_mb: int = 10
    backups: int = 10
    event_log: bool = True


@dataclass
class Config:
    site: SiteCfg = field(default_factory=SiteCfg)
    opc: OpcCfg = field(default_factory=OpcCfg)
    plc: PlcCfg = field(default_factory=PlcCfg)
    model: ModelCfg = field(default_factory=ModelCfg)
    commands: CommandCfg = field(default_factory=CommandCfg)
    supervisor: SupervisorCfg = field(default_factory=SupervisorCfg)
    storage: StorageCfg = field(default_factory=StorageCfg)
    logging: LoggingCfg = field(default_factory=LoggingCfg)
    sensor: list[SensorSlotCfg] = field(default_factory=list)
    channel: list[ChannelSlotCfg] = field(default_factory=list)
    discovery_patterns: dict[str, str] = field(default_factory=dict)
    base_dir: str = "."

    def path(self, p: str) -> str:
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(self.base_dir, p))

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Config":
        cfg = Config()
        _fill(cfg, d)
        return cfg

    @property
    def tbm_period_s(self) -> float:
        return 1.0 if self.opc.tbm_rate == "1S" else 5.0

    @property
    def stale_s(self) -> float:
        return self.model.stale_s or (2.5 * self.tbm_period_s + 1.0)


def _fill(obj, data: dict, where: str = "") -> None:
    names = {f.name: f for f in fields(obj)}
    for k, v in data.items():
        if k not in names:
            raise ConfigError(f"Unknown setting '{where}{k}'")
        cur = getattr(obj, k)
        if is_dataclass(cur) and isinstance(v, dict):
            _fill(cur, v, f"{where}{k}.")
        elif k == "sensor":
            setattr(obj, k, [_mk(SensorSlotCfg, i, f"sensor[{n}].") for n, i in enumerate(v)])
        elif k == "channel":
            setattr(obj, k, [_mk(ChannelSlotCfg, i, f"channel[{n}].") for n, i in enumerate(v)])
        else:
            if cur is not None and not isinstance(cur, (dict, list)) and v is not None:
                try:
                    v = type(cur)(v)
                except (TypeError, ValueError) as e:
                    raise ConfigError(f"Bad value for '{where}{k}': {v!r} ({e})") from e
            setattr(obj, k, v)


def _mk(cls, data: dict, where: str):
    o = cls()
    _fill(o, data, where)
    return o


def load(path: str) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Config file not found: {p}")
    with p.open("rb") as fh:
        try:
            data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{p}: {e}") from e
    cfg = Config.from_dict(data)
    cfg.base_dir = str(p.resolve().parent)
    validate(cfg)
    return cfg


def validate(cfg: Config, for_run: bool = False) -> None:
    errs = []
    if cfg.opc.mode not in ("events", "poll"):
        errs.append("opc.mode must be 'events' or 'poll'")
    if cfg.opc.tbm_rate not in ("1S", "5S"):
        errs.append("opc.tbm_rate must be '1S' or '5S'")
    if cfg.opc.discovery not in ("auto", "file"):
        errs.append("opc.discovery must be 'auto' or 'file'")
    if for_run and not cfg.opc.simulate and not cfg.opc.item_prefix.strip():
        errs.append("opc.item_prefix is required (e.g. \"PM8.SP1\"); run 'scanbridge discover' to list frames")
    if cfg.plc.connection_size not in (0, 508, 4002):
        errs.append("plc.connection_size must be 0, 508 or 4002")
    m = cfg.model
    if m.pv_source not in ("rolling", "scan"):
        errs.append("model.pv_source must be 'rolling' or 'scan'")
    if not (1 <= m.profile_len <= 4000):
        errs.append("model.profile_len out of range 1..4000")
    if not (0 < m.min_coverage <= 1):
        errs.append("model.min_coverage must be in (0,1]")
    if m.stall_s and m.stall_s < 3 * cfg.tbm_period_s:
        errs.append(f"model.stall_s must be >= 3 x TBM period ({3 * cfg.tbm_period_s:.0f} s)")
    slots = [s.slot for s in cfg.sensor if s.slot >= 0]
    if len(slots) != len(set(slots)):
        errs.append("duplicate sensor slot numbers")
    if any(s >= m.max_sensors for s in slots):
        errs.append("sensor slot >= model.max_sensors")
    cslots = [c.slot for c in cfg.channel if c.slot >= 0]
    if len(cslots) != len(set(cslots)):
        errs.append("duplicate channel slot numbers")
    if any(s >= m.max_channels for s in cslots):
        errs.append("channel slot >= model.max_channels")
    for c in cfg.channel:
        if "." not in c.key:
            errs.append(f"channel key '{c.key}' must look like 'wt1.TBM'")
    if errs:
        raise ConfigError("; ".join(errs))
